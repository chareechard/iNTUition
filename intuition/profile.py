"""The student's own research profile - who they are, for the Research tab's
autonomous topic suggestions to write for.

A single record, not a list: one student, one standing profile. Feeds
``ureca.RESEARCH_SUGGEST_SYSTEM``-style prompts so suggested URECA topics are
shaped for this student's programme and year rather than a generic
undergraduate.
"""
import json
import os
import threading
from datetime import datetime
from typing import Dict, Optional

STORAGE_DIR = ".intuition"
FILENAME = "profile.json"

DEFAULTS = {
    "programme": "NTU MACS",
    "year": "Y2",
    "field": "Mathematical and Computer Sciences",
    "keywords": "",
}
FIELDS = tuple(DEFAULTS)


def store_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, download_root: str):
        self.path = store_path(download_root)
        self._lock = threading.Lock()
        self.data: Dict = dict(DEFAULTS)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            for key in FIELDS:
                if str(saved.get(key) or "").strip():
                    self.data[key] = str(saved[key]).strip()

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self) -> Dict:
        with self._lock:
            return dict(self.data)

    def update(self, **fields) -> Dict:
        with self._lock:
            for key in FIELDS:
                value = fields.get(key)
                if value is None:
                    continue
                value = str(value).strip()[:(200 if key == "keywords" else 120)]
                self.data[key] = value or DEFAULTS[key]
            return dict(self.data)

    def summary(self) -> str:
        """One line for prompts, e.g. 'NTU MACS, Year 2, Mathematical and Computer Sciences'."""
        d = self.get()
        year = d["year"]
        year_text = "Year " + year[1:] if year[:1].upper() == "Y" and year[1:].isdigit() else year
        return "{}, {}, {}".format(d["programme"], year_text, d["field"])
