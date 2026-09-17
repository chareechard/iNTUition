import base64
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard


PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(
    b"\x89PNG\r\n\x1a\nminimal-test-png"
).decode("ascii")


class Memory:
    def __init__(self, broken=False):
        self.broken = broken
        self.added = []

    def recent(self, *_args, **_kwargs):
        if self.broken:
            raise OSError("chat database is locked")
        return []

    def add(self, *args, **kwargs):
        if self.broken:
            raise OSError("chat database is read-only")
        self.added.append((args, kwargs))


def _state(tmp_path, memory=None):
    notes = []
    state = SimpleNamespace(
        drive_files=[{"id": "material-1", "name": "lecture.pdf",
                      "rel_path": "SC2002/lecture.pdf",
                      "mime_type": "application/pdf"}],
        lock=threading.Lock(),
        chat_memory=memory or Memory(),
        research_backend=None,
        download_root=str(tmp_path),
        note=notes.append,
    )
    state.notes = notes
    return state


def _handler(state, payload):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = "/api/drive/learn"
    handler.requestline = "POST /api/drive/learn HTTP/1.1"
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


def _learn_patches(tmp_path):
    return (
        patch.object(dashboard.drive, "build_service", return_value=object()),
        patch.object(dashboard.drive, "pull_file",
                     return_value=str(tmp_path / "lecture.pdf")),
        patch.object(dashboard.drive, "extract_learning_text",
                     return_value="A graph is a set of vertices and edges."),
    )


def test_learning_rejects_malformed_snapshot_before_provider_access(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"id": "material-1", "question": "Explain this",
                               "snapshot": "data:image/png;base64,not-valid"})

    handler.do_POST()

    status, response = _response(handler)
    assert status == 400
    assert "valid PNG or JPEG" in response["error"]


def test_learning_rejects_an_image_over_the_decoded_size_cap(tmp_path):
    state = _state(tmp_path)
    encoded = base64.b64encode(b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024)).decode("ascii")
    handler = _handler(state, {"id": "material-1", "question": "Read this",
                               "snapshot": "data:image/jpeg;base64," + encoded})

    handler.do_POST()

    status, response = _response(handler)
    assert status == 400
    assert "valid PNG or JPEG" in response["error"]


def test_learning_accepts_a_large_json_body_for_a_bounded_image(tmp_path):
    state = _state(tmp_path)
    encoded = base64.b64encode(b"\xff\xd8\xff" + b"x" * 1_600_000).decode("ascii")
    handler = _handler(state, {"id": "material-1", "question": "Read this",
                               "snapshot": "data:image/jpeg;base64," + encoded})

    with _learn_patches(tmp_path)[0], _learn_patches(tmp_path)[1], \
            _learn_patches(tmp_path)[2], \
            patch.object(dashboard.omniroute_provider, "complete_image",
                         return_value={"text": "It shows a graph.",
                                       "backend": "omniroute", "model": "vision"}):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 200
    assert response["answer"] == "It shows a graph."


def test_learning_falls_back_to_text_when_vision_raises_unexpectedly(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"id": "material-1", "question": "Explain the image",
                               "snapshot": PNG_DATA_URL})

    with _learn_patches(tmp_path)[0], _learn_patches(tmp_path)[1], \
            _learn_patches(tmp_path)[2], \
            patch.object(dashboard.omniroute_provider, "complete_image",
                         side_effect=RuntimeError("vision process crashed")), \
            patch.object(dashboard.ai_provider, "complete_tier",
                         return_value={"text": "The material explains graphs.",
                                       "backend": "fallback", "model": "chat"}):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 200
    assert response["answer"] == "The material explains graphs."
    assert response["snapshot_note"]
    assert any("vision unavailable" in note for note in state.notes)


def test_learning_returns_an_answer_when_local_chat_memory_is_unavailable(tmp_path):
    state = _state(tmp_path, memory=Memory(broken=True))
    handler = _handler(state, {"id": "material-1", "question": "Summarise this"})

    with _learn_patches(tmp_path)[0], _learn_patches(tmp_path)[1], \
            _learn_patches(tmp_path)[2], \
            patch.object(dashboard.ai_provider, "complete_tier",
                         return_value={"text": "The material defines a graph.",
                                       "backend": "fallback", "model": "chat"}):
        handler.do_POST()

    status, response = _response(handler)
    assert status == 200
    assert response["answer"] == "The material defines a graph."
    assert any("chat history unavailable" in note for note in state.notes)
    assert any("chat memory unavailable" in note for note in state.notes)
