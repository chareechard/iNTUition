import io
import json
from urllib import error
from unittest.mock import patch

from intuition import dashboard, omniroute_provider


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_completion_uses_openai_shape_and_default_model():
    body = {"choices": [{"message": {"content": "Useful result"}}],
            "model": "routed-model",
            "usage": {"prompt_tokens": 8, "completion_tokens": 3}}
    with patch.object(omniroute_provider, "ensure_running"), \
            patch.object(omniroute_provider.request, "urlopen",
                         return_value=Response(json.dumps(body).encode())) as call:
        result = omniroute_provider.complete("question", "system", 99)
    sent = json.loads(call.call_args.args[0].data)
    assert sent["model"] == "auto"
    assert sent["max_tokens"] == 99
    assert result == {"text": "Useful result", "backend": "omniroute",
                      "model": "routed-model", "tokens": {"in": 8, "out": 3},
                      "finish_reason": None}


def test_completion_sends_ordered_image_blocks_then_text_when_images_given():
    body = {"choices": [{"message": {"content": "Useful result"}}],
            "model": "routed-model", "usage": {}}
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 10
    jpg = b"\xff\xd8\xff" + b"x" * 10
    with patch.object(omniroute_provider, "ensure_running"), \
            patch.object(omniroute_provider.request, "urlopen",
                         return_value=Response(json.dumps(body).encode())) as call:
        omniroute_provider.complete("question", "system", images=[png, jpg])
    sent = json.loads(call.call_args.args[0].data)
    content = sent["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "question"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_completion_uses_plain_string_content_when_no_images():
    body = {"choices": [{"message": {"content": "Useful result"}}],
            "model": "routed-model", "usage": {}}
    with patch.object(omniroute_provider, "ensure_running"), \
            patch.object(omniroute_provider.request, "urlopen",
                         return_value=Response(json.dumps(body).encode())) as call:
        omniroute_provider.complete("question", "system")
    sent = json.loads(call.call_args.args[0].data)
    assert sent["messages"][1]["content"] == "question"


def test_installed_local_gateway_is_ready_for_lazy_start():
    with patch.object(omniroute_provider, "running", return_value=False), \
            patch.object(omniroute_provider, "executable", return_value="omniroute.cmd"):
        result = omniroute_provider.status()
    assert result["ready"] is True
    assert result["running"] is False
    assert result["autostart"] is True


def test_default_gateway_is_dashboard_managed():
    with patch.dict(omniroute_provider.os.environ,
                    {"INTUITION_OMNIROUTE_URL": "http://127.0.0.1:20128"}):
        assert omniroute_provider.managed_locally() is True


def test_dashboard_watchdog_restarts_gateway():
    class Stop:
        calls = 0

        def wait(self, interval):
            self.calls += 1
            return self.calls > 1

    state = type("State", (), {"note": lambda self, message: None})()
    with patch.object(omniroute_provider, "ensure_running") as ensure:
        dashboard.omniroute_watchdog(state, Stop(), interval=0)
    ensure.assert_called_once_with()


def test_running_uses_complete_http_request_not_bare_socket():
    with patch.object(omniroute_provider.request, "urlopen",
                      return_value=Response(b'{}')) as call:
        assert omniroute_provider.running() is True
    assert call.call_args.args[0].full_url.endswith("/api/health/ping")
    assert call.call_args.kwargs["timeout"] == 0.8


def test_running_accepts_http_error_as_responsive_gateway():
    failure = error.HTTPError("http://localhost/v1/models", 401, "Unauthorized", {}, None)
    with patch.object(omniroute_provider.request, "urlopen", side_effect=failure):
        assert omniroute_provider.running() is True


def test_start_clears_stale_gateway_before_launching():
    process = type("Process", (), {"poll": lambda self: None})()
    with patch.object(omniroute_provider, "running",
                      side_effect=[False, False, True]), \
            patch.object(omniroute_provider, "executable",
                         return_value="omniroute.cmd"), \
            patch.object(omniroute_provider.subprocess, "run") as stop, \
            patch.object(omniroute_provider.subprocess, "Popen",
                         return_value=process) as start:
        omniroute_provider.ensure_running(wait_seconds=1)
    assert stop.call_args.args[0] == ["omniroute.cmd", "stop"]
    assert start.call_args.args[0][:2] == ["omniroute.cmd", "serve"]


def test_start_surfaces_an_early_process_failure():
    process = type("Process", (), {"poll": lambda self: 1})()
    log = io.BytesIO(b"Error: listen EADDRINUSE 0.0.0.0:20128\n")
    with patch.object(omniroute_provider, "running", return_value=False), \
            patch.object(omniroute_provider, "executable",
                         return_value="omniroute.cmd"), \
            patch.object(omniroute_provider, "_stop_stale"), \
            patch.object(omniroute_provider.tempfile, "TemporaryFile",
                         return_value=log), \
            patch.object(omniroute_provider.subprocess, "Popen",
                         return_value=process):
        try:
            omniroute_provider.ensure_running(wait_seconds=1)
        except omniroute_provider.OmniRouteError as exc:
            assert "EADDRINUSE" in str(exc)
        else:
            raise AssertionError("expected OmniRouteError")


def test_start_reuses_existing_gateway_after_address_in_use_race():
    process = type("Process", (), {"poll": lambda self: 1})()
    log = io.BytesIO(b"Error: listen EADDRINUSE 0.0.0.0:20128\n")
    with patch.object(omniroute_provider, "running",
                      side_effect=[False, False, False, True]), \
            patch.object(omniroute_provider, "executable",
                         return_value="omniroute.cmd"), \
            patch.object(omniroute_provider, "_stop_stale"), \
            patch.object(omniroute_provider.tempfile, "TemporaryFile",
                         return_value=log), \
            patch.object(omniroute_provider.subprocess, "Popen",
                         return_value=process):
        omniroute_provider.ensure_running(wait_seconds=1)


def test_completion_surfaces_provider_http_error_message():
    body = io.BytesIO(json.dumps({"error": {"message": "No provider available"}}).encode())
    failure = error.HTTPError("http://localhost/v1/chat/completions", 503,
                              "Unavailable", {}, body)
    with patch.object(omniroute_provider, "ensure_running"), \
            patch.object(omniroute_provider.request, "urlopen", side_effect=failure):
        try:
            omniroute_provider.complete("question", "system")
        except omniroute_provider.OmniRouteError as exc:
            assert "HTTP 503: No provider available" in str(exc)
        else:
            raise AssertionError("expected OmniRouteError")
