"""Small persistent work-request queue used by the iNTUition dashboard."""
import json
import os
import threading
import uuid
import re
from datetime import datetime
from typing import Dict, List, Optional

STORAGE_DIR = ".intuition"
FILENAME = "todo.json"
DIRECTIONS = ("push", "pull")
STATUSES = ("open", "in_progress", "done")
PRIORITIES = ("low", "normal", "high")


def course_code(value: str) -> str:
    """Return the first real NTU course code in a display name."""
    matches = re.findall(r"\b[A-Z]{2,4}\d{4}\b", str(value or "").upper())
    return next((code for code in matches if not code.startswith("AY")), "")


def queue_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Queue:
    def __init__(self, download_root: str):
        self.path = queue_path(download_root)
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

    def add(self, title: str, direction: str = "push", priority: str = "normal",
            details: str = "", course: str = "") -> Dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("A work request needs a title")
        if direction not in DIRECTIONS:
            raise ValueError("Direction must be push or pull")
        if priority not in PRIORITIES:
            raise ValueError("Unknown priority")
        item = {"id": uuid.uuid4().hex[:12], "title": title[:200],
                "details": (details or "").strip(), "direction": direction,
                "course": course_code(course),
                "priority": priority, "status": "open", "created": _now(),
                "updated": _now()}
        with self._lock:
            self.items.insert(0, item)
        return item

    def get(self, item_id: str) -> Optional[Dict]:
        return next((item for item in self.items if item.get("id") == item_id), None)

    def update(self, item_id: str, **fields) -> Optional[Dict]:
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return None
            for key in ("title", "details", "direction", "priority", "status", "course"):
                if fields.get(key) is None:
                    continue
                value = str(fields[key]).strip()
                if key == "direction" and value not in DIRECTIONS:
                    raise ValueError("Direction must be push or pull")
                if key == "priority" and value not in PRIORITIES:
                    raise ValueError("Unknown priority")
                if key == "status" and value not in STATUSES:
                    raise ValueError("Unknown status")
                if key == "title" and not value:
                    raise ValueError("A work request needs a title")
                if key == "course":
                    value = course_code(value)
                item[key] = value[:200] if key == "title" else value
            item["updated"] = _now()
            return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [item for item in self.items if item.get("id") != item_id]
            return len(self.items) != before

    def sync_ntulearn(self, incoming: List[Dict]) -> int:
        """Replace iNTUition-sourced queries while leaving manual queries untouched."""
        imported = []
        now = _now()
        existing = {str(item.get("source_id")): item for item in self.items
                    if item.get("source") == "ntulearn"}
        for source in incoming:
            source_id = str(source.get("source_id") or "").strip()
            title = str(source.get("title") or "").strip()
            if not source_id or not title:
                continue
            details = " · ".join(part for part in (
                str(source.get("course") or "").strip(),
                ("Due " + str(source.get("due") or "").strip()) if source.get("due") else "",
                str(source.get("kind") or "").strip(),
            ) if part)
            new_item = {
                "id": "ntu-" + source_id.strip("_")[:40],
                "source": "ntulearn", "source_id": source_id,
                "title": title[:200], "details": details,
                "course": course_code(source.get("course")),
                "direction": "pull", "priority": "high",
                "status": "open", "created": now, "updated": now,
            }
            old = existing.get(source_id) or {}
            for key in ("research", "created"):
                if old.get(key):
                    new_item[key] = old[key]
            imported.append(new_item)
        with self._lock:
            manual = [item for item in self.items if item.get("source") != "ntulearn"]
            self.items = imported + manual
        return len(imported)

    def set_research(self, item_id: str, finding: Optional[Dict]) -> Optional[Dict]:
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return None
            item["research"] = finding
            item["updated"] = _now()
            return item

    def snapshot(self) -> Dict:
        status_order = {"in_progress": 0, "open": 1, "done": 2}
        priority_order = {"high": 0, "normal": 1, "low": 2}
        items = sorted(self.items, key=lambda i: i.get("updated", ""), reverse=True)
        items.sort(key=lambda i: (status_order.get(i.get("status"), 9),
                                  priority_order.get(i.get("priority"), 9)))
        counts = {status: sum(i.get("status") == status for i in self.items)
                  for status in STATUSES}
        return {"items": items, "counts": counts}

    def __len__(self):
        return len(self.items)
