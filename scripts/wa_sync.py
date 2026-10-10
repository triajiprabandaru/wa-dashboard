#!/usr/bin/env python3
"""
wa_sync.py - impor SEMUA ekspor WhatsApp di folder inbox dalam satu perintah.
Parse -> database Supabase -> unggah media ke Google Drive -> arsipkan yang selesai.

Susunan inbox (nama folder di bawah inbox = nama project):

    inbox/
      AMG/
        WhatsApp Chat with PROJECT TEAM/     <- folder ekspor (berisi .txt + media)
        WhatsApp Chat with Rizma House.zip   <- atau file zip
      Rumah Depok/
        ...
      _done/                                 <- hasil yang sudah selesai dipindah ke sini

Ekspor yang diletakkan langsung di inbox/ (tanpa folder project) hanya diproses bila --project diisi.

Pemakaian (dari root proyek):
    python scripts/wa_sync.py --dry-run     # uji coba: semua dijalankan lalu dibatalkan
    python scripts/wa_sync.py               # sinkronisasi sungguhan
    python scripts/wa_sync.py --no-drive    # hanya database, tanpa unggah media

Lokasi inbox: opsi --inbox, atau variabel WA_INBOX_DIR di .env, atau folder "inbox" (default).

Perilaku aman:
  * Idempotent: pesan yang sudah ada dilewati, media yang sudah terunggah tidak diunggah ulang.
  * Ekspor hanya dipindah ke _done/ bila seluruh langkahnya berhasil. Yang gagal tetap di inbox
    dan dicoba lagi pada sinkronisasi berikutnya.
  * Urutan tanggal ambigu (mis. 05/01/26) TIDAK ditebak: ekspor dilewati sampai kamu menentukan
    --date-order MDY atau DMY, karena salah tebak akan merusak data dan hash pesan.
  * Satu kunci (.sync.lock) mencegah dua sinkronisasi berjalan bersamaan (mis. Task Scheduler tumpang tindih).

Kode keluar: 0 bila semua berhasil, 1 bila ada yang gagal atau perlu perhatian.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wa_loader import connect, get_workspace, load_env, run_load, upload_pending
from wa_parser import load_export, message_to_dict, parse_chat_text

DONE_DIR = "_done"
LOCK_NAME = ".sync.lock"
LOCK_STALE_SECONDS = 12 * 3600


@dataclass
class Options:
    drive: bool = True
    dry_run: bool = False
    max_mb: float = 5120.0
    date_order: str | None = None
    tz: str = "Asia/Jakarta"


def log(msg: str = "") -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}" if msg else "", flush=True)


# --------------------------------------------------------------------------- #
# Menemukan ekspor di inbox
# --------------------------------------------------------------------------- #

def _hidden(p: Path) -> bool:
    return p.name.startswith(("_", "."))


def _is_export_item(p: Path) -> bool:
    if p.is_dir():
        return any(f.is_file() and f.suffix.lower() == ".txt" for f in p.iterdir())
    return p.is_file() and p.suffix.lower() in (".zip", ".txt")


def discover_exports(inbox: Path, default_project: str | None = None):
    """Kembalikan (daftar (path, project), daftar path tanpa project yang dilewati)."""
    found, orphans = [], []
    for child in sorted(inbox.iterdir()):
        if _hidden(child):
            continue
        if _is_export_item(child):
            if default_project:
                found.append((child, default_project))
            else:
                orphans.append(child)
        elif child.is_dir():
            for sub in sorted(child.iterdir()):
                if not _hidden(sub) and _is_export_item(sub):
                    found.append((sub, child.name))
    return found, orphans


# --------------------------------------------------------------------------- #
# Memproses satu ekspor
# --------------------------------------------------------------------------- #

def _content_root(base: Path) -> Path:
    """Bila zip berisi satu folder pembungkus, turun ke dalamnya."""
    entries = list(base.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return base


def process_export(conn, ws_id, path: Path, project: str, opts: Options) -> dict:
    """Parse + impor + (opsional) unggah media untuk satu ekspor.

    Status hasil: imported | dry_run | needs_attention | failed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmp)
            source = media_dir = _content_root(Path(tmp))
        elif path.is_dir():
            source = media_dir = path
        else:
            source, media_dir = path, path.parent

        group_name, text, index = load_export(source)
        chat = parse_chat_text(text, group_name, opts.date_order, opts.tz, index)
        result = {"group": group_name, "warnings": chat.warnings}

        if not chat.messages:
            return {**result, "status": "failed",
                    "error": "tidak ada pesan yang terdeteksi (format tidak dikenali?)"}

        reason = chat.date_order_reason
        if not opts.date_order and reason.startswith(("AMBIGU", "KONFLIK")):
            return {**result, "status": "needs_attention",
                    "error": f"urutan tanggal tidak pasti ({reason}); ulangi dengan --date-order MDY atau DMY"}

        header = {
            "group_name": group_name, "group_key": group_name,
            "date_order": chat.date_order, "date_order_reason": reason,
            "tz": opts.tz, "warnings": chat.warnings,
        }
        messages = [message_to_dict(m, group_name) for m in chat.messages]

        with conn.cursor() as cur:
            stats = run_load(cur, ws_id, header, messages, project, path.name)
        result["stats"] = stats

        if opts.dry_run:
            conn.rollback()
            return {**result, "status": "dry_run"}
        conn.commit()

        if opts.drive:
            with conn.cursor() as cur:
                media = upload_pending(cur, stats["group_id"], stats["group_name"], project,
                                       media_dir, opts.max_mb, commit=conn.commit)
            conn.commit()
            result["media"] = media
            if media["failed"]:
                return {**result, "status": "failed",
                        "error": f'{media["failed"]} media gagal diunggah; pesan sudah tersimpan, '
                                 "sisanya dicoba lagi pada sinkronisasi berikutnya"}
        return {**result, "status": "imported"}


