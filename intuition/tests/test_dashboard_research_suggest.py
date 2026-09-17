import io
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from intuition import dashboard, profile


def _handler(state, body: dict):
    handler: Any = dashboard.Handler.__new__(dashboard.Handler)
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
        research_suggest_job=None,
        note=lambda _message: None,
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


def test_async_suggest_acknowledges_before_provider_returns(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"keywords": "graph algorithms", "async": True})

    with patch.object(dashboard.ai_provider, "complete_tier",
                       return_value=_fake_reply()), \
            patch.object(dashboard.threading, "Thread") as thread_cls:
        handler.do_POST()
        call = thread_cls.call_args.kwargs
        assert call["target"] is dashboard.do_research_suggest
        call["target"](*call["args"])

    started = _response_json(handler)
    assert started["job"]["status"] == "running"
    assert state.research_suggest_job["status"] == "complete"
    assert state.research_suggest_job["suggestions"] == [{"title": "t", "topic": "x"}]

    status_handler = _handler(state, {})
    status_handler.path = "/api/research/suggest?job={}".format(started["job"]["id"])
    status_handler.do_GET()
    status = _response_json(status_handler)
    assert status["job"]["status"] == "complete"


def test_suggest_with_a_chosen_professor_anchors_the_prompt_and_forces_the_match(tmp_path):
    state = _state(tmp_path)
    professor = dashboard.faculty_db.directory()[0]
    handler = _handler(state, {"professor": professor["id"]})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return _fake_reply()

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=fake_complete_tier):
        handler.do_POST()

    assert "Target supervisor: {}".format(professor["name"]) in captured["prompt"]
    assert "target supervisor" in captured["system"].lower()

    data = _response_json(handler)
    assert data["professor"]["id"] == professor["id"]
    # The chosen professor is a guaranteed lead for every suggestion, even one
    # the keyword/tag catalogue matcher would not have surfaced on its own.
    assert data["faculty_matches"][0][0]["id"] == professor["id"]


def test_research_suggest_pass_is_pinned_to_claude_not_omniroute(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"keywords": "graph algorithms"})

    captured = {}

    def fake_complete_tier(tier, prompt, system, **kwargs):
        captured["preferred"] = kwargs.get("preferred")
        return _fake_reply()

    with patch.object(dashboard.research_mod, "resolve_claude_backend",
                       return_value=dashboard.research_mod.BACKEND_CLI), \
            patch.object(dashboard.ai_provider, "complete_tier",
                          side_effect=fake_complete_tier):
        handler.do_POST()

    assert captured["preferred"] == dashboard.research_mod.BACKEND_CLI


def test_research_suggest_falls_back_to_omniroute_only_without_claude(tmp_path):
    state = _state(tmp_path)
    with patch.object(dashboard.research_mod, "resolve_claude_backend",
                       return_value=None):
        assert (dashboard.research_tab_backend(state)
                == dashboard.research_mod.BACKEND_OMNIROUTE)


def test_suggest_rejects_an_unknown_professor_id(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"professor": "not-a-real-id"})

    with patch.object(dashboard.ai_provider, "complete_tier") as complete:
        handler.do_POST()

    assert complete.call_count == 0
    assert _response_json(handler)["error"] == "unknown professor"


def test_suggest_provider_failure_is_not_retried(tmp_path):
    state = _state(tmp_path)
    handler = _handler(state, {"keywords": "graph algorithms"})

    with patch.object(dashboard.ai_provider, "complete_tier",
                       side_effect=dashboard.ai_provider.ProviderError("provider slow")) as complete:
        handler.do_POST()

    assert complete.call_count == 1
    assert _response_json(handler)["error"] == "provider slow"
