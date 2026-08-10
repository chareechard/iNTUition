"""R&D board: one place to work out what to build for the courses you are taking.

A single board spanning every course rather than one per course - most ideas either
apply to several modules at once or are not really about a module at all, and a board
per course fragments that.

Each entry is a tool you are considering building, with a course tag, a status, and
free-form notes and links. Persisted beside the ledger so it travels with the download
folder and survives a semester rollover - research outlives the term it started in.
"""
import json
import os
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

STORAGE_DIR = ".ntu_learn_downloader"
RND_FILENAME = "rnd.json"

# Deliberately few. More states invite bookkeeping rather than building.
IDEA = "idea"
RESEARCHING = "researching"
PROTOTYPING = "prototyping"
BUILT = "built"
PARKED = "parked"
STATUSES = (IDEA, RESEARCHING, PROTOTYPING, BUILT, PARKED)

# Clicking the status chip walks this cycle; parked is only reachable explicitly so a
# stray click cannot bury an item.
CYCLE = (IDEA, RESEARCHING, PROTOTYPING, BUILT)


def board_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, RND_FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Board:
    def __init__(self, download_root: str):
        self.path = board_path(download_root)
        self._lock = threading.Lock()
        self.items: List[Dict] = []
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self):
        if not os.path.exists(self.path):
            self.items = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", []) if isinstance(data, dict) else []
        except (ValueError, OSError):
            self.items = []

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items}, f, indent=1)
        os.replace(tmp, self.path)

    # ── mutations ────────────────────────────────────────────────────────────

    def add(self, title: str, course: str = "", notes: str = "",
            link: str = "", status: str = IDEA) -> Dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("An entry needs a title")
        if status not in STATUSES:
            status = IDEA
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": title[:200],
            "course": (course or "").strip().upper(),
            "notes": (notes or "").strip(),
            "link": (link or "").strip(),
            "status": status,
            # Filled in by ntu_learn_downloader.research when you ask for it, never
            # before: {"text", "model", "sources", "at", "tokens"}.
            "research": None,
            "created": _now(),
            "updated": _now(),
        }
        with self._lock:
            self.items.insert(0, item)
        return item

    def get(self, item_id: str) -> Optional[Dict]:
        return next((i for i in self.items if i["id"] == item_id), None)

    def update(self, item_id: str, **fields) -> Optional[Dict]:
        with self._lock:
            item = next((i for i in self.items if i["id"] == item_id), None)
            if item is None:
                return None
            for key in ("title", "course", "notes", "link", "status"):
                if key not in fields or fields[key] is None:
                    continue
                value = fields[key]
                if key == "status" and value not in STATUSES:
                    continue
                if key == "course":
                    value = str(value).strip().upper()
                item[key] = str(value).strip() if key != "status" else value
            item["updated"] = _now()
            return item

    def set_research(self, item_id: str, finding: Optional[Dict]) -> Optional[Dict]:
        """Attach (or clear, with None) a Claude finding for one entry.

        Kept off ``update`` deliberately: the finding is a structured record written by
        the backend, not a field the form edits, and it must not be settable from the
        same path that accepts free text.
        """
        with self._lock:
            item = next((i for i in self.items if i["id"] == item_id), None)
            if item is None:
                return None
            item["research"] = finding
            item["updated"] = _now()
            return item

    def advance(self, item_id: str) -> Optional[Dict]:
        """Move an item one step along idea -> researching -> prototyping -> built."""
        item = self.get(item_id)
        if item is None:
            return None
        current = item.get("status", IDEA)
        if current == PARKED:
            nxt = IDEA
        else:
            try:
                nxt = CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)]
            except ValueError:
                nxt = IDEA
        return self.update(item_id, status=nxt)

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != item_id]
            return len(self.items) != before

    # ── views ────────────────────────────────────────────────────────────────

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for i in self.items:
            out[i.get("status", IDEA)] = out.get(i.get("status", IDEA), 0) + 1
        return out

    def courses(self) -> List[str]:
        return sorted({i["course"] for i in self.items if i.get("course")})

    def snapshot(self) -> Dict:
        # Active work first, then ideas, with parked last; newest first inside a group.
        order = {PROTOTYPING: 0, RESEARCHING: 1, IDEA: 2, BUILT: 3, PARKED: 4}
        # Two passes rather than one composite key: timestamps sort descending and
        # status ascending, and Python's stable sort composes them correctly.
        items = sorted(self.items, key=lambda i: i.get("updated", ""), reverse=True)
        items.sort(key=lambda i: order.get(i.get("status", IDEA), 9))
        return {"items": items, "counts": self.counts(),
                "statuses": list(STATUSES), "courses": self.courses()}

    def __len__(self) -> int:
        return len(self.items)
