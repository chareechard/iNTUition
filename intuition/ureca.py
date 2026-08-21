"""Local store and AI drafting helper for URECA project proposals.

URECA (Undergraduate Research Experience on CAmpus) is NTU's self-proposed
undergraduate research programme: an eligible student drafts a project
proposal, a faculty supervisor accepts and registers it (by the fixed
30 September deadline), and the student spends the August-to-June academic
year on the project, finishing with a research-integrity course, an
abstract, a poster, a final paper and a reflection. This module only drafts
and tracks a proposal locally - actually sending it to a supervisor still
happens through NTU's own URECA system, which this app has no access to.
"""
import json
import os
import re
import threading
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional

STORAGE_DIR = ".intuition"
FILENAME = "ureca.json"
CATEGORIES = ("URECA", "IDR-URECA", "CLRI-URECA")
STATUSES = ("drafting", "sent", "registered", "completed")
DELIVERABLES = ("integrity", "abstract", "poster", "paper", "reflection")
DELIVERABLE_LABELS = {
    "integrity": "Research integrity course",
    "abstract": "Abstract",
    "poster": "Poster",
    "paper": "Final paper",
    "reflection": "Reflection",
}
# Free-text fields the store accepts verbatim (title and category are
# validated separately - a title is required, a category is a closed set).
TEXT_FIELDS = ("supervisor", "school", "background", "objectives",
               "methodology", "outcomes", "budgetNotes", "timelineNotes")


def store_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def next_registration_deadline(today: Optional[date] = None) -> str:
    """URECA registers eligible proposals by 30 September every year."""
    today = today or date.today()
    cutoff = date(today.year, 9, 30)
    year = today.year if today <= cutoff else today.year + 1
    return date(year, 9, 30).isoformat()


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

    def add(self, title: str) -> Dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("A proposal needs a title")
        item = {
            "id": uuid.uuid4().hex[:12], "title": title[:200], "category": "URECA",
            "status": "drafting", "created": _now(), "updated": _now(),
            "deliverables": {k: False for k in DELIVERABLES},
        }
        for key in TEXT_FIELDS:
            item[key] = ""
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
            if fields.get("title") is not None:
                title = str(fields["title"]).strip()
                if not title:
                    raise ValueError("A proposal needs a title")
                item["title"] = title[:200]
            if fields.get("category") is not None:
                if fields["category"] not in CATEGORIES:
                    raise ValueError("Unknown category")
                item["category"] = fields["category"]
            if fields.get("status") is not None:
                if fields["status"] not in STATUSES:
                    raise ValueError("Unknown status")
                item["status"] = fields["status"]
            for key in TEXT_FIELDS:
                if fields.get(key) is not None:
                    item[key] = str(fields[key])[:4000]
            if isinstance(fields.get("deliverables"), dict):
                for key in DELIVERABLES:
                    if key in fields["deliverables"]:
                        item["deliverables"][key] = bool(fields["deliverables"][key])
            item["updated"] = _now()
            return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [item for item in self.items if item.get("id") != item_id]
            return len(self.items) != before

    def snapshot(self) -> Dict:
        return {"items": self.items, "deadline": next_registration_deadline()}


# ── AI drafting: same defensive-parsing posture as graphing.py/lab_analysis.py -
# the model's JSON is untrusted output, not a value to render as-is. ──────────
def _extract_json(raw: str) -> Optional[Dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def _text(value: object, limit: int = 1500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


DRAFT_FIELDS = ("background", "objectives", "methodology", "outcomes",
                "budgetNotes", "timelineNotes")


def parse_draft_response(raw_text: str) -> Dict:
    """Never raises - a response that fails to parse just drafts empty
    fields, which the caller only ever fills in alongside what's already
    there, never overwrites."""
    data = _extract_json(raw_text) or {}
    return {key: _text(data.get(key)) for key in DRAFT_FIELDS}


MAX_SUGGESTIONS = 5


def parse_suggest_response(raw_text: str) -> List[Dict]:
    """Parse a list of {"title", "topic"} candidate ideas.

    Never raises - an unparseable or malformed response just yields no
    suggestions, the same defensive posture as ``parse_draft_response``. The
    student always picks from this list explicitly; nothing here is created
    or saved on its own.

    A response cut short by the token budget mid-array is common enough (a
    model runs long on one idea and never reaches the closing bracket) that
    it is worth salvaging: whatever complete {"title", "topic"} objects
    already came through are still usable, so a truncated reply degrades to
    fewer suggestions rather than none.
    """
    data = _extract_json_list(raw_text)
    if not data:
        data = _salvage_json_objects(raw_text)
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = _text(entry.get("title"), limit=200)
        topic = _text(entry.get("topic"), limit=600)
        if title and topic:
            out.append({"title": title, "topic": topic})
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def _extract_json_list(raw: str) -> List:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except ValueError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, list) else []
        except ValueError:
            return []
    return []


def _salvage_json_objects(raw: str) -> List:
    """Parse each complete top-level {...} object out of a truncated array.

    Scans for balanced-brace objects starting after the array's opening
    "[", respecting quoted strings and escapes so a brace inside a title or
    topic string does not throw off the depth count. An object left
    half-written by truncation simply never closes and is dropped.
    """
    text = (raw or "").strip()
    start = text.find("[")
    if start == -1:
        return []
    out = []
    depth = 0
    obj_start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text[start + 1:], start + 1):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    out.append(json.loads(text[obj_start:i + 1]))
                except ValueError:
                    pass
                obj_start = None
            elif depth < 0:
                break
    return out
