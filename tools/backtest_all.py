"""End-to-end backtest runner for all iNTUition subsystems."""
import json
import os
import sys
import time
from datetime import date, datetime
from urllib import request

sys.path.insert(0, os.path.abspath("."))

from intuition import (
    academic_calendar,
    ai_provider,
    announcements,
    auth,
    build_info,
    chat_memory,
    contentcache,
    inbound,
    ledger,
    notes,
    omniroute_provider,
    schedule,
    semester,
    sync,
    todo,
    triage,
    triage_store,
)


def run_full_backtest(download_root: str = "NTU"):
    root = os.path.abspath(download_root)
    results = {}

    print("=" * 60)
    print("      iNTUition System Backtest Suite")
    print("=" * 60)
    print(f"Timestamp    : {datetime.now().isoformat()}")
    print(f"Sync Folder  : {root}")
    print(f"Build Info   : {build_info.summary()}\n")

    # 1. Academic Calendar & Semester Module
    try:
        current_sem = semester.current_semester()
        sem_str = semester.format_semester(current_sem)
        sem_obj = academic_calendar.get(sem_str)
        curr_week = academic_calendar.week_of(date.today(), sem_str)
        phase = academic_calendar.phase_of(date.today(), sem_str)
        results["Academic Calendar"] = {
            "status": "PASS",
            "details": f"Semester: {sem_str}, Week: {curr_week}, Phase: {phase}"
        }
    except Exception as exc:
        results["Academic Calendar"] = {"status": "FAIL", "error": str(exc)}

    # 2. Authentication Token Store
    try:
        token = auth.load_token()
        expiry = auth.expires_at(token) if token else None
        results["Authentication"] = {
            "status": "PASS" if token else "WARN (No active session token cached)",
            "details": f"Token cached: {bool(token)}, Expiry: {expiry or 'N/A'}"
        }
    except Exception as exc:
        results["Authentication"] = {"status": "FAIL", "error": str(exc)}

    # 3. Content Ledger & Cache
    try:
        led = ledger.Ledger(root)
        cache = contentcache.ContentCache(root)
        results["Ledger & Cache"] = {
            "status": "PASS",
            "details": f"Archived entries: {len(led)}, Cache hits/misses: {cache.stats()}"
        }
    except Exception as exc:
        results["Ledger & Cache"] = {"status": "FAIL", "error": str(exc)}

    # 4. Schedule & Temporal Protocol
    try:
        sched = schedule.Schedule(root)
        monday = date.today() - schedule.timedelta(days=date.today().weekday())
        week_schedule = sched.dynamic_week(monday)
        results["Schedule"] = {
            "status": "PASS",
            "details": f"Sessions: {len(sched)}, Dynamic week items: {len(week_schedule)}"
        }
    except Exception as exc:
        results["Schedule"] = {"status": "FAIL", "error": str(exc)}

    # 6. Active Neural Queries (To-Do)
    try:
        queue = todo.Queue(root)
        snap = queue.snapshot()
        results["To-Do Queue"] = {
            "status": "PASS",
            "details": f"Active queries: {len(snap.get('items', []))}"
        }
    except Exception as exc:
        results["To-Do Queue"] = {"status": "FAIL", "error": str(exc)}

    # 8. Announcements Feed
    try:
        feed = announcements.Feed(root)
        snap = feed.snapshot()
        results["Announcements Feed"] = {
            "status": "PASS",
            "details": f"Stored items: {len(feed.items)}, Has TL;DR: {bool(snap.get('summary'))}"
        }
    except Exception as exc:
        results["Announcements Feed"] = {"status": "FAIL", "error": str(exc)}

    # 9. Chat Memory & Notebooks
    try:
        mem = chat_memory.ChatMemory(root)
        nb = notes.Notebook(root)
        recent_chats = mem.recent("default", "test")
        due_cards = nb.due()
        results["Chat Memory & Notebooks"] = {
            "status": "PASS",
            "details": f"Chat memory ready ({len(recent_chats)} recent), Cards due: {len(due_cards)}"
        }
    except Exception as exc:
        results["Chat Memory & Notebooks"] = {"status": "FAIL", "error": str(exc)}



    # 10. Inbound Mail & Triage Store
    try:
        t_store = triage_store.TriageStore(root)
        cfg = triage.load_config(root)
        open_flags = t_store.list_open()
        results["Inbound & Triage Store"] = {
            "status": "PASS",
            "details": f"Open flags: {len(open_flags)}, Keywords: {len(cfg.get('keywords', []))}"
        }
    except Exception as exc:
        results["Inbound & Triage Store"] = {"status": "FAIL", "error": str(exc)}

    # 11. AI Provider & OmniRoute Gateway
    try:
        ai_st = ai_provider.status()
        omni_st = omniroute_provider.status()
        results["AI Provider & OmniRoute"] = {
            "status": "PASS",
            "details": f"Ready: {ai_st.get('ready')}, OmniRoute running: {omni_st.get('running')}"
        }
    except Exception as exc:
        results["AI Provider & OmniRoute"] = {"status": "FAIL", "error": str(exc)}

    # 12. Dashboard Server Liveness
    try:
        req = request.Request("http://127.0.0.1:8384/api/state", method="GET")
        with request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results["Dashboard Server (HTTP)"] = {
            "status": "PASS",
            "details": f"HTTP 200 OK - Courses: {len(data.get('courses', []))}, Plan: {len(data.get('plan', []))}"
        }
    except Exception as exc:
        results["Dashboard Server (HTTP)"] = {
            "status": "WARN / OFFLINE",
            "details": f"Could not reach 127.0.0.1:8384: {exc}"
        }

    # Print Report
    print(f"{'SUBSYSTEM':<28} | {'STATUS':<20} | DETAILS / NOTES")
    print("-" * 80)
    for name, res in results.items():
        st = res["status"]
        det = res.get("details") or res.get("error", "")
        print(f"{name:<28} | {st:<20} | {det}")
    print("-" * 80)

    all_passed = all("FAIL" not in res["status"] for res in results.values())
    print(f"\nOverall Result: {'ALL SUBSYSTEMS HEALTHY' if all_passed else 'SOME SUBSYSTEMS REPORTED ISSUES'}\n")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(run_full_backtest("NTU"))
