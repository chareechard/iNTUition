"""Zero-setup access to the local OmniRoute OpenAI-compatible gateway."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional
from urllib import error, request
from urllib.parse import urlparse


DEFAULT_URL = "http://127.0.0.1:20128"
MODEL = "auto"
_start_lock = threading.Lock()
_models_lock = threading.Lock()
_models_cache = {"at": 0.0, "ids": []}
MODEL_CACHE_SECONDS = 300
_vision_failure = {"until": 0.0, "error": ""}
VISION_RETRY_SECONDS = 300


class OmniRouteError(RuntimeError):
    pass


def base_url() -> str:
    return (os.environ.get("INTUITION_OMNIROUTE_URL") or DEFAULT_URL).rstrip("/")


def executable() -> Optional[str]:
    # Popen cannot directly execute the PowerShell shim on Windows; npm's .cmd
    # shim is the portable non-shell entry point.
    return shutil.which("omniroute.cmd") or shutil.which("omniroute")


def _local_default() -> bool:
    parsed = urlparse(base_url())
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} \
        and parsed.port == 20128


def managed_locally() -> bool:
    """Whether iNTUition owns the standard loopback OmniRoute companion."""
    return _local_default()


def running(timeout: float = 0.8) -> bool:
    # Do not probe readiness with a bare TCP connection. OmniRoute's HTTP server
    # can retain those incomplete connections in CLOSE_WAIT; the dashboard polls
    # status frequently, eventually wedging an otherwise healthy gateway. A real
    # HTTP request both verifies that the application is responsive and closes the
    # connection according to the protocol. Any HTTP response means it is alive.
    # Model discovery can be expensive while providers hydrate. OmniRoute provides a
    # dedicated constant-time liveness endpoint; use it so a healthy process is never
    # mistaken for a stale listener and restarted into EADDRINUSE.
    req = request.Request(base_url() + "/api/health/ping", method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            response.read()
        return True
    except error.HTTPError:
        return True
    except (OSError, error.URLError, ValueError):
        return False


def status() -> Dict:
    is_running = running()
    binary = executable()
    return {
        "backend": "omniroute",
        "ready": is_running or bool(binary and _local_default()),
        "running": is_running,
        "installed": bool(binary),
        "autostart": bool(binary and _local_default()),
        "source": "OmniRoute auto router",
        "model": MODEL,
    }


def models(timeout: float = 10.0, refresh: bool = False):
    """Return OmniRoute's live model ids, cached to keep dashboard polling cheap."""
    now = time.monotonic()
    with _models_lock:
        if (_models_cache["ids"] and not refresh
                and now - _models_cache["at"] < MODEL_CACHE_SECONDS):
            return list(_models_cache["ids"])
    req = request.Request(base_url() + "/v1/models", method="GET")
    api_key = os.environ.get("OMNIROUTE_API_KEY")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ids = [str(row.get("id")) for row in payload.get("data", [])
               if isinstance(row, dict) and row.get("id")]
    except (OSError, error.URLError, ValueError, TypeError):
        with _models_lock:
            return list(_models_cache["ids"])
    with _models_lock:
        _models_cache.update(at=now, ids=ids)
    return list(ids)


