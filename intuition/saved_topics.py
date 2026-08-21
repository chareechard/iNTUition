"""Durable store for topics the student has starred out of AI-suggested
research ideas.

The Research tab's "Suggest topics" batch is disposable by design - a re-run
replaces the whole thing, and nothing about an unstarred suggestion survives
past its own batch (see dashboard.py's /api/research/suggest). Starring one
here is the escape hatch: it survives a re-run and a reload, until the
student unstars it or adopts it into a real proposal (at which point
dashboard.py removes it here too - it has graduated from an idea to a
project, so keeping the idea around alongside the proposal would just be
clutter).
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

STORAGE_DIR = ".intuition"
FILENAME = "saved_topics.json"
MAX_SAVED = 30  # a shortlist to revisit, not an ever-growing archive


def store_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class Store:
    def __init__(self, download_root: str):
        self.path = store_path(download_root)
        self._lock = threading.Lock()
        self.items: List[Dict] = []
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            self.items = []

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items}, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, title: str, topic: str) -> Dict:
        title = _text(title, 200)
        topic = _text(topic, 600)
        if not title or not topic:
            raise ValueError("A saved topic needs both a title and a one-line idea")
        with self._lock:
            # Starring the same idea twice just surfaces the existing entry -
            # a list meant to stay a short, revisitable shortlist shouldn't
            # accumulate duplicates of the same title.
            existing = next((i for i in self.items if i.get("title") == title), None)
            if existing:
                return existing
            item = {"id": uuid.uuid4().hex[:12], "title": title, "topic": topic,
                    "starred": _now()}
            self.items.insert(0, item)
            # The oldest entries are the ones a student is least likely to
            # still care about - drop them rather than let the list grow
            # without bound.
            del self.items[MAX_SAVED:]
            return item

    def get(self, item_id: str) -> Optional[Dict]:
        return next((i for i in self.items if i.get("id") == item_id), None)

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i.get("id") != item_id]
            return len(self.items) != before

    def snapshot(self) -> Dict:
        return {"items": self.items}
