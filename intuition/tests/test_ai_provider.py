import json
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from intuition import ai_provider, research


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


_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 10


class VisionMessages:
    def create(self, **kwargs):
        self.seen = kwargs["messages"][0]["content"]
        return SimpleNamespace(model="claude-test", usage=SimpleNamespace(
            input_tokens=12, output_tokens=4), content=[SimpleNamespace(
                type="text", text="Short summary")])


def test_api_completion_sends_image_blocks_before_the_text_block_when_given():
    messages = VisionMessages()
    ai_provider.complete("input", "Summarize faithfully",
                         client=SimpleNamespace(messages=messages),
                         images=[_PNG])
    assert messages.seen[0]["type"] == "image"
    assert messages.seen[0]["source"]["media_type"] == "image/png"
    assert messages.seen[-1] == {"type": "text", "text": "input"}


def test_cli_completion_stages_images_grants_read_glob_and_cleans_up_after(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_CLI)
    seen = {}

    def fake_runner(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        seen["staged"] = os.listdir(cwd)
        return SimpleNamespace(stdout=json.dumps({
            "result": "A proper answer.", "is_error": False, "subtype": "success",
            "model": "opus"}), stderr="")

    with TemporaryDirectory() as root:
        result = ai_provider.complete(
            "input", "Summarize faithfully", runner=fake_runner,
            download_root=root, images=[_PNG])
        # The staged vision_* subfolder is gone once the call returns - nothing
        # from one run's images should linger for the next caller of this
        # shared sandbox.
        assert os.listdir(research.sandbox_dir(root)) == []

    assert result["text"] == "A proper answer."
    assert len(seen["staged"]) == 1 and seen["staged"][0].startswith("vision_")
    assert "--tools" in seen["cmd"]
    tools_arg = seen["cmd"][seen["cmd"].index("--tools") + 1]
    assert "Read" in tools_arg and "Glob" in tools_arg


def test_cli_completion_grants_no_tools_when_no_images(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_CLI)
    seen = {}

    def fake_runner(cmd, cwd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout=json.dumps({
            "result": "A proper answer.", "is_error": False, "subtype": "success",
            "model": "haiku"}), stderr="")

    with TemporaryDirectory() as root:
        ai_provider.complete("input", "Summarize faithfully", runner=fake_runner,
                             download_root=root)

    tools_arg = seen["cmd"][seen["cmd"].index("--tools") + 1]
    assert tools_arg == ""


def test_is_degenerate_rejects_thin_answers():
    assert ai_provider.is_degenerate(".")
    assert ai_provider.is_degenerate("")
    assert ai_provider.is_degenerate(None)
    assert not ai_provider.is_degenerate("A real answer with real content in it.")


def test_bulk_tier_falls_through_a_degenerate_rung(monkeypatch):
    monkeypatch.setattr(research, "resolve_backend",
                        lambda preferred=None: research.BACKEND_OMNIROUTE)
    calls = []

    def fake_complete(prompt, system, max_tokens, model=None, timeout=None, images=None):
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

    def failing_complete(prompt, system, max_tokens, model=None, timeout=None, images=None):
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

    def failing_complete(prompt, system, max_tokens, model=None, timeout=None, images=None):
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
