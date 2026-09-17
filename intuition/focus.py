"""Durable log of completed focus (Pomodoro) sessions.

The Focus bar in the dashboard runs the timer entirely client-side; when a work
stint finishes it POSTs one row here so the "study time today / total" tally
survives a reload and a browser change. Break stints are recorded too but are
kept out of the study-time totals - they are rest, not work.

This is a rolling log (newest MAX_ROWS kept), not a permanent archive: the
point is a running tally and a two-week sparkline, not lifetime analytics.
"""
import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List

from intuition.persistence import atomic_json_dump

STORAGE_DIR = ".intuition"
FILENAME = "focus.json"
MAX_ROWS = 500
KINDS = ("focus", "short_break", "long_break")


def store_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class Store:
    def __init__(self, download_root: str):
        self.path = store_path(download_root)
        self._lock = threading.RLock()
        self.sessions: List[Dict] = []
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.sessions = data.get("sessions", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            self.sessions = []

    def save(self):
        with self._lock:
            atomic_json_dump(self.path, {"sessions": self.sessions}, indent=1)

    def log(self, minutes: int, kind: str = "focus", task: str = "") -> Dict:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ValueError("minutes must be a whole number")
        if not 1 <= minutes <= 180:
            raise ValueError("minutes must be between 1 and 180")
        if kind not in KINDS:
            kind = "focus"
        with self._lock:
            row = {"id": uuid.uuid4().hex[:12],
                   "date": date.today().isoformat(),
                   "at": _now(), "minutes": minutes, "kind": kind,
                   "task": _text(task, 200)}
            self.sessions.append(row)
            # Keep the newest MAX_ROWS; the oldest rows only matter for a tally
            # that has long since moved on.
            del self.sessions[:-MAX_ROWS]
            self.save()
            return row

    def snapshot(self) -> Dict:
        # Pure in-memory folding over a bounded list (<= MAX_ROWS) - safe to call
        # under state.lock. Only focus stints count toward study time; breaks are
        # in self.sessions but never in these totals.
        with self._lock:
            today = date.today()
            today_s = today.isoformat()
            focus_rows = [s for s in self.sessions
                          if s.get("kind", "focus") == "focus"]

            by_day: Dict[str, List[int]] = {}   # date -> [count, minutes]
            by_task: Dict[str, int] = {}        # task label -> minutes
            for s in focus_rows:
                mins = int(s.get("minutes", 0) or 0)
                bucket = by_day.setdefault(s.get("date", ""), [0, 0])
                bucket[0] += 1
                bucket[1] += mins
                label = str(s.get("task", "")).strip()
                if label:
                    by_task[label] = by_task.get(label, 0) + mins

            days = []
            for offset in range(13, -1, -1):
                key = (today - timedelta(days=offset)).isoformat()
                count, mins = by_day.get(key, (0, 0))
                days.append({"date": key, "count": count, "minutes": mins})

            # Consecutive days with at least one stint, ending today - or
            # yesterday, so the streak still shows before today's first stint.
            cursor = today
            if not by_day.get(cursor.isoformat()):
                cursor -= timedelta(days=1)
            streak = 0
            while by_day.get(cursor.isoformat()):
                streak += 1
                cursor -= timedelta(days=1)

            week_start = (today - timedelta(days=today.weekday())).isoformat()
            month_prefix = today_s[:7]
            week_rows = [s for s in focus_rows if s.get("date", "") >= week_start]
            month_rows = [s for s in focus_rows
                          if str(s.get("date", "")).startswith(month_prefix)]
            top_tasks = sorted(by_task.items(), key=lambda kv: -kv[1])[:5]

            today_count, today_minutes = by_day.get(today_s, (0, 0))
            return {
                "today": today_count,
                "today_minutes": today_minutes,
                "week": len(week_rows),
                "week_minutes": sum(int(s.get("minutes", 0) or 0) for s in week_rows),
                "month": len(month_rows),
                "month_minutes": sum(int(s.get("minutes", 0) or 0) for s in month_rows),
                "total": len(focus_rows),
                "total_minutes": sum(int(s.get("minutes", 0) or 0) for s in focus_rows),
                "streak": streak,
                "days": days,
                "tasks": [{"task": label, "minutes": mins}
                          for label, mins in top_tasks],
            }
