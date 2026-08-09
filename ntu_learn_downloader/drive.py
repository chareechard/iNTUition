"""Google Drive destination for the move pipeline.

Mirrors the local download tree into Drive, verifies each upload, then deletes the local
copy and records the result in the ledger.

Drive has no real paths - only files with parent ids - so every folder level has to be
looked up or created and then cached, otherwise a few hundred files would cost a few
hundred redundant queries.

Credentials
-----------
Google requires an OAuth client that belongs to *you*; there is no way to ship one. See
``SETUP_HELP`` (printed by the CLI) for the console steps. The consent screen opens in
your browser and Google hands the token straight back to the local flow - the tool never
sees your Google password.
"""
import os
from typing import Callable, Dict, List, Optional, Tuple

from ntu_learn_downloader.ledger import Ledger

# Only the app's own files are visible with drive.file, which is the narrowest scope
# that still allows creating folders and uploading. It cannot read anything the tool
# did not create.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

FOLDER_MIME = "application/vnd.google-apps.folder"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".ntu_learn_downloader")
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, "google_client_secret.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "google_token.json")

DEFAULT_ROOT_FOLDER = "NTULearn"


class DriveError(Exception):
    pass


SETUP_HELP = """\
Google Drive needs an OAuth client that belongs to your own Google account:

  1. https://console.cloud.google.com/  ->  create (or pick) a project
  2. APIs & Services -> Library -> enable "Google Drive API"
  3. APIs & Services -> OAuth consent screen -> External -> add your own
     Google address under "Test users"
  4. APIs & Services -> Credentials -> Create credentials
     -> OAuth client ID -> Desktop app -> Download JSON
  5. Save that file as:
     {path}

The first push opens a browser for consent; the token is then cached at
{token} and reused.\
""".format(
    path=CLIENT_SECRET_PATH, token=TOKEN_PATH
)