def archive(path: Path, project: str, done_root: Path) -> Path:
    dest_dir = done_root / project
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{path.name}"
    shutil.move(str(path), str(dest))
    return dest


def describe(result: dict) -> str:
    parts = []
    s = result.get("stats")
    if s:
        parts.append(f'{s["new"]} pesan baru dari {s["total"]} (duplikat {s["duplicates"]}), '
                     f'{s["new_people"]} orang baru')
    m = result.get("media")
    if m:
        parts.append("media: " + ", ".join(f"{k}={v}" for k, v in m.items() if v))
    return "; ".join(parts)


def run_sync(conn, ws_id, exports, opts: Options, done_root: Path, keep: bool = False) -> list[dict]:
    results = []
    for n, (path, project) in enumerate(exports, start=1):
        log(f"({n}/{len(exports)}) [{project}] {path.name}")
        try:
            r = process_export(conn, ws_id, path, project, opts)
        except SystemExit as e:           # load_export melaporkan masalah lewat SystemExit
            conn.rollback()
            r = {"status": "failed", "error": str(e.code)}
        except Exception as e:             # satu ekspor bermasalah tidak boleh menghentikan yang lain
            conn.rollback()
            r = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        r.update(path=path, project=project)

        for w in r.get("warnings", []):
            log(f"    peringatan: {w}")
        detail = describe(r)
        if r["status"] == "imported":
            log(f"    OK. {detail}")
            if keep:
                log("    dibiarkan di inbox (--keep)")
            else:
                try:
                    dest = archive(path, project, done_root)
                    log(f"    dipindah ke {dest}")
                except OSError as e:
                    log(f"    PERINGATAN: gagal memindahkan ({e}). Tutup program yang memakai folder itu; "
                        "impor ulang aman.")
        elif r["status"] == "dry_run":
            log(f"    DRY RUN, dibatalkan. {detail}")
        elif r["status"] == "needs_attention":
            log(f"    DILEWATI: {r['error']}")
        else:
            log(f"    GAGAL: {r['error']}" + (f" ({detail})" if detail else ""))
        results.append(r)
    return results


# --------------------------------------------------------------------------- #
# Kunci agar tidak berjalan ganda
# --------------------------------------------------------------------------- #

@contextmanager
def sync_lock(inbox: Path):
    lock = inbox / LOCK_NAME
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock, flags)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
            sys.exit(f"Sinkronisasi lain sedang berjalan (kunci: {lock}). "
                     "Hapus file itu bila yakin tidak ada proses yang berjalan.")
        log("Kunci lama (>12 jam) diabaikan.")
        lock.unlink()
        fd = os.open(lock, flags)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description="Impor semua ekspor WhatsApp di folder inbox")
    ap.add_argument("--inbox", default=os.environ.get("WA_INBOX_DIR", "inbox"), help="folder inbox (default: inbox)")
    ap.add_argument("--project", help="project untuk ekspor yang diletakkan langsung di inbox (tanpa folder project)")
    ap.add_argument("--no-drive", action="store_true", help="jangan unggah media ke Google Drive")
    ap.add_argument("--dry-run", action="store_true", help="jalankan semuanya lalu batalkan; tidak ada yang berubah")
    ap.add_argument("--keep", action="store_true", help="jangan pindahkan ekspor yang selesai ke _done/")
    ap.add_argument("--date-order", choices=["MDY", "DMY"], help="paksa urutan tanggal untuk semua ekspor")
    ap.add_argument("--tz", default="Asia/Jakarta")
    ap.add_argument("--max-mb", type=float, default=5120, help="lewati media lebih besar dari ini (MB)")
    args = ap.parse_args(argv)

    inbox = Path(args.inbox)
    if not inbox.is_dir():
        sys.exit(f"Folder inbox tidak ditemukan: {inbox.resolve()}\n"
                 "Buat folder itu atau tentukan dengan --inbox / WA_INBOX_DIR.")

    exports, orphans = discover_exports(inbox, args.project)
    for o in orphans:
        log(f"DILEWATI (tanpa folder project, gunakan --project): {o.name}")
    if not exports:
        log("Tidak ada ekspor untuk diproses.")
        return 1 if orphans else 0

    opts = Options(drive=not args.no_drive, dry_run=args.dry_run, max_mb=args.max_mb,
                   date_order=args.date_order, tz=args.tz)
    log(f"{len(exports)} ekspor ditemukan" + (" (DRY RUN)" if opts.dry_run else "")
        + ("" if opts.drive else " (tanpa Drive)"))

    with sync_lock(inbox):
        with connect() as conn:
            with conn.cursor() as cur:
                ws_id = get_workspace(cur)
            results = run_sync(conn, ws_id, exports, opts, inbox / DONE_DIR, keep=args.keep)

    count = lambda s: sum(1 for r in results if r["status"] == s)  # noqa: E731
    log()
    log(f'Ringkasan: berhasil={count("imported")}, dry-run={count("dry_run")}, '
        f'perlu perhatian={count("needs_attention")}, gagal={count("failed")}')
    return 1 if (count("failed") or count("needs_attention") or orphans) else 0


if __name__ == "__main__":
    sys.exit(main())
