#!/usr/bin/env python3
"""
wa_parser.py - parser export chat WhatsApp (Android-first, toleran terhadap varian lain).

Input  : folder export, file .zip, atau file .txt
Output : JSONL (1 baris header chat + 1 baris per pesan), siap diimpor ke database

Pemakaian:
    python wa_parser.py "C:\\...\\WhatsApp Chat with PROJECT TEAM" --out chat.jsonl
    python wa_parser.py export.zip --date-order DMY --hash-media

Fitur utama:
  * Deteksi urutan tanggal (M/D vs D/M) otomatis, dengan peringatan jika ambigu
  * Pesan multi-baris, lampiran ("file attached" / "<Media omitted>" + nama file di baris berikut)
  * Pesan sistem (added, left, subject changed, dst.), pesan dihapus, penanda "edited"
  * Link, mention, jenis media
  * msg_hash stabil -> impor berulang (export malam yang tumpang tindih) aman / idempotent
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# Konstanta & regex
# --------------------------------------------------------------------------- #

# Karakter tak terlihat yang disisipkan WhatsApp (LRM, RLM, embedding). Isolates
# (U+2068/2069) dipertahankan dulu karena dipakai untuk membaca @mention.
_STRIP = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"))
_SPACES = dict.fromkeys(map(ord, "\u202f\u00a0"), " ")  # NNBSP sebelum AM/PM pada Android baru

HEADER_RE = re.compile(
    r"^\[?(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>\d{2,4}),?\s+"
    r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[:.](?P<s>\d{2}))?\s*(?P<ap>[AaPp]\.?[Mm]\.?)?\]?"
    r"\s*(?:-\s+)?(?P<rest>.*)$"
)

# Frasa sistem yang muncul SEBELUM tanda ": " (nama grup bisa mengandung ":" sehingga
# pemisahan pengirim saja tidak cukup). Pola best-effort untuk bahasa Inggris & Indonesia;
# baris tanpa pengirim tetap dianggap sistem apa pun isinya.
SYSTEM_EVENTS = [
    ("encryption", re.compile(r"end-to-end encrypted|terenkripsi", re.I)),
    ("created", re.compile(r"created group|membuat grup", re.I)),
    ("subject", re.compile(r"changed the (group )?(subject|name)|mengubah (subjek|nama)", re.I)),
    ("description", re.compile(r"changed the group description|mengubah deskripsi", re.I)),
    ("icon", re.compile(r"changed this group's icon|changed the group icon|mengubah ikon", re.I)),
    ("pinned", re.compile(r"pinned a message|menyematkan pesan", re.I)),
    ("joined", re.compile(r"joined using|bergabung", re.I)),
    ("added", re.compile(r"\badded\b|menambahkan|ditambahkan", re.I)),
    ("removed", re.compile(r"\bremoved\b|mengeluarkan|dikeluarkan", re.I)),
    ("left", re.compile(r"\bleft$|\bkeluar$", re.I)),
    ("admin", re.compile(r"now an admin|admin", re.I)),
    ("security", re.compile(r"security code changed|kode keamanan", re.I)),
]
# Subset yang boleh dicek pada bagian kiri sebelum ": " (menghindari salah klasifikasi pesan biasa)
_SYSTEM_LEFT_RE = re.compile(
    r"changed the (group )?(subject|name|description|icon)|changed this group|created group|"
    r"pinned a message|joined using|mengubah (subjek|nama|deskripsi|ikon)|membuat grup|menyematkan pesan",
    re.I,
)

DELETED_BODIES = {
    "this message was deleted", "you deleted this message",
    "pesan ini telah dihapus", "anda menghapus pesan ini",
}
EDITED_RE = re.compile(r"\s*<(?:This message was edited|Pesan ini telah diedit)>\s*")
ATTACH_RE = re.compile(r"^(?P<fn>.+?\.[A-Za-z0-9]{2,5}) \((?:file attached|file terlampir)\)$")
OMITTED_RE = re.compile(r"^<(?:Media omitted|Media tidak disertakan)>$")
MENTION_RE = re.compile("@\u2068(.+?)\u2069")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{8,}$")
GROUP_FILE_RE = re.compile(
    r"^(?:WhatsApp Chat(?: with| -)|Chat WhatsApp dengan)\s*(?P<name>.+?)\.txt$", re.I
)

MEDIA_KINDS = {
    "image": {"jpg", "jpeg", "png", "gif", "heic", "bmp"},
    "sticker": {"webp"},
    "video": {"mp4", "3gp", "mov", "mkv", "avi"},
    "audio": {"opus", "mp3", "m4a", "aac", "ogg", "wav", "amr"},
    "document": {
        "pdf", "doc", "docx", "xls", "xlsx", "xlsm", "ppt", "pptx", "txt", "csv", "zip", "rar",
        "7z", "dwg", "dxf", "skp", "json", "kml", "kmz", "svg", "html", "md", "ods", "odt",
    },
}
_EXT_TO_KIND = {ext: kind for kind, exts in MEDIA_KINDS.items() for ext in exts}


def media_kind(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if filename.upper().startswith("STK-"):
        return "sticker"
    return _EXT_TO_KIND.get(ext, "other")


def _looks_like_filename(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 200 or "\n" in line or "/" in line or "\\" in line:
        return False
    if "." not in line:
        return False
    ext = line.rsplit(".", 1)[-1].lower()
    return ext in _EXT_TO_KIND


# --------------------------------------------------------------------------- #
# Struktur data
# --------------------------------------------------------------------------- #

@dataclass
class Attachment:
    filename: str | None          # None = media tidak diekspor & tanpa nama
    media_kind: str
    available: bool = False       # file benar-benar ada di export?
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass
class ParsedMessage:
    ts: datetime                  # waktu lokal (aware, mengikuti --tz)
    sender: str | None
    sender_phone: str | None
    kind: str                     # text | media | system | deleted
    body: str
    system_event: str | None = None
    edited: bool = False
    mentions: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    occurrence: int = 0
    msg_hash: str = ""
    src_line: int = 0


@dataclass
class ParsedChat:
    group_name: str
    date_order: str
    date_order_reason: str
    messages: list[ParsedMessage]
    warnings: list[str]


# --------------------------------------------------------------------------- #
# Tanggal
# --------------------------------------------------------------------------- #

def _build_naive(a, b, y, h, m, s, ap, order):
    day, month = (a, b) if order == "DMY" else (b, a)
    year = y + 2000 if y < 100 else y
    if ap:
        ap = ap.lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
    return datetime(year, month, day, h, m, s)


def _backward_jumps(entries, order):
    prev, jumps, invalid = None, 0, 0
    for e in entries:
        try:
            cur = _build_naive(*e, order)
        except ValueError:
            invalid += 1
            continue
        if prev and cur < prev:
            jumps += 1
        prev = cur
    return jumps, invalid


def detect_date_order(entries, default="MDY"):
    """entries: list of (a, b, y, h, m, s, ap). Kembalikan (order, alasan)."""
    any_a_gt12 = any(e[0] > 12 for e in entries)
    any_b_gt12 = any(e[1] > 12 for e in entries)
    if any_a_gt12 and not any_b_gt12:
        return "DMY", "komponen pertama > 12 (hari di depan)"
    if any_b_gt12 and not any_a_gt12:
        return "MDY", "komponen kedua > 12 (hari di belakang)"
    if any_a_gt12 and any_b_gt12:
        return default, "KONFLIK: kedua komponen pernah > 12, gunakan --date-order"
    j_dmy, inv_dmy = _backward_jumps(entries, "DMY")
    j_mdy, inv_mdy = _backward_jumps(entries, "MDY")
    if (j_dmy, inv_dmy) < (j_mdy, inv_mdy):
        return "DMY", f"ambigu; urutan DMY lebih konsisten kronologis ({j_dmy} vs {j_mdy} lompatan mundur)"
    if (j_mdy, inv_mdy) < (j_dmy, inv_dmy):
        return "MDY", f"ambigu; urutan MDY lebih konsisten kronologis ({j_mdy} vs {j_dmy} lompatan mundur)"
    return default, "AMBIGU: tidak bisa dibedakan, memakai default; cek manual atau gunakan --date-order"


def _tzinfo(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name == "Asia/Jakarta":  # tanpa DST, offset tetap UTC+7
        return timezone(timedelta(hours=7))
    return timezone.utc


# --------------------------------------------------------------------------- #
# Parser inti
# --------------------------------------------------------------------------- #

def _clean_line(line: str) -> str:
    return line.translate(_STRIP).translate(_SPACES)


def _split_entries(text: str, warnings: list[str]):
    """Pass 1: kelompokkan baris menjadi entri (header + baris lanjutan)."""
    entries, current = [], None
    orphan = 0
    for i, raw in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = _clean_line(raw)
        m = HEADER_RE.match(line)
        if m:
            current = {
                "nums": (
                    int(m["a"]), int(m["b"]), int(m["y"]), int(m["h"]), int(m["m"]),
                    int(m["s"] or 0), m["ap"],
                ),
                "rest": m["rest"],
                "extra": [],
                "line": i,
            }
            entries.append(current)
        elif current is not None:
            current["extra"].append(line)
        elif line.strip():
            orphan += 1
    if orphan:
        warnings.append(f"{orphan} baris sebelum header pertama diabaikan")
    return entries


def _split_sender(rest: str):
    """Kembalikan (sender|None, body, system_event|None)."""
    m = re.match(r"^(.+?): (.*)$", rest, re.S)
    if m:
        left, right = m.group(1), m.group(2)
    elif rest.endswith(":") and ": " not in rest:
        left, right = rest[:-1], ""
    else:
        return None, rest, _system_event(rest)
    if _SYSTEM_LEFT_RE.search(left):
        return None, rest, _system_event(rest)
    return left.strip(), right, None


def _system_event(text: str) -> str:
    for name, rx in SYSTEM_EVENTS:
        if rx.search(text):
            return name
    return "other"


def _extract_links(body: str):
    links, seen = [], set()
    for raw in URL_RE.findall(body):
        url = raw.rstrip(".,;:!?")
        if url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "domain": urlparse(url).netloc.lower()})
    return links


def _process_body(body: str, media_index):
    """Kembalikan (kind, body_bersih, attachments, edited)."""
    edited = bool(EDITED_RE.search(body))
    body = EDITED_RE.sub("", body).strip("\n")
    if body.strip().lower() in DELETED_BODIES:
        return "deleted", "", [], edited

    lines = body.split("\n")
    first = lines[0].strip()
    attachments, caption = [], lines

    m = ATTACH_RE.match(first)
    if m:
        fn = m["fn"].strip()
        attachments.append(Attachment(fn, media_kind(fn)))
        caption = lines[1:]
    elif OMITTED_RE.match(first):
        caption = lines[1:]
        if caption and _looks_like_filename(caption[0]):
            fn = caption[0].strip()
            attachments.append(Attachment(fn, media_kind(fn)))
            caption = caption[1:]
        else:
            attachments.append(Attachment(None, "other"))

    for att in attachments:
        if att.filename and media_index and att.filename in media_index:
            att.available = True
            att.size_bytes = media_index[att.filename].size

    kind = "media" if attachments else "text"
    return kind, "\n".join(caption).strip(), attachments, edited


def parse_chat_text(
    text: str,
    group_name: str = "",
    date_order: str | None = None,
    tz: str = "Asia/Jakarta",
    media_index: dict | None = None,
) -> ParsedChat:
    warnings: list[str] = []
    entries = _split_entries(text, warnings)
    if not entries:
        return ParsedChat(group_name, date_order or "MDY", "tidak ada header ditemukan", [],
                          warnings + ["tidak ada pesan yang terdeteksi; format tidak dikenali?"])

    if date_order:
        order, reason = date_order.upper(), "ditentukan manual"
    else:
        order, reason = detect_date_order([e["nums"] for e in entries])
    if reason.startswith(("AMBIGU", "KONFLIK")):
        warnings.append(f"urutan tanggal: {reason}")

    zone = _tzinfo(tz)
    messages: list[ParsedMessage] = []
    seen: Counter = Counter()
    invalid = 0

    for e in entries:
        try:
            naive = _build_naive(*e["nums"], order)
        except ValueError:
            invalid += 1
            continue
        ts = naive.replace(tzinfo=zone)

        sender, first_body, event = _split_sender(e["rest"])
        full = "\n".join([first_body] + e["extra"]) if sender is not None else "\n".join([first_body] + e["extra"]).strip()

        if sender is None:
            messages.append(ParsedMessage(ts, None, None, "system", full.strip(), system_event=event, src_line=e["line"]))
        else:
            mentions = MENTION_RE.findall(full)
            full = full.replace("\u2068", "").replace("\u2069", "")
            kind, body, atts, edited = _process_body(full, media_index)
            phone = re.sub(r"\D", "", sender) if PHONE_RE.match(sender) else None
            messages.append(ParsedMessage(
                ts, sender, phone, kind, body, edited=edited, mentions=mentions,
                links=_extract_links(body), attachments=atts, src_line=e["line"],
            ))

        msg = messages[-1]
        att_key = "|".join(a.filename or "" for a in msg.attachments)
        key = (msg.ts.isoformat(), msg.sender, msg.kind, msg.body, att_key)
        msg.occurrence = seen[key]
        seen[key] += 1

    if invalid:
        warnings.append(f"{invalid} header dengan tanggal tidak valid dilewati (cek urutan tanggal)")
    return ParsedChat(group_name, order, reason, messages, warnings)


def message_hash(group_key: str, m: ParsedMessage) -> str:
    """Hash stabil: sama untuk pesan yang sama di export berbeda."""
    att_key = "|".join(a.filename or "" for a in m.attachments)
    raw = "\x1f".join([group_key, m.ts.isoformat(), m.sender or "", m.kind, m.body, att_key, str(m.occurrence)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Loader: folder / zip / txt
# --------------------------------------------------------------------------- #

class _MediaRef:
    def __init__(self, size, reader):
        self.size, self._reader = size, reader

    def read(self) -> bytes:
        return self._reader()


def _pick_txt(names: list[str]) -> str | None:
    named = [n for n in names if GROUP_FILE_RE.match(Path(n).name)]
    if named:
        return named[0]
    chat = [n for n in names if Path(n).name.lower() in ("_chat.txt", "chat.txt")]
    if chat:
        return chat[0]
    return names[0] if len(names) == 1 else None


def load_export(path: str | Path):
    """Kembalikan (group_name, text, media_index{filename: _MediaRef})."""
    p = Path(path)
    if p.is_dir():
        txts = [f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".txt"]
        pick = _pick_txt(txts)
        if not pick:
            raise SystemExit(f"Tidak bisa menentukan file chat .txt di {p} (kandidat: {txts})")
        text = (p / pick).read_bytes().decode("utf-8-sig", errors="replace")
        index = {
            f.name: _MediaRef(f.stat().st_size, (lambda f=f: f.read_bytes()))
            for f in p.iterdir() if f.is_file() and f.name != pick
        }
        return _group_name(pick, p.name), text, index
    if p.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(p)
        infos = {Path(i.filename).name: i for i in zf.infolist() if not i.is_dir()}
        txts = [n for n in infos if n.lower().endswith(".txt")]
        pick = _pick_txt(txts)
        if not pick:
            raise SystemExit(f"Tidak bisa menentukan file chat .txt dalam zip (kandidat: {txts})")
        text = zf.read(infos[pick]).decode("utf-8-sig", errors="replace")
        index = {
            n: _MediaRef(i.file_size, (lambda i=i: zf.read(i)))
            for n, i in infos.items() if n != pick
        }
        return _group_name(pick, p.stem), text, index
    if p.suffix.lower() == ".txt":
        return _group_name(p.name, p.stem), p.read_bytes().decode("utf-8-sig", errors="replace"), {}
    raise SystemExit(f"Jenis input tidak dikenali: {p}")


def _group_name(txt_name: str, fallback: str) -> str:
    m = GROUP_FILE_RE.match(txt_name)
    return m["name"].strip() if m else fallback


# --------------------------------------------------------------------------- #
# Serialisasi & CLI
# --------------------------------------------------------------------------- #

def message_to_dict(m: ParsedMessage, group_key: str) -> dict:
    return {
        "_type": "message",
        "msg_hash": message_hash(group_key, m),
        "ts": m.ts.isoformat(),
        "sender": m.sender,
        "sender_phone": m.sender_phone,
        "kind": m.kind,
        "system_event": m.system_event,
        "body": m.body,
        "edited": m.edited,
        "mentions": m.mentions,
        "links": m.links,
        "attachments": [a.__dict__ for a in m.attachments],
        "occurrence": m.occurrence,
        "src_line": m.src_line,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse export chat WhatsApp ke JSONL")
    ap.add_argument("path", help="folder export, .zip, atau .txt")
    ap.add_argument("--out", help="file JSONL keluaran (default: tanpa keluaran, hanya ringkasan)")
    ap.add_argument("--group-key", help="kunci grup yang stabil antar-export (default: nama grup)")
    ap.add_argument("--date-order", choices=["MDY", "DMY"], help="paksa urutan tanggal")
    ap.add_argument("--tz", default="Asia/Jakarta")
    ap.add_argument("--hash-media", action="store_true", help="hitung sha256 tiap lampiran yang tersedia")
    args = ap.parse_args(argv)

    group_name, text, index = load_export(args.path)
    chat = parse_chat_text(text, group_name, args.date_order, args.tz, index)
    group_key = args.group_key or group_name

    if args.hash_media:
        for m in chat.messages:
            for a in m.attachments:
                if a.available:
                    a.sha256 = hashlib.sha256(index[a.filename].read()).hexdigest()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            header = {
                "_type": "chat", "group_name": group_name, "group_key": group_key,
                "date_order": chat.date_order, "date_order_reason": chat.date_order_reason,
                "tz": args.tz, "message_count": len(chat.messages), "warnings": chat.warnings,
            }
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            for m in chat.messages:
                f.write(json.dumps(message_to_dict(m, group_key), ensure_ascii=False) + "\n")

    kinds = Counter(m.kind for m in chat.messages)
    senders = Counter(m.sender for m in chat.messages if m.sender)
    missing = sum(1 for m in chat.messages for a in m.attachments if a.filename and not a.available)
    out = sys.stderr
    print(f"Grup           : {group_name}", file=out)
    print(f"Urutan tanggal : {chat.date_order} ({chat.date_order_reason})", file=out)
    print(f"Pesan          : {len(chat.messages)}  {dict(kinds)}", file=out)
    if chat.messages:
        print(f"Rentang        : {chat.messages[0].ts:%Y-%m-%d} s/d {chat.messages[-1].ts:%Y-%m-%d}", file=out)
    print(f"Pengirim       : {len(senders)}  (teratas: {', '.join(f'{s} ({c})' for s, c in senders.most_common(3))})", file=out)
    if index:
        print(f"Lampiran       : {sum(len(m.attachments) for m in chat.messages)} disebut, {missing} file tidak ada di export", file=out)
    for w in chat.warnings:
        print(f"PERINGATAN     : {w}", file=out)


if __name__ == "__main__":
    main()
