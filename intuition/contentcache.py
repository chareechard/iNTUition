"""Remembers what each Blackboard content item looked like last scan.

Why
---
A scan has two costs: listing the content tree, and asking each document for its
attachments. The listing collapses to a single ``?recursive=true`` request per course.
The attachment lookups do not - there is one per document, and they dominate.

But an attachment list only changes when its content item changes, and every item in the
recursive listing carries a ``modified`` stamp. So: cache the attachments against the
stamp they were fetched under, and re-request only when the stamp moves. A course where
the professor has changed nothing then costs exactly one request.

Stored beside the ledger so it travels with the download folder. Losing it is harmless -
the next scan just pays full price once and rebuilds it.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

STORAGE_DIR = ".intuition"
CACHE_FILENAME = "content_cache.json"
# An empty attachment list is weak evidence: Blackboard can publish a file after the
# parent document's modified stamp has settled (seen in SC2002). Positive results stay
# stamp-cached indefinitely; negative results get checked again on later scans.
NEGATIVE_TTL = timedelta(minutes=10)


def cache_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, CACHE_FILENAME)


class ContentCache:
    """course_id -> content_id -> {"modified": str, "attachments": [{id, fileName}]}"""

    def __init__(self, download_root: str):
        self.path = cache_path(download_root)
        self._lock = threading.Lock()
        self.courses: Dict[str, Dict[str, Dict]] = {}
        self.hits = 0
        self.misses = 0
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.courses = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.courses = data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            self.courses = {}

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.courses, f, sort_keys=True)
        os.replace(tmp, self.path)

    def get_attachments(
        self, course_id: str, content_id: str, modified: Optional[str]
    ) -> Optional[List[Dict]]:
        """Cached attachments, or None if this item must be re-fetched.

        A missing ``modified`` on either side means we cannot prove the item is
        unchanged, so we deliberately miss rather than risk serving a stale list.
        """
        with self._lock:
            entry = self.courses.get(course_id, {}).get(content_id)
            if entry is None or not modified or entry.get("modified") != modified:
                self.misses += 1
                return None
            attachments = entry.get("attachments", [])
            if not attachments:
                try:
                    checked = datetime.fromisoformat(entry["checked_at"])
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                except (KeyError, TypeError, ValueError):
                    # Old cache rows did not record when an empty result was observed;
                    # treating them as stale repairs installs affected by this bug.
                    self.misses += 1
                    return None
                if datetime.now(timezone.utc) - checked > NEGATIVE_TTL:
                    self.misses += 1
                    return None
            self.hits += 1
            return attachments

    def put_attachments(
        self,
        course_id: str,
        content_id: str,
        modified: Optional[str],
        attachments: List[Dict],
    ):
        if not modified:
            # Nothing to invalidate against; do not cache it.
            return
        with self._lock:
            self.courses.setdefault(course_id, {})[content_id] = {
                "modified": modified,
                "attachments": attachments,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

    def prune(self, course_id: str, live_ids):
        """Drop items the professor has deleted from the course."""
        with self._lock:
            items = self.courses.get(course_id)
            if not items:
                return
            for gone in set(items) - set(live_ids):
                del items[gone]

    def stats(self) -> Dict[str, int]:
        return {"hits": self.hits, "misses": self.misses}

    def reset_stats(self):
        self.hits = self.misses = 0
