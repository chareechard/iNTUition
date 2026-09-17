"""Small local SQLite store for material-grounded chatbot conversations."""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List


class ChatMemory:
    """Persist bounded chat turns without storing source document contents."""

    def __init__(self, root: str):
        directory = os.path.join(os.path.abspath(root), ".intuition")
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "chat_memory.sqlite3")
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS material_chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                material_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                backend TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS material_chat_lookup ON material_chat(session_id, material_id, id)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def add(self, session_id: str, material_id: str, role: str, content: str,
            backend: str = "", model: str = "") -> None:
        content = str(content or "").strip()[:20000]
        if not content or role not in ("user", "assistant"):
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO material_chat(session_id, material_id, role, content, backend, model, created_at) VALUES(?,?,?,?,?,?,?)",
                       (session_id[:80], material_id[:256], role, content,
                        str(backend or "")[:80], str(model or "")[:120], now))
            # A lightweight local safeguard: retain at most 5,000 total turns.
            db.execute("DELETE FROM material_chat WHERE id NOT IN (SELECT id FROM material_chat ORDER BY id DESC LIMIT 5000)")

    def recent(self, session_id: str, material_id: str, limit: int = 10) -> List[Dict]:
        limit = max(1, min(int(limit), 20))
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT role, content, backend, model, created_at FROM material_chat WHERE session_id=? AND material_id=? ORDER BY id DESC LIMIT ?",
                              (session_id[:80], material_id[:256], limit)).fetchall()
        return [dict(zip(("role", "content", "backend", "model", "created_at"), row))
                for row in reversed(rows)]

    def clear(self, session_id: str, material_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM material_chat WHERE session_id=? AND material_id=?",
                       (session_id[:80], material_id[:256]))