def _stop_stale(binary: str) -> None:
    """Ask OmniRoute to clear its own unresponsive local server, if any.

    Using the CLI lifecycle command avoids guessing which process owns the port and
    accidentally terminating an unrelated application.  A missing/stale pid file is
    harmless: the subsequent launch will fail with the useful bind error instead.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            [binary, "stop"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _startup_error(log) -> str:
    try:
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    lines = [ansi.sub("", line).strip() for line in output.splitlines()
             if ansi.sub("", line).strip()]
    useful = next((line for line in reversed(lines)
                   if "error" in line.lower() or "EADDR" in line), None)
    return (useful or (lines[-1] if lines else ""))[:500]


def ensure_running(wait_seconds: Optional[float] = None) -> None:
    if wait_seconds is None:
        # Current OmniRoute builds may compile/load a large provider catalogue on the
        # first launch. The dashboard must wait for readiness instead of killing a
        # healthy-but-still-starting companion at the former 30-second deadline.
        wait_seconds = float(os.environ.get("INTUITION_OMNIROUTE_START_TIMEOUT", "120"))
    if running():
        return
    if not _local_default():
        raise OmniRouteError("OmniRoute is not reachable at the configured URL")
    binary = executable()
    if not binary:
        raise OmniRouteError("OmniRoute is not installed")
    with _start_lock:
        if running():
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # A crashed OmniRoute can retain the listener while no longer answering
        # HTTP. Starting over it only produces EADDRINUSE and used to be hidden
        # behind the generic 30-second readiness timeout.
        _stop_stale(binary)
        try:
            log = tempfile.TemporaryFile()
            process = subprocess.Popen(
                [binary, "serve", "--port", "20128", "--no-open", "--no-tray"],
                stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as exc:
            raise OmniRouteError("Could not start OmniRoute: {}".format(exc))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if running(timeout=0.5):
                log.close()
                return
            if process.poll() is not None:
                detail = _startup_error(log)
                log.close()
                # Another OmniRoute process can already own the port while it is
                # still loading providers and not yet answering its health check.
                # Our replacement then exits with EADDRINUSE.  Treat that as a
                # startup race and give the existing listener the remainder of the
                # normal readiness window instead of aborting the desktop launch.
                if "EADDRINUSE" in detail.upper():
                    while time.monotonic() < deadline:
                        if running(timeout=0.5):
                            return
                        time.sleep(0.4)
                raise OmniRouteError("OmniRoute exited during startup{}".format(
                    ": " + detail if detail else ""))
            time.sleep(0.4)
        try:
            process.terminate()
        except OSError:
            pass
        detail = _startup_error(log)
        log.close()
        if detail:
            raise OmniRouteError("OmniRoute did not become ready: {}".format(detail))
    raise OmniRouteError("OmniRoute did not become ready on port 20128")


def _image_data_url(data: bytes) -> str:
    media_type = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
    return "data:{};base64,{}".format(
        media_type, base64.b64encode(data).decode("ascii"))


def complete(prompt: str, system: str, max_tokens: int = 1200,
             model: str = MODEL, timeout: Optional[float] = None,
             images: Optional[List[bytes]] = None) -> Dict:
    ensure_running()
    user_content = prompt
    if images:
        # Same OpenAI-style content-block shape complete_image() already proves out
        # for a single snapshot, generalised to N ordered page images plus the text.
        user_content = [{"type": "text", "text": prompt}]
        user_content += [{"type": "image_url", "image_url": {"url": _image_data_url(data)}}
                         for data in images]
    payload = json.dumps({
        "model": model or MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OMNIROUTE_API_KEY")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = request.Request(base_url() + "/v1/chat/completions", payload,
                          headers=headers, method="POST")
    try:
        # A router/provider can occasionally stall. Bound that delay so the shared
        # adapter can fall back to the existing direct Claude route.
        request_timeout = (timeout if timeout is not None else
                           float(os.environ.get("INTUITION_OMNIROUTE_TIMEOUT", "45")))
        with request.urlopen(req, timeout=request_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = "HTTP {}".format(exc.code)
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            parsed = json.loads(body)
            message = (parsed.get("error") or {}).get("message") \
                if isinstance(parsed.get("error"), dict) else parsed.get("error")
            if message:
                detail += ": " + str(message)
        except (ValueError, TypeError, AttributeError):
            pass
        raise OmniRouteError("OmniRoute request failed ({})".format(detail))
    except (OSError, error.URLError, ValueError) as exc:
        raise OmniRouteError("OmniRoute request failed: {}".format(exc))
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        text = ""
    if not text:
        raise OmniRouteError("OmniRoute returned no text")
    usage = data.get("usage") or {}
    return {"text": text, "backend": "omniroute",
            "model": data.get("model") or model or MODEL,
            "tokens": {"in": usage.get("prompt_tokens", 0) or 0,
                       "out": usage.get("completion_tokens", 0) or 0},
            "finish_reason": data["choices"][0].get("finish_reason")}


def complete_image(prompt: str, system: str, image_data_url: str,
                   max_tokens: int = 1200, model: str = MODEL,
                   timeout: Optional[float] = None) -> Dict:
    """Send one dashboard snapshot through OmniRoute's OpenAI vision format."""
    now = time.monotonic()
    if _vision_failure["until"] > now:
        raise OmniRouteError(_vision_failure["error"] or "Vision provider is temporarily unavailable")
    ensure_running()
    vision_model = "auto/best-vision" if not model or model == MODEL else model
    payload = json.dumps({
        "model": vision_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OMNIROUTE_API_KEY")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = request.Request(base_url() + "/v1/chat/completions", payload,
                          headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout or 60) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
    except error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            parsed = json.loads(body)
            failure = parsed.get("error") if isinstance(parsed, dict) else None
            detail = (failure.get("message") if isinstance(failure, dict) else failure) or body
        except (ValueError, TypeError, AttributeError):
            detail = ""
        message = "Snapshot analysis failed (HTTP {}{})".format(
            exc.code, ": " + str(detail)[:500] if detail else "")
        if exc.code in (402, 422, 429):
            _vision_failure.update(until=time.monotonic() + VISION_RETRY_SECONDS,
                                   error=message)
        raise OmniRouteError(message)
    except (OSError, error.URLError, ValueError, KeyError, IndexError,
            TypeError, AttributeError) as exc:
        raise OmniRouteError("Snapshot analysis failed: {}".format(exc))
    if not text:
        raise OmniRouteError("Snapshot analysis returned no text")
    return {"text": text, "backend": "omniroute",
            "model": data.get("model") or vision_model}
