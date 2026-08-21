"""One hardened way to call the Claude Code CLI non-interactively.

Canonical source. Can be vendored into sibling projects that need the same
guarantees - see ``VENDOR_TARGETS`` and ``tools/check_vendored.py``. Two callers
today, both in this package:

* Research backend (``research.py``) - web-enabled, may read staged course material.
* Email triage (``triage.py``) - no tools at all, schema-constrained, and fed
  untrusted third-party email bodies.

The reason this exists in one file: the flags below are a **security boundary**,
not a convenience. A prompt built from content you did not write - an email body,
a fetched page, a PDF - is attacker-influenced text. Handing that to a CLI
session that has inherited your CLAUDE.md, your MCP servers and the default tool
set means an instruction inside that content is addressed to something holding
real capability. Every flag here removes a way for that to matter, and each is
pinned by a test. Two divergent copies of this logic is how one of them quietly
loses a flag.

Deliberately standard-library only, so a vendored copy has no install step.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Sequence

DEFAULT_BINARY = os.environ.get("INTUITION_CLAUDE_BIN", "claude")
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_USD = 1.00

# Projects carrying a vendored copy of this file, relative to this repo's parent.
# Empty since triage moved in-package: both callers now import this module
# directly, so there is no copy that can drift. Kept because the check is what
# makes vendoring safe if a future sibling ever needs one.
VENDOR_TARGETS = ()


class BridgeError(Exception):
    """A run that produced no usable answer. Never raised for model content."""


def binary() -> str:
    return DEFAULT_BINARY


def present(cli: str = "") -> bool:
    return shutil.which(cli or DEFAULT_BINARY) is not None


def no_window() -> int:
    """Creation flags that keep a console child from stealing a window.

    The CLI is a console-subsystem executable. Spawned from the packaged
    desktop shell - which PyInstaller builds windowed, with no console of its
    own - Windows allocates a fresh console for it, so every research or triage
    run flashes an empty terminal on top of the app. CREATE_NO_WINDOW is the
    documented way to say the child needs no console of its own; stdout and
    stderr are captured through pipes regardless. Zero elsewhere, where the flag
    does not exist and no console is ever allocated.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def build_command(
    prompt: str,
    model: str = "opus",
    tools: Sequence[str] = (),
    system_prompt: Optional[str] = None,
    json_schema: Optional[str] = None,
    max_usd: float = DEFAULT_MAX_USD,
    cli: str = "",
    prompt_on_stdin: bool = False,
) -> List[str]:
    """Build an isolated non-interactive invocation.

    ``tools`` is an allow-list of built-in tool names, e.g. ``("WebSearch",
    "WebFetch")``. Empty - the default - means no tools whatsoever, which is what
    a pure classification task should use.

    ``prompt_on_stdin`` omits the prompt from argv, for callers that pipe it in.
    A long prompt on a Windows command line is a real limit, and an email body
    does not belong in a process listing.

    Each flag, and why it is not optional:

    ``--safe-mode``               no CLAUDE.md, skills, plugins, hooks, custom
                                  agents or MCP servers. None of the operator's
                                  Claude Code configuration reaches a session
                                  that is about to read untrusted text. Auth
                                  still works.
    ``--strict-mcp-config``       with no ``--mcp-config`` passed, zero MCP
                                  servers even if safe-mode's guarantees change.
    ``--no-session-persistence``  nothing written to the session store: the run
                                  cannot be resumed and never appears in
                                  ``claude -c``/``--resume``. Keeps automated
                                  runs out of the human's own history.
    ``--disable-slash-commands``  content in the prompt cannot invoke a skill.
    ``--tools``                   pins the built-in tool set to ``tools``.
    ``--allowed-tools``           pre-approves exactly those, since print mode
                                  has no human to ask and would otherwise deny.
    ``--permission-mode dontAsk`` anything outside the set is refused outright
                                  rather than blocking on an unanswerable prompt.
    ``--max-budget-usd``          hard ceiling per run.
    """
    cmd = [cli or DEFAULT_BINARY, "-p"]
    if not prompt_on_stdin:
        cmd.append(prompt)
    cmd += [
        "--output-format", "json",
        "--model", model,
        "--safe-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--max-budget-usd", str(max_usd),
    ]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if json_schema:
        # Enforced server-side, which beats asking for a shape in prose.
        cmd += ["--json-schema", json_schema]
    allowed = [t for t in tools if t]
    cmd += ["--tools", ",".join(allowed)]
    if allowed:
        cmd += ["--allowed-tools"] + list(allowed)
    return cmd


