"""Persistent record of what has been pushed to Drive.

Why this exists
---------------
The pipeline moves files: once a download is safely in Drive the local copy is deleted.
That reclaims disk, but it also destroys the thing the sync diff was reading. Without a
ledger, ``sync.classify`` would see an absent file, call it ``new``, and re-download
everything on every single scan - forever.

The ledger is the surviving memory of a file after its bytes are gone. It records, per
relative path, the Blackboard ``modified`` stamp that was current when the file was
uploaded, so a later scan can still answer "has this changed since I archived it?".

Stored as JSON next to the download root so it travels with the folder it describes.
"""
import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

STORAGE_DIR = ".ntu_learn_downloader"
LEDGER_FILENAME = "drive_ledger.json"


def ledger_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, LEDGER_FILENAME)


class Ledger:
    """Maps a download-root-relative path to its Drive archival record.

    Record shape::

        {"drive_id": str, "remote_modified": str|None, "size": int,
         "uploaded_at": iso8601, "folder_id": str}
    """

    def __init__(self, download_root: str):
        self.download_root = download_root
        self.path = ledger_path(download_root)
        self._lock = threading.Lock()
        self.entries: Dict[str, Dict] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.entries = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Tolerate an older/corrupt file rather than losing the whole run.
            self.entries = data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            self.entries = {}

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=1, sort_keys=True)
        # Atomic-ish replace so an interrupted write cannot truncate the ledger.
        os.replace(tmp, self.path)

    @staticmethod
    def key(rel_path: str) -> str:
        """Normalise separators so a ledger written on Windows reads on POSIX."""
        return rel_path.replace("\\", "/")

    def get(self, rel_path: str) -> Optional[Dict]:
        with self._lock:
            return self.entries.get(self.key(rel_path))

    def record(
        self,
        rel_path: str,
        drive_id: str,
        remote_modified: Optional[str],
        size: int,
        folder_id: Optional[str] = None,
    ):
        with self._lock:
            self.entries[self.key(rel_path)] = {
                "drive_id": drive_id,
                "remote_modified": remote_modified,
                "size": size,
                "folder_id": folder_id,
                "uploaded_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }

    def forget(self, rel_path: str):
        with self._lock:
            self.entries.pop(self.key(rel_path), None)

    def __len__(self) -> int:
        return len(self.entries)
