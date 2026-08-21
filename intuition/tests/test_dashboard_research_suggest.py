import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard, profile


def _handler(state, body: dict):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = "/api/research/suggest"
    handler.requestline = "POST /api/research/suggest HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    raw = json.dumps(body).encode("utf-8")
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.close_connection = False
    return handler


def _state(tmp_path):
    return SimpleNamespace(
        profile=profile.Store(str(tmp_path)),
        courses=[{"name": "26S1-SC2002-Data Structures"}],
        lock=threading.Lock(),
        research_backend=None,
        download_root=str(tmp_path),
    )


def _fake_reply():
    return {"text": json.dumps([{"title": "t", "topic": "x"}]),
            "backend": "cli", "model": "opus"}


def _response_json(handler):
    body = handler.wfile.getvalue()
    _, _, payload = body.partition(b"\r\n\r\n")
    return json.loads(payload)


def test_suggest_prompt_is_anchored_by_a_freshly_typed_keyword(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"keywords": "graph algorithms, low-resource NLP"})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["tier"] = tier
        captured["prompt"] = prompt
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    assert captured["tier"] == "scholar"
    assert "graph algorithms, low-resource NLP" in captured["prompt"]
    assert "Anchor" in captured["prompt"] or "anchor" in captured["prompt"]


def test_suggest_system_uses_profile_without_hardcoded_student_assumptions(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["system"] = system
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    system = captured["system"]
    assert "capable Year 2 student in Mathematical and Computer Sciences" not in system
    assert "Do not assume a specific programme" in system
    assert "combine at least two skills" in system
    assert "non-trivial, answerable research question" in system
    assert "demanding but finishable" in system
    assert "graduate theory" not in system

def test_suggest_prompt_falls_back_to_the_saved_profile_keyword(tmp_path):
    state = _state(tmp_path)
    state.profile.update(keywords="applied cryptography")
    state.profile.save()
    # Nothing typed this run - the saved profile value should still anchor it.
    handler = _handler(state, {})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["prompt"] = prompt
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    assert "applied cryptography" in captured["prompt"]


def test_suggest_prompt_omits_the_anchor_line_when_no_keyword_exists(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["prompt"] = prompt
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    assert "keywords" not in captured["prompt"].lower()


def test_a_freshly_typed_keyword_overrides_a_different_saved_profile_value(tmp_path):
    state = _state(tmp_path)
    state.profile.update(keywords="applied cryptography")
    state.profile.save()
    handler = _handler(state, {"keywords": "graph algorithms"})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["prompt"] = prompt
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    assert "graph algorithms" in captured["prompt"]
    assert "applied cryptography" not in captured["prompt"]


def test_suggest_response_reaches_the_client(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"keywords": "graph algorithms"})

    with patch.object(dashboard.ai_provider, "complete_tier",
                       return_value=_fake_reply()):
        handler.do_POST()

    data = _response_json(handler)
    assert data["suggestions"] == [{"title": "t", "topic": "x"}]
    assert data["backend"] == "cli"
