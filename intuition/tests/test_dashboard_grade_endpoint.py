"""Tests for the /api/study/grade HTTP endpoint's tutorial-only gate.

do_grade_tutorial (the worker thread) is covered in test_dashboard_grading.py;
this file covers the request-validation layer in front of it - specifically
that a material outside a "Tutorial" folder is rejected before any upload is
even decoded or a job started, mirroring the frontend's own gate so a direct
API call can't bypass it.
"""
import base64
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard
from intuition.notes import Notebook

PDF_DATA_URL = "data:application/pdf;base64," + base64.b64encode(b"%PDF-fake").decode()


def _state(tmp_path, drive_files):
    return SimpleNamespace(
        drive_files=drive_files,
        lock=threading.Lock(),
        notebook=Notebook(str(tmp_path)),
        grading=False,
        grading_job=None,
        research_backend=None,
        download_root=str(tmp_path),
        note=lambda _message: None,
    )


def _handler(state, payload):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = "/api/study/grade"
    handler.requestline = "POST /api/study/grade HTTP/1.1"
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


def test_non_tutorial_material_is_rejected_before_starting_a_job(tmp_path):
    state = _state(tmp_path, [
        {"id": "midterm-q", "name": "AY 1516 - Questions.pdf",
         "rel_path": "MH1300/Misc/Midterm Exams (Past Year)/AY 1516 - Questions.pdf",
         "mime_type": "application/pdf"},
    ])
    handler = _handler(state, {"id": "midterm-q", "filename": "work.pdf",
                               "data": PDF_DATA_URL})

    with patch.object(dashboard.drive, "credentials_present", return_value=True):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 400
    assert "tutorial" in response["error"].lower()
    assert state.grading is False
    assert state.grading_job is None


def test_material_not_in_the_drive_index_is_also_rejected(tmp_path):
    state = _state(tmp_path, [])
    handler = _handler(state, {"id": "unknown-id", "filename": "work.pdf",
                               "data": PDF_DATA_URL})

    with patch.object(dashboard.drive, "credentials_present", return_value=True):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 400
    assert "tutorial" in response["error"].lower()


def test_tutorial_material_is_accepted_and_starts_a_job(tmp_path):
    state = _state(tmp_path, [
        {"id": "tut-1", "name": "Tut01.pdf",
         "rel_path": "MH1300/Tutorials/Tut01.pdf", "mime_type": "application/pdf"},
    ])
    handler = _handler(state, {"id": "tut-1", "filename": "work.pdf",
                               "data": PDF_DATA_URL})

    with patch.object(dashboard.drive, "credentials_present", return_value=True), \
         patch.object(dashboard, "do_grade_tutorial"):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 200
    assert response["ok"] is True
    assert state.grading is True
    assert state.grading_job["document_id"] == "tut-1"
