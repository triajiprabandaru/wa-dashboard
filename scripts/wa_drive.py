#!/usr/bin/env python3
"""
wa_drive.py - unggah media ke Google Drive dengan izin sempit (drive.file).

Persiapan:
    pip install google-api-python-client google-auth-oauthlib pillow
    simpan file OAuth client dari Google Cloud sebagai google_client.json

Otorisasi sekali (membuka browser):
    python wa_drive.py auth

drive.file hanya memberi akses ke file/folder yang DIBUAT aplikasi ini, jadi struktur
"WA Dashboard / <Project> / <Grup>" dibuat oleh kode, bukan manual.
"""
from __future__ import annotations

import io
import mimetypes
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _paths():
    return (
        os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "google_client.json"),
        os.environ.get("GOOGLE_TOKEN_FILE", "google_token.json"),
    )


def authorize() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file, token_file = _paths()
    if not Path(client_file).exists():
        sys.exit(f"File {client_file} tidak ditemukan. Unduh JSON OAuth client (tipe Desktop) dari Google Cloud.")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")
    print(f"Otorisasi berhasil. Token disimpan di {token_file} (jangan dibagikan).")


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, service=None):
        self.svc = service or self._build()
        self._cache: dict = {}

    @staticmethod
    def _build():
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        _, token_file = _paths()
        if not Path(token_file).exists():
            sys.exit("Belum ada token Google. Jalankan dulu: python wa_drive.py auth")
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                sys.exit(
                    "Token Google kedaluwarsa atau dicabut (umumnya karena aplikasi masih berstatus Testing, "
                    "yang membatasi token 7 hari). Pastikan status 'In production', lalu jalankan ulang: "
                    f"python wa_drive.py auth\n(detail: {e})"
                )
            Path(token_file).write_text(creds.to_json(), encoding="utf-8")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def ensure_path(self, names: list[str]) -> str:
        """Pastikan rantai folder ada (dibuat bila belum), kembalikan id folder terdalam."""
        parent = "root"
        for name in names:
            key = (parent, name)
            if key not in self._cache:
                q = (f"name = '{_q(name)}' and mimeType = 'application/vnd.google-apps.folder' "
                     f"and '{parent}' in parents and trashed = false")
                found = self.svc.files().list(q=q, fields="files(id)", pageSize=1, spaces="drive").execute()
                if found.get("files"):
                    fid = found["files"][0]["id"]
                else:
                    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}
                    fid = self.svc.files().create(body=body, fields="id").execute()["id"]
                self._cache[key] = fid
            parent = self._cache[key]
        return parent

    def upload(self, path, name: str, folder_id: str, mime: str | None = None):
        """Unggah resumable. Kembalikan (file_id, webViewLink)."""
        from googleapiclient.http import MediaFileUpload

        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
        req = self.svc.files().create(body={"name": name, "parents": [folder_id]}, media_body=media,
                                      fields="id,webViewLink")
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"], resp.get("webViewLink")

    def share_public(self, file_id: str) -> None:
        """Beri izin baca ke siapa pun yang punya link agar thumbnail bisa ditampilkan di browser tanpa auth."""
        self.svc.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()


def upload_to_drive(local_path, filename: str, group_name: str, project_name: str = "Default") -> tuple[str, str]:
    """Upload satu file ke 'WA Dashboard/<project>/<grup>' di Drive, kembalikan (file_id, view_url)."""
    drive = Drive()
    folder_id = drive.ensure_path(["WA Dashboard", project_name, group_name])
    file_id, view_url = drive.upload(local_path, filename, folder_id)
    drive.share_public(file_id)
    return file_id, view_url


def make_thumbnail(path, max_px: int = 480, quality: int = 70) -> bytes | None:
    """Buat pratinjau JPEG kecil secara lokal. None bila format tidak didukung (mis. HEIC)."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "auth":
        authorize()
    else:
        print(__doc__)
