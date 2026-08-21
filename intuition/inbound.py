"""Render the flagged-email list, so deadlines land next to the timetable.

``triage`` watches NTU senders - the scholarships team, SPMS, URECA - and flags
mail that needs action. iNTUition already shows the teaching week and what is on
today. Those belong on one screen.

This is the read half, and it stays deliberately narrow:

* **Read-only, and enforced.** The database is opened with SQLite's ``mode=ro``
  URI, so a bug on a dashboard poll cannot mark something done or delete a row.
  ``triage_store`` stays the only writer.
* **One direction.** The file path is the entire interface, so any store in this
  shape can be pointed at with ``--inbound_db``. No store yet is the normal case,
  not an error - the panel simply does not appear until the first scan.
* **No body text.** Subject, sender, priority and the one-line reason are enough
  to decide whether to open the mail. Email bodies stay in the triage process
  that read them, and nothing here reaches the research backend.
"""
import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

ENV_VAR = "INTUITION_INBOUND_DB"

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3,
                  "False Positive": 4}
MAX_ROWS = 12


def resolve_path(download_root: Optional[str] = None,
                 configured: Optional[str] = None) -> str:
    """Which store to read, in order of authority.

    An explicit path beats the environment, which beats this project's own store.
    That ordering is what lets another store in the same shape be pointed at
    without a migration.
    """
    if configured:
        return configured
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        return env
    return default_path(download_root)


def default_path(download_root: Optional[str] = None) -> str:
    """This project's own triage store, beneath the sync folder."""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        return env
    from intuition import triage_store
    return triage_store.db_path(download_root or ".")


def available(path: Optional[str] = None) -> bool:
    return os.path.isfile(path or default_path())


def _as_float(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _first_sentence(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    cut = re.split(r"(?<=[.!?])\s", text)[0]
    return cut[:limit].rstrip() + ("..." if len(cut) > limit else "")


def _actions(value) -> List[str]:
    """Stored newline-joined, but tolerate a JSON array or a bare string too."""
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(a).strip() for a in parsed if str(a).strip()]
        except ValueError:
            pass
    return [a.strip() for a in text.splitlines() if a.strip()]


def _row_to_flag(row: sqlite3.Row) -> Dict:
    keys = row.keys()

    def get(name, default=""):
        return (row[name] if name in keys and row[name] is not None else default)

    subject = str(get("subject")).strip()
    snippet = str(get("snippet") if "snippet" in keys else get("matched_snippet")).strip()
    return {
        "id": get("email_id"),
        "sender": get("sender"),
        "subject": subject,
        # OWA's reading pane does not always yield a subject, so a row identified
        # only by sender would be unreadable. The matched snippet is the evidence
        # it was flagged on and makes a serviceable stand-in.
        "title": subject or _first_sentence(snippet) or "(no subject captured)",
        "priority": get("priority", "Medium"),
        "confidence": _as_float(get("confidence")),
        "actions": _actions(get("action_items")),
        "reason": get("reasoning"),
        "snippet": snippet,
        "flagged_at": get("flagged_at"),
        "due": get("due"),
        "link": get("link"),
        # Deliberately absent: body_content.
    }


def open_flags(path: Optional[str] = None, limit: int = MAX_ROWS) -> List[Dict]:
    """Open flags, most urgent first. Any failure yields an empty list.

    A scan may be mid-write, the file may be locked, an older schema may lack a
    column - none of that is worth failing a dashboard poll over.
    """
    path = path or default_path()
    if not os.path.isfile(path):
        return []
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path.replace("\\", "/")),
                               uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM flagged_emails WHERE status = 'open' "
                "ORDER BY flagged_at DESC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    flags = [_row_to_flag(r) for r in rows]
    # Stable passes compose unlike a single ascending tuple: urgency first, then
    # newest-first within each priority band.
    flags.sort(key=lambda f: f["flagged_at"] or "", reverse=True)
    flags.sort(key=lambda f: PRIORITY_ORDER.get(f["priority"], 9))
    return flags[:limit]


def _scan_meta(path: str) -> Dict:
    """Latest scan telemetry; older compatible databases simply return none."""
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path.replace("\\", "/")),
                               uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT value FROM triage_meta WHERE key='last_scan'").fetchone()
        finally:
            conn.close()
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}


def _group(flags: List[Dict]) -> List[Dict]:
    """Collapse repeated opportunity emails in the view without deleting evidence."""
    grouped: List[Dict] = []
    by_key: Dict[tuple, Dict] = {}
    for flag in flags:
        label = flag.get("subject") or flag.get("title") or ""
        label = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        sender = re.sub(r"\s+", " ", str(flag.get("sender") or "").lower()).strip()
        key = (label, sender, flag.get("due") or "") if label else (flag["id"], "", "")
        existing = by_key.get(key)
        if existing is None:
            item = dict(flag, ids=[flag["id"]], duplicate_count=1)
            by_key[key] = item
            grouped.append(item)
        else:
            existing["ids"].append(flag["id"])
            existing["duplicate_count"] += 1
    return grouped


_CACHE: Dict[str, tuple] = {}
CACHE_SECONDS = 10


def snapshot(path: Optional[str] = None, ttl: float = CACHE_SECONDS) -> Dict:
    """What the dashboard renders, including why the panel is empty when it is.

    Cached briefly: the dashboard polls twice a second and this opens a database.
    Flags change only when a scan runs, so a few seconds of staleness is invisible.
    """
    path = path or default_path()
    hit = _CACHE.get(path)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    snap = _build(path)
    _CACHE[path] = (now, snap)
    return snap


def _build(path: str) -> Dict:
    present = os.path.isfile(path)
    # Grouped and counted before the row cap - "total" describes how many open
    # flags actually exist, not just how many fit in the panel. Without this
    # split, a busy inbox silently loses everything past MAX_ROWS with no sign
    # anything is missing.
    grouped = _group(open_flags(path, limit=1000)) if present else []
    flags = grouped[:MAX_ROWS]
    counts: Dict[str, int] = {}
    for f in grouped:
        counts[f["priority"]] = counts.get(f["priority"], 0) + 1
    return {
        "available": present,
        "path": path,
        "flags": flags,
        "counts": counts,
        "total": len(grouped),
        "shown": len(flags),
        "scan": _scan_meta(path) if present else {},
    }