def run(
    prompt: str,
    cwd: str,
    model: str = "opus",
    tools: Sequence[str] = (),
    system_prompt: Optional[str] = None,
    json_schema: Optional[str] = None,
    max_usd: float = DEFAULT_MAX_USD,
    timeout: int = DEFAULT_TIMEOUT,
    cli: str = "",
    prompt_on_stdin: bool = False,
    runner=None,
) -> Dict:
    """Run one prompt and return the parsed CLI envelope.

    ``cwd`` should be a directory holding only what this run may see: it is the
    one place the session looks around by default. Raises ``BridgeError`` for a
    failure to run or parse; a refusal or an empty answer is returned to the
    caller to interpret, since only the caller knows what an empty answer means
    for its task.
    """
    cmd = build_command(prompt, model=model, tools=tools,
                        system_prompt=system_prompt, json_schema=json_schema,
                        max_usd=max_usd, cli=cli,
                        prompt_on_stdin=prompt_on_stdin)
    call = runner or subprocess.run
    kwargs = dict(capture_output=True, text=True, cwd=cwd, timeout=timeout,
                  encoding="utf-8", errors="replace",
                  creationflags=no_window())
    if prompt_on_stdin:
        kwargs["input"] = prompt

    try:
        proc = call(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise BridgeError("The CLI did not finish within {}s.".format(timeout))
    except FileNotFoundError:
        raise BridgeError(
            "The Claude CLI ({}) is not on PATH.".format(cli or DEFAULT_BINARY))
    except OSError as e:
        raise BridgeError("Could not run the CLI: {}".format(e))

    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise BridgeError("The CLI returned nothing. {}".format(
            (proc.stderr or "").strip()[:300] or "No error output."))
    try:
        data = json.loads(stdout)
    except ValueError:
        raise BridgeError("Could not parse the CLI's JSON: {}".format(stdout[:300]))

    denied = denials(data)
    if data.get("is_error") or data.get("subtype") != "success":
        raise BridgeError("CLI run failed ({}){}".format(
            data.get("api_error_status") or data.get("subtype") or "unknown",
            " - denied: " + ", ".join(denied) if denied else ""))
    return data


def denials(data: Dict) -> List[str]:
    """Tool names the run wanted and was refused - a misconfiguration signal."""
    return sorted({d.get("tool_name", "?")
                   for d in (data.get("permission_denials") or [])})


def result_text(data: Dict) -> str:
    return (data.get("result") or "").strip()


def accounting(data: Dict):
    """Totals for the whole run, and the model that answered.

    Top-level ``usage`` describes only the final iteration - on a multi-step run
    it reports a handful of input tokens and zero searches, which reads as though
    nothing happened. ``modelUsage`` is cumulative and is what ``total_cost_usd``
    derives from, so the figures agree with each other.
    """
    per_model = data.get("modelUsage") or {}
    tokens = {"in": 0, "out": 0}
    searches = 0
    if per_model:
        tokens = {
            "in": sum((m or {}).get("inputTokens", 0) or 0 for m in per_model.values()),
            "out": sum((m or {}).get("outputTokens", 0) or 0
                       for m in per_model.values()),
        }
        searches = sum((m or {}).get("webSearchRequests", 0) or 0
                       for m in per_model.values())
    if not tokens["out"]:
        usage = data.get("usage") or {}
        tokens = {"in": usage.get("input_tokens", 0) or 0,
                  "out": usage.get("output_tokens", 0) or 0}
        searches = (usage.get("server_tool_use") or {}).get(
            "web_search_requests", 0) or 0
    return tokens, searches, round(data.get("total_cost_usd") or 0, 4)


def served_model(data: Dict, requested: str) -> str:
    """Name the model that answered, not the small one used for side tasks.

    A run reports every model it touched, and the CLI uses a cheap one for
    internal work like title generation. On a short answer that side model can
    out-token the one doing the work, so match the requested alias first.
    """
    per_model = data.get("modelUsage") or {}
    if not per_model:
        return requested
    alias = (requested or "").lower()
    for name, info in per_model.items():
        canonical = (info or {}).get("canonicalModel") or name
        if alias and alias in canonical.lower():
            return canonical
    best = max(per_model.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0))
    return (best[1] or {}).get("canonicalModel") or best[0]
