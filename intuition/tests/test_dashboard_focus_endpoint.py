"""Tests for the POST /api/focus request layer - the `log` action the Focus
bar calls when a work stint finishes. Store behaviour is covered in test_focus.py;
this covers validation and routing in the HTTP handler.
"""
import io
import json
import threading
from types import SimpleNamespace

from intuition import focus as focus_mod
from intuition import dashboard


def _state(tmp_path):
    return SimpleNamespace(
        focus=focus_mod.Store(str(tmp_path)),
        lock=threading.RLock(),
        note=lambda _message: None,
    )


def _handler(state, payload):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = "/api/focus"
    handler.requestline = "POST /api/focus HTTP/1.1"
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


def test_log_records_a_stint_and_returns_the_tally(tmp_path):
    state = _state(tmp_path)
    status, response = _post(state, {"action": "log", "minutes": 25,
                                     "kind": "focus", "task": "CE4057 lab"})
    assert status == 200 and response["ok"] is True
    assert response["today"] == 1 and response["today_minutes"] == 25
    assert len(state.focus.sessions) == 1
    assert state.focus.sessions[0]["task"] == "CE4057 lab"


def test_break_stints_stay_out_of_the_study_tally(tmp_path):
    state = _state(tmp_path)
    _post(state, {"action": "log", "minutes": 5, "kind": "short_break"})
    status, response = _post(state, {"action": "log", "minutes": 25, "kind": "focus"})
    assert response["today"] == 1 and response["total"] == 1
    assert len(state.focus.sessions) == 2


def test_missing_or_non_integer_minutes_is_400(tmp_path):
    state = _state(tmp_path)
    assert _post(state, {"action": "log"})[0] == 400
    assert _post(state, {"action": "log", "minutes": "lots"})[0] == 400


def test_out_of_range_minutes_is_400(tmp_path):
    state = _state(tmp_path)
    assert _post(state, {"action": "log", "minutes": 0})[0] == 400
    assert _post(state, {"action": "log", "minutes": 240})[0] == 400
    assert state.focus.sessions == []


def test_bad_kind_is_400(tmp_path):
    assert _post(_state(tmp_path), {"action": "log", "minutes": 25, "kind": "sprint"})[0] == 400


def test_unknown_action_is_400(tmp_path):
    status, response = _post(_state(tmp_path), {"action": "reset"})
    assert status == 400 and response["error"] == "unknown action"
