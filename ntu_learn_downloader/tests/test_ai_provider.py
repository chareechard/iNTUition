from types import SimpleNamespace

from ntu_learn_downloader import ai_provider, research


class Messages:
    def create(self, **kwargs):
        assert kwargs["system"] == "Summarize faithfully"
        return SimpleNamespace(model="claude-test", usage=SimpleNamespace(
            input_tokens=12, output_tokens=4), content=[SimpleNamespace(
                type="text", text="Short summary")])


def test_api_completion_uses_shared_shape():
    result = ai_provider.complete("input", "Summarize faithfully",
                                  client=SimpleNamespace(messages=Messages()))
    assert result["text"] == "Short summary"
    assert result["backend"] == "api"
    assert result["tokens"] == {"in": 12, "out": 4}


def test_is_degenerate_rejects_thin_answers():
    assert ai_provider.is_degenerate(".")
    assert ai_provider.is_degenerate("")
    assert ai_provider.is_degenerate(None)
    assert not ai_provider.is_degenerate("A real answer with real content in it.")


def test_bulk_tier_falls_through_a_degenerate_rung(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_OMNIROUTE)
    calls = []

    def fake_complete(prompt, system, max_tokens, model=None, timeout=None):
        calls.append(model)
        if model == "auto/coding:free":
            return {"text": ".", "backend": "omniroute", "model": model}
        return {"text": "A proper, decision-ready summary.",
                "backend": "omniroute", "model": model}

    monkeypatch.setattr(ai_provider.omniroute_provider, "complete", fake_complete)
    result = ai_provider.complete_tier("bulk", "input", "system")
    assert calls == ["auto/coding:free", "auto/fast"]
    assert result["rung"] == "auto/fast"
    assert result["tier"] == "bulk"
    assert result["text"] == "A proper, decision-ready summary."


def test_bulk_tier_falls_back_to_cli_once_omniroute_is_exhausted(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_OMNIROUTE)
    monkeypatch.setattr(research, "resolve_claude_backend",
                        lambda: research.BACKEND_CLI)

    def failing_complete(prompt, system, max_tokens, model=None, timeout=None):
        raise ai_provider.omniroute_provider.OmniRouteError("route unavailable")

    monkeypatch.setattr(ai_provider.omniroute_provider, "complete", failing_complete)
    monkeypatch.setattr(ai_provider, "complete",
                        lambda *a, **kw: {"text": "A proper CLI-backed answer.",
                                          "backend": "cli", "model": "haiku"})
    result = ai_provider.complete_tier("bulk", "input", "system")
    assert result["backend"] == "cli"
    assert result["tier"] == "bulk"


def test_scholar_tier_never_falls_back(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_OMNIROUTE)

    def failing_complete(prompt, system, max_tokens, model=None, timeout=None):
        raise ai_provider.omniroute_provider.OmniRouteError("route unavailable")

    monkeypatch.setattr(ai_provider.omniroute_provider, "complete", failing_complete)
    called = []
    monkeypatch.setattr(research, "resolve_claude_backend",
                        lambda: called.append(True))
    try:
        ai_provider.complete_tier("scholar", "input", "system")
        assert False, "expected ProviderError"
    except ai_provider.ProviderError:
        pass
    assert not called, "scholar must not fall back to a different backend"
