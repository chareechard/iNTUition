"""Read Cerberus's flagged-email list, so deadlines land next to the timetable.

Cerberus (the J.A.R.V.I.S email triage) already watches NTU senders - the
scholarships team, SPMS, URECA - and flags mail that needs action. iNTUition
already shows the teaching week and what is on today. Those belong on one screen.

The coupling is deliberately the weakest kind available:

* **Read-only, and enforced.** The database is opened with SQLite's ``mode=ro``
  URI, so a bug here cannot mark something done or delete a row. Cerberus stays
  the only writer of its own state.
* **One direction.** iNTUition imports nothing from Cerberus and vice versa; the
  file path is the entire interface. Cerberus not being installed is the normal
  case, not an error - the panel simply does not appear.
* **No body text.** Subject, sender, priority and the one-line reason are enough
  to decide whether to open the mail. Email bodies are not copied into this
  process, and nothing here reaches the research backend.
"""
import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

# Cerberus lives beside this project by default; both are checked out under one
# Projects directory. Override with INTUITION_CERBERUS_DB.
DEFAULT_RELATIVE = os.path.join(
    "J.A.R.V.I.S", "cerberus", "storage", "flagged.db")

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3,
                  "False Positive": 4}
MAX_ROWS = 12


def resolve_path(download_root: Optional[str] = None,
                 configured: Optional[str] = None) -> str:
    """Which store to read, in order of authority.

    iNTUition's own triage store wins when it exists: once this project is doing the
    triaging, the Cerberus database is the legacy one. An explicit path beats both,
    which is what makes the changeover a flag rather than a migration.
    """
    if configured:
        return configured
    env = (os.environ.get("INTUITION_CERBERUS_DB") or "").strip()
    if env:
        return env
    if download_root:
        from ntu_learn_downloader import triage_store
        own = triage_store.db_path(download_root)
        if os.path.isfile(own):
            return own
    return default_path()


def default_path(projects_dir: Optional[str] = None) -> str:
    """Where to look when nothing is configured."""
    env = (os.environ.get("INTUITION_CERBERUS_DB") or "").strip()
    if env:
        return env
    if projects_dir is None:
        # .../Projects/NTULearn-Downloader/ntu_learn_downloader/inbound.py
        projects_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(projects_dir, DEFAULT_RELATIVE)


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
    """Cerberus stores this as a JSON array; tolerate a plain string too."""
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
        # Cerberus's scraper currently stores an empty subject for most rows, so a
        # row identified only by sender would be unreadable. The matched snippet is
        # the evidence it flagged on and makes a serviceable stand-in.
        "title": subject or _first_sentence(snippet) or "(no subject captured)",
        "priority": get("priority", "Medium"),
        "confidence": _as_float(get("confidence")),
        "actions": _actions(get("action_items")),
        "reason": get("reasoning"),
        "snippet": snippet,
        "flagged_at": get("flagged_at"),
        "link": get("link"),
        # Deliberately absent: body_content.
    }


def open_flags(path: Optional[str] = None, limit: int = MAX_ROWS) -> List[Dict]:
    """Open flags, most urgent first. Any failure yields an empty list.

    Cerberus may be mid-write, the file may be locked, an older schema may lack a
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
    # Urgency beats recency: a Critical from Tuesday outranks a Medium from today.
    flags.sort(key=lambda f: (PRIORITY_ORDER.get(f["priority"], 9),
                              f["flagged_at"] or ""))
    return flags[:limit]


_CACHE: Dict[str, tuple] = {}
CACHE_SECONDS = 10


def snapshot(path: Optional[str] = None, ttl: float = CACHE_SECONDS) -> Dict:
    """What the dashboard renders, including why the panel is empty when it is.

    Cached briefly: the dashboard polls twice a second and this opens a database.
    Triage runs on a 90-second cycle, so a few seconds of staleness is invisible.
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
    flags = open_flags(path) if present else []
    counts: Dict[str, int] = {}
    for f in flags:
        counts[f["priority"]] = counts.get(f["priority"], 0) + 1
    return {
        "available": present,
        "path": path,
        "flags": flags,
        "counts": counts,
        "total": len(flags),
    }
