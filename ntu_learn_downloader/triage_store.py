"""The flag store iNTUition writes, in the shape ``inbound`` already reads.

Same table as the Cerberus prototype's ``flagged_emails``, deliberately: the reader
then works against either database, and a Cerberus store can be pointed at directly
during the changeover without a migration.

Self-cleaning on write, as the prototype was - a flag is a working list entry, not an
archive, and nobody wants a cron job to keep a to-do list from growing forever.
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

STORAGE_DIR = ".ntu_learn_downloader"
DB_FILENAME = "triage.db"

OPEN_TTL_DAYS = 60      # a flag nobody actioned in two months is not going to be
DONE_TTL_DAYS = 7       # a ticked-off flag is kept only long enough to undo a mistake

COLUMNS = ("email_id", "sender", "subject", "priority", "reasoning", "action_items",
           "flagged_at", "status", "actioned_at", "confidence", "matched_snippet",
           "link", "due")


def db_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, DB_FILENAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TriageStore:
    def __init__(self, download_root: str):
        self.path = db_path(download_root)
        self._lock = threading.Lock()
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS flagged_emails ("
                "email_id TEXT PRIMARY KEY, " +
                ", ".join("{} TEXT".format(c) for c in COLUMNS[1:]) + ")")
            conn.commit()
        finally:
            conn.close()

    def record(self, flag: Dict) -> None:
        """Insert, or refresh an existing open flag. Re-triaging is not duplicating."""
        actions = flag.get("action_items") or []
        row = {
            "email_id": flag.get("email_id", ""),
            "sender": flag.get("sender", ""),
            "subject": flag.get("subject", ""),
            "priority": flag.get("priority", "Medium"),
            "reasoning": flag.get("reasoning", ""),
            "action_items": "\n".join(actions) if isinstance(actions, list) else str(actions),
            "flagged_at": flag.get("flagged_at") or _now(),
            "status": "open",
            "actioned_at": None,
            "confidence": str(flag.get("confidence", "")),
            "matched_snippet": flag.get("matched_snippet", ""),
            "link": flag.get("link", ""),
            "due": flag.get("due", ""),
        }
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO flagged_emails ({}) VALUES ({}) "
                    "ON CONFLICT(email_id) DO UPDATE SET "
                    "priority=excluded.priority, reasoning=excluded.reasoning, "
                    "action_items=excluded.action_items, due=excluded.due, "
                    "confidence=excluded.confidence".format(
                        ", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))),
                    [row[c] for c in COLUMNS])
                conn.commit()
                self._cleanup(conn)
            finally:
                conn.close()

    def _cleanup(self, conn: sqlite3.Connection) -> int:
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=OPEN_TTL_DAYS)).isoformat()
        done = (now - timedelta(days=DONE_TTL_DAYS)).isoformat()
        cur = conn.execute("DELETE FROM flagged_emails WHERE flagged_at < ?", (old,))
        removed = cur.rowcount or 0
        cur = conn.execute(
            "DELETE FROM flagged_emails WHERE status = 'done' AND actioned_at < ?",
            (done,))
        removed += cur.rowcount or 0
        conn.commit()
        return removed

    def mark_done(self, email_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE flagged_emails SET status='done', actioned_at=? "
                    "WHERE email_id = ? AND status = 'open'", (_now(), email_id))
                conn.commit()
                return bool(cur.rowcount)
            finally:
                conn.close()

    def known_ids(self) -> set:
        """Ids already triaged, so a re-run does not pay to classify them twice."""
        conn = self._connect()
        try:
            return {r[0] for r in conn.execute("SELECT email_id FROM flagged_emails")}
        finally:
            conn.close()

    def list_open(self) -> List[Dict]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM flagged_emails WHERE status='open' "
                "ORDER BY flagged_at DESC")]
        finally:
            conn.close()

    def __len__(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT count(*) FROM flagged_emails").fetchone()[0]
        finally:
            conn.close()