def _require_libs():
    """Import the Google client lazily so the rest of the tool runs without it."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.http import MediaFileUpload  # noqa: F401
    except ImportError as e:
        raise DriveError(
            "Google Drive support needs extra packages:\n"
            "    pip install google-api-python-client google-auth-oauthlib\n"
            "({})".format(e)
        )


def ensure_config_dir() -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    return CONFIG_DIR


def credentials_present() -> bool:
    return os.path.exists(CLIENT_SECRET_PATH)


def inspect_client_secret() -> Dict:
    """Validate the downloaded OAuth client JSON before we try to use it.

    The two mistakes that actually happen are downloading a *service account* key or a
    *Web application* client instead of a Desktop app client. Both fail later with
    unhelpful errors, so name the problem here.
    """
    import json

    if not credentials_present():
        return {"ok": False, "problem": "No file at {}".format(CLIENT_SECRET_PATH)}
    try:
        with open(CLIENT_SECRET_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        return {"ok": False, "problem": "Not valid JSON ({})".format(e)}

    if data.get("type") == "service_account":
        return {
            "ok": False,
            "problem": "This is a service-account key, not an OAuth client. A service "
                       "account has its own empty Drive, not yours. Create an OAuth "
                       "client ID of type 'Desktop app' instead.",
        }
    if "web" in data:
        return {
            "ok": False,
            "problem": "This is a 'Web application' client. The desktop consent flow "
                       "needs an OAuth client of type 'Desktop app'.",
        }
    if "installed" not in data:
        return {
            "ok": False,
            "problem": "Unrecognised client file: expected an 'installed' section "
                       "(Desktop app client).",
        }

    client_id = data["installed"].get("client_id", "")
    return {"ok": True, "client_id": client_id, "project": data["installed"].get("project_id")}


def token_present() -> bool:
    return os.path.exists(TOKEN_PATH)


def get_credentials(interactive: bool = True):
    """Load cached credentials, refreshing or running the consent flow as needed."""
    _require_libs()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except ValueError:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    if not interactive:
        raise DriveError("No usable Drive token; run a push from a terminal to consent.")

    if not credentials_present():
        raise DriveError(
            "Missing OAuth client secret.\n\n" + SETUP_HELP
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def build_service(interactive: bool = True):
    _require_libs()
    from googleapiclient.discovery import build

    creds = get_credentials(interactive=interactive)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class DriveMirror:
    """Mirrors a local directory tree into Drive under a single root folder."""

    def __init__(self, service, root_folder: str = DEFAULT_ROOT_FOLDER):
        self.service = service
        self.root_folder = root_folder
        # (parent_id, name) -> folder_id. Folder lookups dominate the request count
        # without this.
        self._folders: Dict[Tuple[str, str], str] = {}
        self._root_id: Optional[str] = None

    # -- folders ----------------------------------------------------------------

    def _find_child(self, parent_id: str, name: str, folder: bool) -> Optional[str]:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = "name = '{}' and '{}' in parents and trashed = false".format(safe, parent_id)
        q += " and mimeType {} '{}'".format("=" if folder else "!=", FOLDER_MIME)
        resp = (
            self.service.files()
            .list(q=q, fields="files(id, name, size)", pageSize=10,
                  supportsAllDrives=True, includeItemsFromAllDrives=True)
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, parent_id: str, name: str) -> str:
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = (
            self.service.files()
            .create(body=meta, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def ensure_folder(self, parent_id: str, name: str) -> str:
        cached = self._folders.get((parent_id, name))
        if cached:
            return cached
        folder_id = self._find_child(parent_id, name, folder=True)
        if folder_id is None:
            folder_id = self._create_folder(parent_id, name)
        self._folders[(parent_id, name)] = folder_id
        return folder_id

    def root_id(self) -> str:
        if self._root_id is None:
            self._root_id = self.ensure_folder("root", self.root_folder)
        return self._root_id

    def ensure_path(self, parts: List[str]) -> str:
        """Resolve (creating as needed) a chain of folders under the root."""
        parent = self.root_id()
        for part in parts:
            if not part or part in (".", ".."):
                continue
            parent = self.ensure_folder(parent, part)
        return parent

    # -- files ------------------------------------------------------------------

    def upload(
        self,
        local_path: str,
        parent_id: str,
        progress: Optional[Callable[[float], None]] = None,
    ) -> Dict:
        """Upload (or replace) one file and return the resulting Drive metadata."""
        _require_libs()
        from googleapiclient.http import MediaFileUpload

        name = os.path.basename(local_path)
        existing_id = self._find_child(parent_id, name, folder=False)

        # Resumable matters here: lecture videos are routinely hundreds of MB.
        media = MediaFileUpload(local_path, resumable=True, chunksize=8 * 1024 * 1024)

        if existing_id:
            request = self.service.files().update(
                fileId=existing_id, media_body=media, fields="id, name, size",
                supportsAllDrives=True,
            )
        else:
            request = self.service.files().create(
                body={"name": name, "parents": [parent_id]},
                media_body=media,
                fields="id, name, size",
                supportsAllDrives=True,
            )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress:
                progress(status.progress())
        return response


def push_file(
    mirror: DriveMirror,
    entry: Dict,
    download_root: str,
    ledger: Ledger,
    move: bool = True,
    progress: Optional[Callable[[float], None]] = None,
) -> Dict:
    """Upload one planned entry, verify it, then reclaim the local copy.

    The local file is only deleted after Drive echoes back a size matching the bytes on
    disk. A mismatch leaves the file alone and raises.
    """
    local_path = entry["path"]
    if not os.path.isfile(local_path):
        raise DriveError("Not on disk: {}".format(local_path))

    local_size = os.path.getsize(local_path)
    rel_path = os.path.relpath(local_path, download_root)
    parts = os.path.dirname(rel_path).replace("\\", "/").split("/")
    parent_id = mirror.ensure_path([p for p in parts if p])

    result = mirror.upload(local_path, parent_id, progress=progress)

    remote_size = int(result.get("size") or 0)
    # Google Docs-converted files report no size; only enforce when Drive gives one.
    if remote_size and remote_size != local_size:
        raise DriveError(
            "Size mismatch for {}: local {} vs Drive {} - keeping local copy".format(
                rel_path, local_size, remote_size
            )
        )

    ledger.record(
        rel_path,
        drive_id=result["id"],
        remote_modified=entry.get("modified"),
        size=local_size,
        folder_id=parent_id,
    )

    if move:
        os.remove(local_path)
        _prune_empty_dirs(os.path.dirname(local_path), download_root)

    return {
        "rel_path": rel_path,
        "drive_id": result["id"],
        "size": local_size,
        "moved": move,
    }


def _prune_empty_dirs(directory: str, stop_at: str):
    """Walk upward removing directories left empty by the move, never past the root."""
    stop_at = os.path.abspath(stop_at)
    directory = os.path.abspath(directory)
    while directory.startswith(stop_at) and directory != stop_at:
        try:
            if os.listdir(directory):
                return
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)
