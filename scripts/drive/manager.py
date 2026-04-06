"""
drive/manager.py — Google Drive Integration

Setup:
  1. Google Cloud Console → Projekt erstellen
  2. Google Drive API aktivieren
  3. Service Account erstellen → JSON-Key downloaden
  4. Service Account Email im Drive-Ordner als Editor hinzufügen
  5. Key nach config/google_service_account.json kopieren
  6. pip install google-api-python-client google-auth
"""

import io
import json
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

UPLOAD_WORKERS = 4   # Parallele Upload-Threads

ROOT             = Path(__file__).resolve().parents[2]
CREDENTIALS_PATH = ROOT / "config" / "google_service_account.json"
DOWNLOAD_DIR     = ROOT / "data" / "drive_cache"

MIME_TYPES = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".mp4":  "video/mp4",
    ".mov":  "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".mkv":  "video/x-matroska",
    ".json": "application/json",
    ".txt":  "text/plain",
    ".pdf":  "application/pdf",
}

IMAGE_MIMES = ["image/jpeg", "image/png", "image/webp", "image/gif"]
VIDEO_MIMES = ["video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska"]

# Thread-sicherer Service-Cache
_service_lock = threading.Lock()
_drive_service = None


def _get_service():
    global _drive_service
    with _service_lock:
        if _drive_service:
            return _drive_service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Service Account Key fehlt: {CREDENTIALS_PATH}\n"
                    "Siehe drive/manager.py für Setup-Anleitung."
                )
            creds = service_account.Credentials.from_service_account_file(
                str(CREDENTIALS_PATH),
                scopes=["https://www.googleapis.com/auth/drive"],
            )
            _drive_service = build("drive", "v3", credentials=creds)
            return _drive_service
        except ImportError:
            raise ImportError(
                "Google Drive Pakete fehlen:\n"
                "pip install google-api-python-client google-auth"
            )

# ─── Folder Management ────────────────────────────────────────────────────────

def get_or_create_folder(name: str, parent_id: Optional[str] = None) -> str:
    """Gibt Folder-ID zurück. Erstellt Ordner falls nicht vorhanden."""
    service = _get_service()
    query   = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files   = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]

    folder = service.files().create(body=meta, fields="id").execute()
    print(f"[DRIVE] Ordner erstellt: {name}")
    return folder["id"]


def create_dated_folder(base_name: str, parent_id: Optional[str] = None) -> tuple[str, str]:
    """Erstellt Ordner: z.B. '2025-01-15_Mittwoch_hilda_feedback'."""
    now  = datetime.now()
    days = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    name = f"{now.strftime('%Y-%m-%d')}_{days[now.weekday()]}_{base_name}"
    return get_or_create_folder(name, parent_id), name

# ─── File Operations ──────────────────────────────────────────────────────────

def upload_file(local_path: Path, folder_id: str, rename: Optional[str] = None) -> str:
    from googleapiclient.http import MediaFileUpload
    service = _get_service()
    name    = rename or local_path.name
    mime    = MIME_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
    meta    = {"name": name, "parents": [folder_id]}
    media   = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
    file    = service.files().create(body=meta, media_body=media, fields="id").execute()
    print(f"[DRIVE] Hochgeladen: {name}")
    return file["id"]


def upload_text(content: str, filename: str, folder_id: str) -> str:
    from googleapiclient.http import MediaIoBaseUpload
    service = _get_service()
    mime    = MIME_TYPES.get(Path(filename).suffix.lower(), "text/plain")
    meta    = {"name": filename, "parents": [folder_id]}
    media   = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype=mime)
    file    = service.files().create(body=meta, media_body=media, fields="id").execute()
    print(f"[DRIVE] Text-Datei erstellt: {filename}")
    return file["id"]


def download_file(file_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload
    service = _get_service()
    request = service.files().get_media(fileId=file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        dl   = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest


def list_folder_files(folder_id: str, mime_filter: Optional[list[str]] = None) -> list[dict]:
    service = _get_service()
    query   = f"'{folder_id}' in parents and trashed=false"
    if mime_filter:
        mime_q  = " or ".join(f"mimeType='{m}'" for m in mime_filter)
        query  += f" and ({mime_q})"
    results = service.files().list(
        q      = query,
        fields = "files(id, name, mimeType, size, modifiedTime)",
    ).execute()
    return results.get("files", [])

# ─── High-level workflows ─────────────────────────────────────────────────────

def iter_drive_media(folder_id: str) -> Generator[tuple[dict, Path], None, None]:
    """
    Iterator: Lädt jede Mediendatei aus Drive einzeln in ein Temp-Verzeichnis,
    gibt (file_meta, temp_path) zurück und löscht die Temp-Datei danach.

    Verwendung:
        for meta, path in iter_drive_media(folder_id):
            result = analyze(path)   # path wird danach automatisch gelöscht
    """
    files  = list_folder_files(folder_id, IMAGE_MIMES + VIDEO_MIMES)
    print(f"[DRIVE] {len(files)} Mediendateien in Drive gefunden")

    tmpdir = Path(tempfile.mkdtemp())
    try:
        for f in files:
            local_path = tmpdir / f["name"]
            try:
                download_file(f["id"], local_path)
                yield f, local_path
            except Exception as e:
                print(f"  [ERROR] {f['name']}: {e}")
            finally:
                if local_path.exists():
                    local_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def upload_feedback_files(
    local_files: list[Path],
    label:       str,
    parent_id:   Optional[str] = None,
) -> str:
    folder_id, folder_name = create_dated_folder(f"{label}_feedback", parent_id)
    existing = [f for f in local_files if f.exists()]

    _lock    = threading.Lock()
    uploaded = [0]

    def _upload_one(f: Path) -> None:
        try:
            upload_file(f, folder_id)
            with _lock:
                uploaded[0] += 1
        except Exception as e:
            print(f"  [ERROR] {f.name}: {e}")

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        list(ex.map(_upload_one, existing))

    print(f"[DRIVE] {uploaded[0]}/{len(local_files)} Dateien in '{folder_name}'")
    return folder_id

# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Google Drive Manager")
    ap.add_argument("action", choices=["list", "download", "test"])
    ap.add_argument("--folder-id", default=None)
    args = ap.parse_args()

    if args.action == "test":
        try:
            svc   = _get_service()
            about = svc.about().get(fields="user").execute()
            print(f"[DRIVE] Verbunden als: {about['user']['emailAddress']}")
        except Exception as e:
            print(f"[DRIVE] Fehler: {e}")

    elif args.action == "list":
        if not args.folder_id:
            raise SystemExit("--folder-id erforderlich")
        for f in list_folder_files(args.folder_id):
            size = int(f.get("size") or 0)
            print(f"  {f['name']} ({f['mimeType']}) {size // 1024}KB")

    elif args.action == "download":
        if not args.folder_id:
            raise SystemExit("--folder-id erforderlich")
        out = DOWNLOAD_DIR / "media"
        out.mkdir(parents=True, exist_ok=True)
        count = 0
        for meta, tmp_path in iter_drive_media(args.folder_id):
            dest = out / meta["name"]
            shutil.copy2(tmp_path, dest)
            count += 1
        print(f"\n{count} Dateien heruntergeladen nach {out}")
