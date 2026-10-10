#!/usr/bin/env python3
"""
wa_loader.py - muat hasil wa_parser.py (JSONL) ke Supabase Postgres; unggah media ke Google Drive.

Persiapan (sekali):
    pip install "psycopg[binary]"
    salin .env.example menjadi .env lalu isi nilainya
    python wa_drive.py auth   ← otorisasi Google Drive (sekali, membuka browser)

Perintah:
    python wa_loader.py bootstrap  --workspace "Workspace Saya" --owner-email anda@email.com
    python wa_loader.py add-member --email teman@email.com --role editor --project "AMG"
    python wa_loader.py load chat.jsonl --project "AMG" --dry-run
    python wa_loader.py load chat.jsonl --project "AMG" --media-dir "C:\\...\\folder export" --drive

Aman dijalankan berulang: pesan yang sudah ada dilewati (berdasarkan msg_hash).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

BATCH = 500


# --------------------------------------------------------------------------- #
# Env & koneksi
# --------------------------------------------------------------------------- #

def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def connect():
    """Utamakan isian terpisah DB_HOST/DB_USER/DB_PASSWORD (aman untuk karakter khusus di password);
    DATABASE_URL tetap didukung sebagai cadangan."""
    try:
        import psycopg
    except ImportError:
        sys.exit('Pasang dulu: pip install "psycopg[binary]"')

    host = os.environ.get("DB_HOST")
    try:
        if host:
            missing = [k for k in ("DB_USER", "DB_PASSWORD") if not os.environ.get(k)]
            if missing:
                sys.exit("Isian .env belum lengkap: " + ", ".join(missing))
            return psycopg.connect(
                host=host, port=int(os.environ.get("DB_PORT", "5432")),
                user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                dbname=os.environ.get("DB_NAME", "postgres"), sslmode="require", connect_timeout=15,
            )
        url = os.environ.get("DATABASE_URL")
        if not url:
            sys.exit("Isi DB_HOST, DB_USER, DB_PASSWORD di .env (lihat .env.example)")
        return psycopg.connect(url, connect_timeout=15)
    except psycopg.OperationalError as e:
        msg = str(e).strip().splitlines()[0]
        sys.exit(
            f"Gagal terhubung ke database: {msg}\n"
            f"  host yang dicoba: {host or '(dari DATABASE_URL)'}\n"
            "  Periksa: host harus berakhiran .pooler.supabase.com, user berbentuk postgres.<kode-project>, "
            "dan password benar."
        )


def batched(rows, n=BATCH):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


# --------------------------------------------------------------------------- #
# Workspace, anggota, project
# --------------------------------------------------------------------------- #

def find_user(cur, email: str):
    cur.execute("select id from auth.users where lower(email) = lower(%s)", (email,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"Email {email} belum ada di Supabase > Authentication > Users. Undang dulu, lalu ulangi.")
    return row[0]


def get_workspace(cur, name: str | None = None):
    if name:
        cur.execute("select id from workspaces where name = %s order by created_at limit 1", (name,))
    else:
        cur.execute("select id from workspaces order by created_at limit 1")
    row = cur.fetchone()
    if not row:
        sys.exit('Workspace belum ada. Jalankan: python wa_loader.py bootstrap --workspace "Nama" --owner-email email')
    return row[0]


def get_project(cur, ws_id, name: str, create: bool):
    cur.execute("select id from projects where workspace_id = %s and name = %s limit 1", (ws_id, name))
    row = cur.fetchone()
    if row:
        return row[0]
    if not create:
        sys.exit(f'Project "{name}" belum ada.')
    cur.execute("insert into projects (workspace_id, name) values (%s, %s) returning id", (ws_id, name))
    return cur.fetchone()[0]


def cmd_bootstrap(args):
    with connect() as conn, conn.cursor() as cur:
        uid = find_user(cur, args.owner_email)
        cur.execute("select id from workspaces where name = %s limit 1", (args.workspace,))
        row = cur.fetchone()
        if row:
            ws_id = row[0]
        else:
            cur.execute("insert into workspaces (name) values (%s) returning id", (args.workspace,))
            ws_id = cur.fetchone()[0]
        cur.execute(
            "insert into workspace_members (workspace_id, user_id, role) values (%s, %s, 'owner') "
            "on conflict (workspace_id, user_id) do update set role = 'owner'",
            (ws_id, uid),
        )
    print(f'Workspace "{args.workspace}" siap. {args.owner_email} adalah owner.')


def cmd_add_member(args):
    with connect() as conn, conn.cursor() as cur:
        ws_id = get_workspace(cur)
        uid = find_user(cur, args.email)
        pid = get_project(cur, ws_id, args.project, create=False)
        cur.execute(
            "insert into workspace_members (workspace_id, user_id, role) values (%s, %s, 'viewer') "
            "on conflict (workspace_id, user_id) do nothing",
            (ws_id, uid),
        )
        cur.execute(
            "insert into project_members (project_id, user_id, role) values (%s, %s, %s) "
            "on conflict (project_id, user_id) do update set role = excluded.role",
            (pid, uid, args.role),
        )
    print(f'{args.email} sekarang {args.role} di project "{args.project}".')


# --------------------------------------------------------------------------- #
# Muat chat
# --------------------------------------------------------------------------- #

def read_jsonl(path: str):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    if not rows or rows[0].get("_type") != "chat":
        sys.exit("Baris pertama JSONL harus header chat (hasil wa_parser.py).")
    return rows[0], [r for r in rows[1:] if r.get("_type") == "message"]


def _clean(text):
    return text.replace("\x00", "") if isinstance(text, str) else text


def resolve_participants(cur, ws_id, aliases: dict):
    """aliases: {nama_di_export: nomor_atau_None}. Kembalikan ({alias: participant_id}, jumlah_baru)."""
    cur.execute("select alias, participant_id from participant_aliases where workspace_id = %s", (ws_id,))
    known = dict(cur.fetchall())
    created = 0
    for alias, phone in aliases.items():
        if alias in known:
            continue
        pid = uuid.uuid4()
        cur.execute(
            "insert into participants (id, workspace_id, display_name, phone) values (%s, %s, %s, %s)",
            (pid, ws_id, _clean(alias), phone),
        )
        cur.execute(
            "insert into participant_aliases (workspace_id, alias, participant_id) values (%s, %s, %s)",
            (ws_id, _clean(alias), pid),
        )
        known[alias] = pid
        created += 1
    return known, created


def run_load(cur, ws_id, header, messages, project_name=None, source_name=None):
    group_key = header.get("group_key") or header["group_name"]
    cur.execute(
        "insert into groups (workspace_id, name, source_key) values (%s, %s, %s) "
        "on conflict (workspace_id, source_key) do update set name = excluded.name returning id",
        (ws_id, _clean(header["group_name"]), group_key),
    )
    group_id = cur.fetchone()[0]

    if project_name:
        project_id = get_project(cur, ws_id, project_name, create=True)
        cur.execute(
            "insert into project_groups (project_id, group_id) values (%s, %s) on conflict do nothing",
            (project_id, group_id),
        )

    aliases = {}
    for m in messages:
        if m["sender"] and m["sender"] not in aliases:
            aliases[m["sender"]] = m.get("sender_phone")
    pmap, new_people = resolve_participants(cur, ws_id, aliases)

    cur.execute("select msg_hash from messages where group_id = %s", (group_id,))
    existing = {r[0] for r in cur.fetchall()}
    fresh = [m for m in messages if m["msg_hash"] not in existing]

    stamps = sorted(m["ts"] for m in messages) if messages else []
    cur.execute(
        "insert into imports (group_id, filename, date_order, tz, msg_total, msg_new, first_ts, last_ts, warnings) "
        "values (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s::jsonb) returning id",
        (
            group_id, source_name, header.get("date_order"), header.get("tz", "Asia/Jakarta"),
            len(messages), len(fresh), stamps[0] if stamps else None, stamps[-1] if stamps else None,
            json.dumps(header.get("warnings", []), ensure_ascii=False),
        ),
    )
    import_id = cur.fetchone()[0]

    msg_rows, att_rows, link_rows = [], [], []
    for m in fresh:
        mid = uuid.uuid4()
        msg_rows.append((
            mid, group_id, import_id, m["msg_hash"], m["ts"], _clean(m["sender"]),
            pmap.get(m["sender"]), m["kind"], m.get("system_event"), _clean(m["body"]),
            bool(m.get("edited")), [_clean(x) for x in m.get("mentions", [])], m.get("src_line"),
        ))
        for a in m.get("attachments", []):
            att_rows.append((uuid.uuid4(), mid, _clean(a.get("filename")), a["media_kind"],
                             a.get("size_bytes"), a.get("sha256")))
        for link in m.get("links", []):
            link_rows.append((uuid.uuid4(), mid, link["url"], link.get("domain")))

    for chunk in batched(msg_rows):
        cur.executemany(
            "insert into messages (id, group_id, import_id, msg_hash, ts, sender_alias, participant_id, kind, "
            "system_event, body, edited, mentions, seq) "
            "values (%s, %s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s)",
            chunk,
        )
    for chunk in batched(att_rows):
        cur.executemany(
            "insert into attachments (id, message_id, filename, media_kind, available, size_bytes, sha256) "
            "values (%s, %s, %s, %s, false, %s, %s)",
            chunk,
        )
    for chunk in batched(link_rows):
        cur.executemany("insert into links (id, message_id, url, domain) values (%s, %s, %s, %s)", chunk)

    return {
        "group_id": group_id, "group_name": header["group_name"],
        "total": len(messages), "new": len(fresh),
        "duplicates": len(messages) - len(fresh), "new_people": new_people,
        "attachments": len(att_rows), "links": len(link_rows),
    }


# --------------------------------------------------------------------------- #
# Unggah media ke Google Drive
# --------------------------------------------------------------------------- #

def upload_pending(cur, group_id, group_name: str, project_name: str | None, media_dir: Path, max_mb: float):
    from wa_drive import upload_to_drive

    cur.execute(
        "select a.id, a.filename from attachments a join messages m on m.id = a.message_id "
        "where m.group_id = %s and a.storage_path is null and a.filename is not null",
        (group_id,),
    )
    pending = cur.fetchall()
    stats = {"uploaded": 0, "reused": 0, "missing": 0, "too_big": 0, "failed": 0}
    for att_id, filename in pending:
        path = media_dir / filename
        if not path.is_file():
            stats["missing"] += 1
            continue
        size = path.stat().st_size
        if size > max_mb * 1024 * 1024:
            stats["too_big"] += 1
            print(f"  dilewati (>{max_mb:g} MB): {filename}")
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        cur.execute("select storage_path from attachments where sha256 = %s and storage_path is not null limit 1", (sha,))
        row = cur.fetchone()
        if row:
            file_id, reused = row[0], True
        else:
            try:
                file_id, _ = upload_to_drive(path, filename, group_name, project_name or "Default")
            except Exception as e:
                print(f"  GAGAL unggah {filename}: {e}")
                stats["failed"] += 1
                continue
            reused = False
        cur.execute(
            "update attachments set available = true, storage_path = %s, size_bytes = %s, sha256 = %s where id = %s",
            (file_id, size, sha, att_id),
        )
        stats["reused" if reused else "uploaded"] += 1
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_load(args):
    header, messages = read_jsonl(args.jsonl)
    print(f'Grup: {header["group_name"]}  |  {len(messages)} pesan di file')
    with connect() as conn:
        with conn.cursor() as cur:
            ws_id = get_workspace(cur)
            stats = run_load(cur, ws_id, header, messages, args.project, Path(args.jsonl).name)
        print(f'Pesan baru      : {stats["new"]}  (sudah ada sebelumnya: {stats["duplicates"]})')
        print(f'Orang baru      : {stats["new_people"]}')
        print(f'Lampiran / link : {stats["attachments"]} / {stats["links"]}')
        if args.dry_run:
            conn.rollback()
            print("DRY RUN: tidak ada yang disimpan.")
            return
        conn.commit()

        if args.drive:
            if not args.media_dir:
                sys.exit("--drive memerlukan --media-dir")
            with conn.cursor() as cur:
                up = upload_pending(
                    cur, stats["group_id"], stats["group_name"],
                    args.project, Path(args.media_dir), args.max_mb,
                )
            conn.commit()
            print("Media           : " + ", ".join(f"{k}={v}" for k, v in up.items()))
    print("Selesai.")


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(description="Muat chat WhatsApp ke Supabase Postgres + Google Drive")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="buat workspace pertama dan owner-nya")
    b.add_argument("--workspace", required=True)
    b.add_argument("--owner-email", required=True)
    b.set_defaults(fn=cmd_bootstrap)

    a = sub.add_parser("add-member", help="beri akses seseorang ke satu project")
    a.add_argument("--email", required=True)
    a.add_argument("--role", choices=["editor", "viewer"], default="editor")
    a.add_argument("--project", required=True)
    a.set_defaults(fn=cmd_add_member)

    l = sub.add_parser("load", help="impor JSONL hasil wa_parser")
    l.add_argument("jsonl")
    l.add_argument("--project", help="nama project; dibuat jika belum ada, grup ditautkan ke sana")
    l.add_argument("--media-dir", help="folder export yang berisi file media")
    l.add_argument("--drive", action="store_true", help="unggah media ke Google Drive (jalankan 'python wa_drive.py auth' dulu)")
    l.add_argument("--max-mb", type=float, default=5120, help="lewati file lebih besar dari ini dalam MB (default 5120 = 5 GB)")
    l.add_argument("--dry-run", action="store_true", help="jalankan semuanya lalu batalkan (rollback)")
    l.set_defaults(fn=cmd_load)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
