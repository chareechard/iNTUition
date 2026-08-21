"""Small shared completion adapter over iNTUition's established AI credentials."""
import base64
import os
import shutil
import uuid
from typing import Dict, List, NamedTuple, Optional

from intuition import claude_bridge, omniroute_provider, research


class ProviderError(RuntimeError):
    pass


MIN_ANSWER_CHARS = 20


def is_degenerate(text: Optional[str]) -> bool:
    """Whether a reply is too short to be a real answer rather than a routing failure.

    Free and auto-routed models occasionally return a near-empty reply - a
    single punctuation mark, an apology with no content - that a naive caller
    would file as a real result. Every rung of every tier ladder checks this
    before accepting an answer.
    """
    return len((text or "").strip()) < MIN_ANSWER_CHARS


def status(preferred: Optional[str] = None) -> Dict:
    if preferred not in (research.BACKEND_CLI, research.BACKEND_API):
        omni = omniroute_provider.status()
        if omni["ready"]:
            return omni
    return research.status(preferred)


def _image_media_type(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"


def _stage_images_for_cli(cwd: str, images: List[bytes]) -> str:
    """Write ``images`` into a throwaway subfolder of ``cwd`` for the CLI's Read
    tool to open, returning that subfolder's name. A unique name per call, not a
    fixed one: this sandbox is shared across every CLI-backend caller, and nothing
    here assumes only one of them ever runs at a time."""
    folder = "vision_" + uuid.uuid4().hex[:12]
    directory = os.path.join(cwd, folder)
    os.makedirs(directory, exist_ok=True)
    for i, data in enumerate(images, start=1):
        ext = "png" if _image_media_type(data) == "image/png" else "jpg"
        with open(os.path.join(directory, "page{}.{}".format(i, ext)), "wb") as f:
            f.write(data)
    return folder


def complete(prompt: str, system: str, preferred: Optional[str] = None,
             max_tokens: int = 1200, runner=None, client=None,
             download_root: str = ".", model: Optional[str] = None,
             timeout: Optional[float] = None,
             json_schema: Optional[str] = None,
             images: Optional[List[bytes]] = None) -> Dict:
    backend = (research.BACKEND_API if client is not None else
               research.BACKEND_CLI if runner is not None else
               research.resolve_backend(preferred))
    if backend is None:
        raise ProviderError("No AI provider is available")
    if backend == research.BACKEND_OMNIROUTE:
        try:
            return omniroute_provider.complete(prompt, system, max_tokens,
                                                model=model or omniroute_provider.MODEL,
                                                timeout=timeout, images=images)
        except omniroute_provider.OmniRouteError as exc:
            # An explicit OmniRoute choice should surface its error. With automatic
            # routing, retain the existing Claude login/API as a resilient fallback.
            fallback = research.resolve_claude_backend()
            if preferred == research.BACKEND_OMNIROUTE or fallback is None:
                raise ProviderError(str(exc))
            backend = fallback
    if backend == research.BACKEND_CLI:
        cwd = research.sandbox_dir(download_root)
        cli_prompt, staged_dir = prompt, None
        if images:
            staged_dir = _stage_images_for_cli(cwd, images)
            cli_prompt = (
                "{}\n\n---\nRendered page images for this material are staged at "
                "./{}/page1.png, page2.png, ... in page order - read each one with "
                "your Read tool before answering; the extracted text above cannot "
                "show what's inside a diagram, an image-only slide, or a callout "
                "box.".format(prompt, staged_dir))
        try:
            envelope = claude_bridge.run(
                cli_prompt, cwd=cwd,
                model=model or "haiku", tools=(["Read", "Glob"] if images else []),
                system_prompt=system, json_schema=json_schema,
                max_usd=0.15, timeout=int(timeout or 180),
                runner=runner, prompt_on_stdin=True)
        except claude_bridge.BridgeError as exc:
            raise ProviderError(str(exc))
        finally:
            if staged_dir:
                shutil.rmtree(os.path.join(cwd, staged_dir), ignore_errors=True)
        text = claude_bridge.result_text(envelope)
        if not text:
            raise ProviderError("AI provider returned no summary")
        tokens, _searches, cost = claude_bridge.accounting(envelope)
        return {"text": text, "backend": backend,
                "model": claude_bridge.served_model(envelope, model or "haiku"),
                "tokens": tokens, "cost_usd": cost}

    if client is None:
        try:
            import anthropic
            key = research.load_key()
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        except Exception as exc:
            raise ProviderError(str(exc))
    content = []
    for data in (images or []):
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": _image_media_type(data),
            "data": base64.b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": prompt})
    try:
        message = client.messages.create(
            model=model or research.MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}])
    except Exception as exc:
        raise ProviderError(str(exc))
    text = research._text(message)
    if not text:
        raise ProviderError("AI provider returned no summary")
    usage = getattr(message, "usage", None)
    return {"text": text, "backend": backend,
            "model": getattr(message, "model", model or research.MODEL),
            "tokens": {"in": getattr(usage, "input_tokens", 0) or 0,
                       "out": getattr(usage, "output_tokens", 0) or 0},
            "finish_reason": getattr(message, "stop_reason", None)}


