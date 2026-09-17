"""HTTP-layer tests for the Lab's repository endpoints.

The repository CRUD itself lives in test_lab.py (LabRepos); this file covers the
wiring in dashboard.py - that /api/lab/* requests are scoped to the ?repo=/‟repo"
they name, that a fresh lab exposes a default repository, and that creating a
repository keeps other repositories untouched.
"""
import io
import json
import threading
from types import SimpleNamespace

from intuition import dashboard, lab


def _state(tmp_path):
    return SimpleNamespace(
        lab_repos=lab.LabRepos(str(tmp_path)),
        lab_jobs=lab.JobManager(),
        download_root=str(tmp_path),
        lock=threading.Lock(),
        note=lambda _message: None,
    )


def _handler(state, method, path, payload=None):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = path
    handler.requestline = "{} {} HTTP/1.1".format(method, path)
    handler.request_version = "HTTP/1.1"
    raw = json.dumps(payload or {}).encode("utf-8")
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


def _get(state, path):
    handler = _handler(state, "GET", path)
    handler.do_GET()
    return _response(handler)


def _post(state, path, payload):
    handler = _handler(state, "POST", path, payload)
    handler.do_POST()
    return _response(handler)


def test_fresh_lab_has_a_default_repository(tmp_path):
    status, body = _get(_state(tmp_path), "/api/lab/tree")
    assert status == 200
    assert body["repo"] == lab.DEFAULT_REPO_NAME
    assert [r["name"] for r in body["repos"]] == [lab.DEFAULT_REPO_NAME]
    assert body["tree"] == []


def test_files_are_scoped_to_the_named_repository(tmp_path):
    state = _state(tmp_path)
    status, body = _post(state, "/api/lab/repos", {"action": "create", "name": "assignment-1"})
    assert status == 200
    assert {"assignment-1", lab.DEFAULT_REPO_NAME} <= {r["name"] for r in body["repos"]}

    status, body = _post(state, "/api/lab/file", {
        "repo": "assignment-1", "action": "create", "path": "main.c", "kind": "file"})
    assert status == 200
    assert "main.c" in [n["name"] for n in body["tree"]]

    # the other repository never saw that file
    _, other = _get(state, "/api/lab/tree?repo=" + lab.DEFAULT_REPO_NAME)
    assert other["tree"] == []
    _, mine = _get(state, "/api/lab/tree?repo=assignment-1")
    assert [n["name"] for n in mine["tree"]] == ["main.c"]


def test_unknown_repo_falls_back_to_default_rather_than_erroring(tmp_path):
    status, body = _get(_state(tmp_path), "/api/lab/tree?repo=was-deleted")
    assert status == 200
    assert body["repo"] == lab.DEFAULT_REPO_NAME


def test_bad_repository_name_is_rejected(tmp_path):
    status, body = _post(_state(tmp_path), "/api/lab/repos",
                         {"action": "create", "name": "../evil"})
    assert status == 400
    assert "repository" in body["error"].lower()


def test_reorder_endpoint_persists_sibling_order(tmp_path):
    state = _state(tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        _post(state, "/api/lab/file", {"action": "create", "path": name, "kind": "file"})
    status, body = _post(state, "/api/lab/file", {
        "action": "reorder", "parent": "", "order": ["c.py", "a.py", "b.py"]})
    assert status == 200
    assert [n["name"] for n in body["tree"]] == ["c.py", "a.py", "b.py"]

    _, tree = _get(state, "/api/lab/tree")
    assert [n["name"] for n in tree["tree"]] == ["c.py", "a.py", "b.py"]


def test_move_endpoint_moves_a_file_to_another_repository(tmp_path):
    state = _state(tmp_path)
    _post(state, "/api/lab/repos", {"action": "create", "name": "target"})
    _post(state, "/api/lab/file", {"action": "create", "path": "main.py", "kind": "file"})

    status, body = _post(state, "/api/lab/file", {
        "action": "move", "path": "main.py", "toRepo": "target"})
    assert status == 200
    assert [n["name"] for n in body["tree"]] == []

    _, target = _get(state, "/api/lab/tree?repo=target")
    assert [n["name"] for n in target["tree"]] == ["main.py"]


def test_move_endpoint_rejects_unknown_repository(tmp_path):
    state = _state(tmp_path)
    _post(state, "/api/lab/file", {"action": "create", "path": "main.py", "kind": "file"})
    status, body = _post(state, "/api/lab/file", {
        "action": "move", "path": "main.py", "toRepo": "nope"})
    assert status == 400
    assert "error" in body


def test_delete_repository_removes_it_and_keeps_at_least_one(tmp_path):
    state = _state(tmp_path)
    _post(state, "/api/lab/repos", {"action": "create", "name": "temp"})
    status, body = _post(state, "/api/lab/repos", {"action": "delete", "name": "temp"})
    assert status == 200
    assert "temp" not in {r["name"] for r in body["repos"]}
    assert len(body["repos"]) >= 1
