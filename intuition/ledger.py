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
from typing import Dict, Optional, Tuple

STORAGE_DIR = ".intuition"
LEDGER_FILENAME = "drive_ledger.json"


def ledger_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, LEDGER_FILENAME)


class Ledger:
    """Maps a download-root-relative path to its Drive archival record.

    Record shape::

        {"drive_id": str, "remote_modified": str|None, "size": int,
         "uploaded_at": iso8601, "folder_id": str, "source_id": str}

    ``source_id`` is the Blackboard resource identity (see ``sync.source_id``). It is
    the durable key: paths change whenever a lecturer reorganises a course, and a
    path-only ledger reports every moved file as new and re-downloads it. Records
    written before this existed carry no ``source_id`` and are matched by path until
    a scan backfills one.
    """

    def __init__(self, download_root: str):
        self.download_root = download_root
        self.path = ledger_path(download_root)
        self._lock = threading.Lock()
        self.entries: Dict[str, Dict] = {}
        self._by_source: Dict[str, str] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.entries = {}
            self._reindex()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Tolerate an older/corrupt file rather than losing the whole run.
            self.entries = data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            self.entries = {}
        self._reindex()

    def _reindex(self):
        """Rebuild the resource-id index. Callers hold the lock, or are in load()."""
        self._by_source = {}
        for key, entry in self.entries.items():
            source = (entry or {}).get("source_id")
            if source:
                self._by_source[source] = key

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

    def find_by_source(self, source_id: str) -> Optional[Tuple[str, Dict]]:
        """Locate a record by Blackboard resource id, returning ``(key, entry)``.

        This is what lets a file survive being moved to a different folder in the
        course: the path changed, the resource did not.
        """
        if not source_id:
            return None
        with self._lock:
            key = self._by_source.get(source_id)
            if key is None:
                return None
            entry = self.entries.get(key)
            return (key, entry) if entry is not None else None

    def attach_source(self, rel_path: str, source_id: str) -> bool:
        """Backfill the resource id on a record that predates it."""
        if not source_id:
            return False
        with self._lock:
            entry = self.entries.get(self.key(rel_path))
            if entry is None or entry.get("source_id") == source_id:
                return False
            entry["source_id"] = source_id
            self._by_source[source_id] = self.key(rel_path)
            return True

    def record(
        self,
        rel_path: str,
        drive_id: str,
        remote_modified: Optional[str],
        size: int,
        folder_id: Optional[str] = None,
        source_id: str = "",
    ):
        with self._lock:
            key = self.key(rel_path)
            self.entries[key] = {
                "drive_id": drive_id,
                "remote_modified": remote_modified,
                "size": size,
                "folder_id": folder_id,
                "source_id": source_id,
                "uploaded_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            if source_id:
                self._by_source[source_id] = key

    def migrate(self, old_rel_path: str, new_rel_path: str) -> bool:
        """Move a record onto a new key, keeping the archival evidence intact.

        Ledgers written before paths became root-independent are keyed on the on-disk
        path. Re-keying them in place is what stops a scan from mistaking an archived
        file for a new one and downloading it all over again. An existing record under
        the new key wins, since it is already in the current scheme.
        """
        with self._lock:
            old_key, new_key = self.key(old_rel_path), self.key(new_rel_path)
            if old_key == new_key or old_key not in self.entries:
                return False
            entry = self.entries.pop(old_key)
            self.entries.setdefault(new_key, entry)
            self._reindex()
            return True

    def forget(self, rel_path: str):
        with self._lock:
            self.entries.pop(self.key(rel_path), None)
            self._reindex()

    def __len__(self) -> int:
        return len(self.entries)