class Attempt(NamedTuple):
    model: str
    deadline: float          # seconds; a slow rung is a failed rung


# Ladders are written in OmniRoute's vocabulary - see docs/ai-infrastructure.md for the
# measurements behind each choice. On the CLI and API backends these names mean nothing,
# so a tier collapses to that backend's single existing completion path instead of
# laddering; laddering across routes only means something inside OmniRoute's catalogue.
TIERS = {
    "chat":    [Attempt("auto/coding:free", 25), Attempt("auto/fast", 25),
                Attempt("claude/claude-sonnet-5", 60)],
    "scholar": [Attempt("claude/claude-opus-5", 300)],
    "bulk":    [Attempt("auto/coding:free", 45), Attempt("auto/fast", 45)],
    "vision":  [Attempt("auto/best-vision", 60)],
}


def complete_tier(tier: str, prompt: str, system: str, preferred: Optional[str] = None,
                  max_tokens: int = 1200, download_root: str = ".",
                  json_schema: Optional[str] = None,
                  images: Optional[List[bytes]] = None) -> Dict:
    """Walk the tier's ladder; the first non-degenerate answer wins.

    A rung fails if it errors, exceeds its deadline, or returns a degenerate answer
    (``is_degenerate``). The returned dict's ``rung`` names the model that actually
    answered, because a caller that files the result - a summary, a recall card - has
    to be able to record who wrote it.

    The scholar tier never ladders and never falls through to another backend: if its
    one pinned model is unavailable, this raises rather than quietly answering from a
    different model. Every other tier ladders freely - a bulk or chat call that falls
    through has cost the user a few seconds, not the provenance of a filed note.

    ``images`` is Compendium's only current caller of this param - every other tier
    call passes nothing and behaves exactly as before.
    """
    attempts: List[Attempt] = TIERS[tier]
    backend = research.resolve_backend(preferred)
    if backend is None:
        raise ProviderError("No AI provider is available")

    if backend != research.BACKEND_OMNIROUTE:
        # OmniRoute is not in play, so there is no ladder to walk - the tier's model
        # names would mean nothing on the CLI/API backend. Scholar still insists on the
        # model docs/ai-infrastructure.md pins it to; every other tier takes whatever
        # that backend's normal default is.
        tier_model = research.MODEL if tier == "scholar" else None
        result = complete(prompt, system, preferred=preferred, max_tokens=max_tokens,
                          download_root=download_root, model=tier_model,
                          json_schema=json_schema, images=images)
        if is_degenerate(result.get("text")):
            raise ProviderError("AI provider returned a degenerate answer")
        return dict(result, tier=tier, rung=result.get("model"))

    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            result = omniroute_provider.complete(
                prompt, system, max_tokens, model=attempt.model, timeout=attempt.deadline,
                images=images)
        except omniroute_provider.OmniRouteError as exc:
            last_error = exc
            continue
        if is_degenerate(result.get("text")):
            last_error = ProviderError(
                "{} returned a degenerate answer".format(attempt.model))
            continue
        return dict(result, tier=tier, rung=attempt.model)

    if tier == "scholar":
        raise ProviderError(str(last_error) if last_error
                            else "Scholar tier ladder exhausted")

    fallback = research.resolve_claude_backend()
    if fallback is None:
        raise ProviderError(str(last_error) if last_error
                            else "AI provider ladder exhausted")
    result = complete(prompt, system, preferred=fallback, max_tokens=max_tokens,
                      download_root=download_root, json_schema=json_schema,
                      images=images)
    if is_degenerate(result.get("text")):
        raise ProviderError("AI provider returned a degenerate answer")
    return dict(result, tier=tier, rung=result.get("model"))
