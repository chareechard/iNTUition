"""Tests for the POST /api/announcements request layer - specifically the
`add` and `delete` actions for personal (student-authored) announcements, which
are server-backed and need only the dashboard session, not a Blackboard token.

The Feed model itself is covered in test_announcements.py; this file covers
validation and routing in the HTTP handler.
"""
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import announcements as announcements_mod
from intuition import dashboard


def _state(tmp_path):
    return SimpleNamespace(
        announcements=announcements_mod.Feed(str(tmp_path)),
        announcements_syncing=False,
        announcements_summarizing=False,
        announcement_summary_error="",
        lock=threading.RLock(),
        token="",
        research_backend=None,
        note=lambda _message: None,
    )


def _handler(state, payload):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = "/api/announcements"
    handler.requestline = "POST /api/announcements HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    raw = json.dumps(payload).encode("utf-8")
    handler.headers = {"Content-Length": str(len(raw)),
                       "Content-Type": "application/json"}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.close_connection = False
    return handler


def _response(handler):
    raw = handler.wfile.getvalue()
    header, _, body = raw.partition(b"\r\n\r\n")
    status = int(header.split(b" ", 2)[1])
    return status, json.loads(body)


def _post(state, payload):
    handler = _handler(state, payload)
    handler.do_POST()
    return _response(handler)


def test_add_creates_a_local_announcement(tmp_path):
    state = _state(tmp_path)
    status, response = _post(state, {"action": "add", "text": "Lab moved to LT5",
                                     "priority": "urgent"})
    assert status == 200 and response["ok"] is True
    assert response["id"].startswith("local-")
    assert len(state.announcements.local) == 1
    assert state.announcements.local[0]["priority"] == "urgent"


def test_add_rejects_empty_text(tmp_path):
    status, response = _post(_state(tmp_path), {"action": "add", "text": "   "})
    assert status == 400 and "required" in response["error"]


def test_add_rejects_overlong_text(tmp_path):
    status, _ = _post(_state(tmp_path), {"action": "add", "text": "x" * 241})
    assert status == 400


def test_add_rejects_bad_priority(tmp_path):
    status, response = _post(_state(tmp_path),
                             {"action": "add", "text": "x", "priority": "loud"})
    assert status == 400 and "priority" in response["error"]


def test_delete_removes_a_local_announcement(tmp_path):
    state = _state(tmp_path)
    item = state.announcements.add_local("temporary note")
    status, response = _post(state, {"action": "delete", "id": item["id"]})
    assert status == 200 and response["ok"] is True
    assert state.announcements.local == []


def test_delete_refuses_non_local_and_synced_ids(tmp_path):
    state = _state(tmp_path)
    state.announcements.items = [announcements_mod.clean(
        {"id": "lms-1", "title": "Course post"},
        {"id": "_1", "name": "SC2002"})]
    state.announcements.save()
    assert _post(state, {"action": "delete", "id": "12345"})[0] == 404
    assert _post(state, {"action": "delete", "id": "lms-1"})[0] == 404
    assert [i["id"] for i in state.announcements.items] == ["lms-1"]


def test_read_action_still_works_for_local_ids(tmp_path):
    state = _state(tmp_path)
    item = state.announcements.add_local("study")
    status, _ = _post(state, {"action": "read", "id": item["id"], "read": True})
    assert status == 200
    assert state.announcements.snapshot()["unread"] == 0


def test_unknown_action_still_returns_400(tmp_path):
    status, response = _post(_state(tmp_path), {"action": "frobnicate"})
    assert status == 400 and response["error"] == "unknown action"


def test_purge_collapses_cross_posts_without_a_token(tmp_path):
    state = _state(tmp_path)
    body = "<p>No tutorial this week - Students' Union Day.</p>"
    state.announcements.items = [
        announcements_mod.clean({"id": "sc", "title": "SC2002 No tutorial",
                                 "body": body}, {"id": "_1", "name": "SC2002"}),
        announcements_mod.clean({"id": "cz", "title": "CZ2002 No tutorial",
                                 "body": body}, {"id": "_2", "name": "CZ2002"}),
    ]
    state.announcements.save()
    status, response = _post(state, {"action": "purge"})
    assert status == 200 and response["ok"] is True
    assert response["folded"] == 1
    assert state.announcements.snapshot()["total"] == 1
