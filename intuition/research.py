"""Claude research backend, shared by the dashboard's AI-backed features.

Two backends, same finding shape:

* **cli** (default when ``claude`` is installed) - shells out to the Claude Code CLI in
  print mode. No API key: it uses the login you already have. Every run is a fresh,
  isolated session - see ``build_cli_command`` for exactly what that means.
* **api** - the Anthropic SDK against your own API key. Needs a key and is billed
  separately from a Claude subscription.


The only part of iNTUition that talks to an AI backend, and the only part that sends
anything anywhere other than ntulearn.ntu.edu.sg and Google Drive. The limits that
remain:

* **Never automatic.** A run happens only when a caller explicitly asks
  ``research()`` for a finding on one item. Nothing is sent on a scan, a poll, or a
  page load.
* **Course material only when a caller opts in, and only that item's own course.**
  With materials sharing on, a capped selection of readable files for the item's
  course tag is staged into the sandbox for the length of one run, and the finding
  records exactly which files those were. With it off, only the item text and course
  codes are sent. See ``materials`` for how the selection is made and bounded.

The Anthropic SDK is an optional dependency (``pip install "iNTUition[research]"``)
and the key is read from ``ANTHROPIC_API_KEY`` or ``~/.intuition/anthropic_key``,
never from the repository.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from intuition import claude_bridge
from intuition import materials

# Opus 5 - the board asks open-ended "what should I build" questions, which is exactly
# where reasoning depth pays for itself. Web search is enabled so answers cite libraries
# and papers that exist rather than plausible-sounding ones.
MODEL = "claude-opus-5"
WEB_SEARCH_TOOL = "web_search_20260209"
MAX_TOKENS = 8000
MAX_SEARCHES = 6

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".intuition")
KEY_FILENAME = "anthropic_key"
STORAGE_DIR = ".intuition"   # matches ledger/todo, inside the download root

# ── CLI backend ──────────────────────────────────────────────────────────────
BACKEND_CLI = "cli"
BACKEND_API = "api"
BACKEND_OMNIROUTE = "omniroute"
BACKENDS = (BACKEND_OMNIROUTE, BACKEND_CLI, BACKEND_API)

CLI_BINARY = os.environ.get("INTUITION_CLAUDE_BIN", "claude")
# The CLI takes an alias and resolves it to the current model itself, so this keeps
# working across releases without a version pin here.
CLI_MODEL = "opus"
CLI_TIMEOUT = 600          # seconds; web search plus reasoning is not quick
CLI_MAX_USD = 1.00         # hard per-entry ceiling, enforced by the CLI itself
CLI_TOOLS = "WebSearch,WebFetch"
SANDBOX_DIRNAME = "research"

SETUP_HELP = """\
Research needs one backend. Either works; the CLI needs no key at all.

  A. Claude CLI (default, recommended)
       Install Claude Code and log in once: claude
     That is the whole setup - iNTUition shells out to it in print mode and uses the
     login you already have. Each run is its own isolated session: no CLAUDE.md, no
     skills, plugins, hooks or MCP servers, no saved history, web tools only, and a
     hard ${max_usd} ceiling per entry. It cannot see your terminal's conversations and
     they cannot see it.

  B. Anthropic API key (billed separately from a Claude subscription)
       pip install anthropic
       Create a key at https://platform.claude.com/settings/keys, then either
         setx ANTHROPIC_API_KEY sk-ant-...        (Windows - opens in new shells)
         export ANTHROPIC_API_KEY=sk-ant-...      (macOS/Linux)
       or save it to {path} with:
         python -m intuition.research_run --save_key
       An `ant auth login` profile is picked up automatically too.

Pick one explicitly with --backend cli|api, or INTUITION_RESEARCH_BACKEND.
Verify with:
  python -m intuition.research_run --check

Research only ever runs on an explicit press. Only the entry text and your course
codes are sent - never course files.\
"""

# Where `ant auth login` puts its profiles. The SDK reads these itself; iNTUition only
# needs to know whether one exists, so it can say "you are set up" honestly.
_PROFILE_DIRS = [
    os.path.join(os.path.expanduser("~"), ".config", "anthropic", "credentials"),
    os.path.join(os.environ.get("APPDATA", ""), "Anthropic", "credentials"),
]

SYSTEM_PROMPT = """\
You advise a Nanyang Technological University undergraduate who is deciding what small \
software tools to build to help them learn their current courses. They keep a research \
board; you are researching one entry on it.

Search before you answer. Look for real, current work: search the web, and search \
GitHub specifically - repositories are where the closest prior art for a student tool \
usually lives. Prefer a repository you have actually looked at over one you remember. \
Note whether a repo looks maintained and whether it is worth forking rather than \
starting from scratch.

If a ./materials folder is present, it holds real files from this course - lecture \
transcripts, slides, handouts. Read the ones that look relevant before answering, and \
ground your advice in what the course actually covers: its notation, its emphasis, the \
specific topics it treats. Quote or name a specific piece of material when it changes \
your recommendation. Do not assume it is complete; it is a capped sample.

Answer in Markdown, under 450 words, with these sections and nothing else:

**Verdict** - one sentence on whether this is worth building, and why.
**Prior art** - two or three existing tools, libraries or repositories that already do \
most of it, each with a link, at least one from GitHub where one exists. Say plainly if \
there is nothing close.
**Shape it** - the smallest version worth building first, and what to cut. Tie it to \
the course's actual content where the materials let you.
**Watch out** - the one or two things most likely to sink it.

Be concrete and specific to the course named, if one is. Recommend not building it when \
an existing tool is good enough - saying so is more useful than encouragement. Do not \
speculate about assessment rules or deadlines unless the materials state them.\
"""


def key_path() -> str:
    return os.path.join(CONFIG_DIR, KEY_FILENAME)


def load_key() -> Optional[str]:
    """Environment first, then the saved file. Never printed or logged anywhere."""
    env = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if env:
        return env
    try:
        with open(key_path(), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise ValueError("An API key is required")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = key_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    try:  # best effort - Windows ignores POSIX modes
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def sdk_present() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def profile_present() -> bool:
    """Whether `ant auth login` has left a credential the SDK can resolve on its own."""
    for directory in _PROFILE_DIRS:
        if not directory:
            continue
        try:
            if any(n.endswith(".json") for n in os.listdir(directory)):
                return True
        except OSError:
            continue
    return False


def credential_source() -> Optional[str]:
    """Which credential will be used, in the SDK's own precedence order.

    Named, never valued - the key itself is not returned or logged anywhere.
    """
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return "ANTHROPIC_API_KEY"
    try:
        with open(key_path(), "r", encoding="utf-8") as f:
            if f.read().strip():
                return key_path()
    except OSError:
        pass
    if profile_present():
        return "ant auth login profile"
    return None


def configured() -> bool:
    return credential_source() is not None


def available() -> bool:
    """Usable by either route: the CLI's own login, or an API credential."""
    return cli_present() or (sdk_present() and configured())


def status(preferred: Optional[str] = None) -> Dict:
    """Everything the UI needs to say whether research will work, and via what."""
    backend = resolve_backend(preferred)
    if backend == BACKEND_OMNIROUTE:
        from intuition import omniroute_provider
        return omniroute_provider.status()
    return {
        "backend": backend,
        "ready": backend is not None,
        "cli": cli_present(),
        "cli_version": cli_version() if cli_present() else None,
        "sdk": sdk_present(),
        "configured": configured(),
        # Named, never valued - the key itself never leaves this process.
        "source": ("Claude CLI login" if backend == BACKEND_CLI
                   else credential_source()),
        "model": CLI_MODEL if backend == BACKEND_CLI else MODEL,
        "key_path": key_path(),
        "max_usd": CLI_MAX_USD,
    }


class ResearchError(Exception):
    pass


# ── CLI backend: an isolated research session ────────────────────────────────

def cli_path() -> Optional[str]:
    return shutil.which(CLI_BINARY)


def cli_present() -> bool:
    return cli_path() is not None


_cli_version_cache: Dict[str, Optional[str]] = {}


def cli_version() -> Optional[str]:
    """Cached: the dashboard polls status twice a second and this spawns a process."""
    if CLI_BINARY in _cli_version_cache:
        return _cli_version_cache[CLI_BINARY]
    _cli_version_cache[CLI_BINARY] = _read_cli_version()
    return _cli_version_cache[CLI_BINARY]


def _read_cli_version() -> Optional[str]:
    if not cli_present():
        return None
    try:
        out = subprocess.run([CLI_BINARY, "--version"], capture_output=True, text=True,
                             timeout=20, creationflags=claude_bridge.no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    # "2.1.225 (Claude Code)" -> "2.1.225"; the UI already says which CLI this is.
    return ((out.stdout or "").strip().split() or [None])[0]


def sandbox_dir(download_root: str) -> str:
    """The empty directory each research session runs in.

    Deliberately not the repository and not your download tree: the session's working
    directory is the one place a coding agent looks around by default, so it is given
    somewhere with nothing in it.
    """
    path = os.path.join(download_root, STORAGE_DIR, SANDBOX_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def build_cli_command(prompt: str, model: str = CLI_MODEL, web: bool = True,
                      max_usd: float = CLI_MAX_USD,
                      read_materials: bool = False) -> List[str]:
    """Assemble the research invocation.

    The isolation flags themselves live in ``claude_bridge`` - shared with the
    email triage, which needs the same guarantees against text it did not write. This function only decides the research-specific parts: which tools the
    task needs, and the researcher system prompt in place of the coding-agent one.
    """
    tools = []
    if web:
        tools += ["WebSearch", "WebFetch"]
    if read_materials:
        # Read-only, and only because material was staged for this run. Still no
        # Write, Edit, Bash or Task, and no --add-dir: the reach stops at the
        # sandbox the session already runs in.
        tools += ["Read", "Glob", "Grep"]
    return claude_bridge.build_command(
        prompt, model=model, tools=tools, system_prompt=SYSTEM_PROMPT,
        max_usd=max_usd, cli=CLI_BINARY)


_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _sources_from_text(text: str) -> List[Dict]:
    """The CLI returns prose, not content blocks, so citations come from its links."""
    out, seen = [], set()
    for url in _URL.findall(text or ""):
        url = url.rstrip(".,;:")
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": url})
    return out


def research_via_cli(item: Dict, courses: Optional[List[str]] = None,
                     download_root: str = ".", model: str = CLI_MODEL,
                     web: bool = True, max_usd: float = CLI_MAX_USD,
                     runner=None, material_specs: Optional[List[Dict]] = None,
                     drive_service=None, direction: Optional[str] = None) -> Dict:
    if not cli_present():
        raise ResearchError(
            "The Claude CLI ({}) is not on PATH. Install Claude Code, or switch the "
            "backend to api.".format(CLI_BINARY))

    cwd = sandbox_dir(download_root)
    # Staging happens here rather than in the caller so one place owns the temporary
    # copies and the `finally` below is guaranteed to remove them.
    staged: List[Dict] = []
    if material_specs:
        staged = materials.stage(material_specs, cwd, service=drive_service)

    try:
        prompt = build_prompt(item, courses, materials=staged, direction=direction)
        cmd = build_cli_command(prompt, model=model, web=web, max_usd=max_usd,
                                read_materials=bool(staged))

        run = runner or subprocess.run
        try:
            proc = run(cmd, capture_output=True, text=True, cwd=cwd,
                       timeout=CLI_TIMEOUT, creationflags=claude_bridge.no_window())
        except subprocess.TimeoutExpired:
            raise ResearchError("The CLI did not finish within {}s.".format(CLI_TIMEOUT))
        except OSError as e:
            raise ResearchError("Could not run {}: {}".format(CLI_BINARY, e))
        return _finish_cli(proc, data_model=model, staged=staged)
    finally:
        # Course material never outlives the run that needed it.
        materials.clear(cwd)


def _finish_cli(proc, data_model: str, staged: List[Dict]) -> Dict:
    """Parse one completed CLI run into a finding."""
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise ResearchError("The CLI returned nothing. {}".format(
            (proc.stderr or "").strip()[:300] or "No error output."))
    try:
        data = json.loads(stdout)
    except ValueError:
        raise ResearchError("Could not parse the CLI's JSON: {}".format(stdout[:300]))

    denied = sorted({d.get("tool_name", "?")
                     for d in (data.get("permission_denials") or [])})
    if data.get("is_error") or data.get("subtype") != "success":
        raise ResearchError("CLI run failed ({}){}".format(
            data.get("api_error_status") or data.get("subtype") or "unknown",
            " - denied: " + ", ".join(denied) if denied else ""))

    text = (data.get("result") or "").strip()
    if not text:
        raise ResearchError("The CLI produced no answer.")
    if denied:
        # Not fatal - the answer exists, but it was reached without the web, so say so
        # rather than passing off an unsourced answer as a researched one.
        text += ("\n\n_(iNTUition: {} was denied, so this answer is unsourced. Check "
                 "the permission flags.)_".format(", ".join(denied)))

    tokens, searches = _cli_accounting(data)
    served = _cli_model(data, data_model)
    return {
        "text": text,
        "model": served,
        "backend": BACKEND_CLI,
        "sources": _sources_from_text(text),
        "at": datetime.now().isoformat(timespec="seconds"),
        "tokens": tokens,
        "searches": searches,
        "cost_usd": round(data.get("total_cost_usd") or 0, 4),
        # Exactly what was shared, recorded on the finding so it is auditable later.
        "materials": [m["name"] for m in staged],
    }


def _cli_accounting(data: Dict):
    """Totals for the whole run - see claude_bridge.accounting."""
    tokens, searches, _cost = claude_bridge.accounting(data)
    return tokens, searches


def _cli_model(data: Dict, requested: str) -> str:
    """Which model answered - see claude_bridge.served_model."""
    return claude_bridge.served_model(data, requested)


def resolve_claude_backend() -> Optional[str]:
    """Return the legacy direct provider, used as OmniRoute's fallback."""
    if cli_present():
        return BACKEND_CLI
    if sdk_present() and configured():
        return BACKEND_API
    return None


def resolve_backend(preferred: Optional[str] = None) -> Optional[str]:
    """Explicit choice, then the environment, then whatever is actually usable."""
    choice = (preferred or os.environ.get("INTUITION_RESEARCH_BACKEND") or "").strip()
    if choice in BACKENDS:
        return choice
    from intuition import omniroute_provider
    if omniroute_provider.status()["ready"]:
        return BACKEND_OMNIROUTE
    return resolve_claude_backend()


def build_prompt(item: Dict, courses: Optional[List[str]] = None,
                 materials: Optional[List[Dict]] = None,
                 direction: Optional[str] = None) -> str:
    """The entire payload that leaves the machine, assembled in one readable place.

    ``materials`` names the files staged for this run - the contents reach the model
    only if it chooses to read them, and only from the sandbox.

    ``direction`` is the board's standing steer, typed by the user. Unlike an email
    body or a fetched page, this is text the user wrote for this purpose, so it is
    an instruction rather than evidence - and it is stated as a preference, not a
    hard constraint, so a genuinely better answer outside it can still come back.
    """
    lines = ["Research this board entry.", "",
             "Entry: {}".format(item.get("title", "").strip())]
    if item.get("course"):
        lines.append("Course: {}".format(item["course"]))
    if item.get("notes"):
        lines.append("My notes: {}".format(item["notes"]))
    if item.get("link"):
        lines.append("Link I found: {}".format(item["link"]))
    steer = (direction or "").strip()
    if steer:
        lines += ["", "The direction I want research to take, in general:", steer,
                  "Weigh this when choosing what to pursue and what to recommend. "
                  "If the best answer for this entry falls outside it, say so and "
                  "give it anyway."]
    if courses:
        lines += ["", "Courses I am taking this semester: {}".format(
            ", ".join(sorted(set(courses))))]
    if materials:
        lines += ["", "My own course materials are in ./materials - read what looks "
                      "relevant before answering:"]
        lines += ["  - {}".format(m["name"]) for m in materials]
    return "\n".join(lines)


def _sources(message) -> List[Dict]:
    """Pull cited pages out of the web-search result blocks, newest search last.

    A failed search returns a single error object rather than a list of results, so
    the shape has to be checked before iterating.
    """
    out: List[Dict] = []
    seen = set()
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        results = getattr(block, "content", None)
        if not isinstance(results, list):
            continue  # {"error_code": ...} - search unavailable or capped
        for r in results:
            url = getattr(r, "url", "") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "title": getattr(r, "title", "") or url})
    return out


def _text(message) -> str:
    return "\n".join(
        b.text for b in (getattr(message, "content", []) or [])
        if getattr(b, "type", "") == "text" and getattr(b, "text", "")
    ).strip()


def research(item: Dict, courses: Optional[List[str]] = None,
             model: Optional[str] = None, key: Optional[str] = None,
             web: bool = True, client=None, backend: Optional[str] = None,
             download_root: str = ".", runner=None,
             material_specs: Optional[List[Dict]] = None,
             drive_service=None, direction: Optional[str] = None) -> Dict:
    """Research one entry and return the finding. Raises ResearchError, never blocks.

    Routes to whichever backend is available unless one is named. Passing ``client``
    forces the API path and ``runner`` forces the CLI path - both exist for tests.
    """
    if not ((item or {}).get("title") or "").strip():
        raise ResearchError("Nothing to research: the entry has no title")

    chosen = backend or (BACKEND_API if client is not None else
                         BACKEND_CLI if runner is not None else resolve_backend())
    if chosen is None:
        raise ResearchError(
            "No research backend. Install the Claude CLI (recommended - uses your "
            "existing login), or set an Anthropic API key. Help: python -m "
            "intuition.research_run --setup")
    if chosen == BACKEND_CLI:
        return research_via_cli(item, courses=courses, download_root=download_root,
                                model=model or CLI_MODEL, web=web, runner=runner,
                                material_specs=material_specs,
                                drive_service=drive_service, direction=direction)

    if chosen == BACKEND_OMNIROUTE:
        from intuition import omniroute_provider
        try:
            result = omniroute_provider.complete(
                build_prompt(item, courses, direction=direction), SYSTEM_PROMPT,
                MAX_TOKENS)
        except omniroute_provider.OmniRouteError as exc:
            fallback = resolve_claude_backend()
            if backend == BACKEND_OMNIROUTE or fallback is None:
                raise ResearchError(str(exc))
            return research(item, courses=courses, model=model, key=key, web=web,
                            client=client, backend=fallback,
                            download_root=download_root, runner=runner,
                            material_specs=material_specs,
                            drive_service=drive_service, direction=direction)
        result.update({"materials": [], "sources": [],
                       "at": datetime.now().isoformat(timespec="seconds")})
        return result

    model = model or MODEL
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise ResearchError(
                "The anthropic package is not installed. Run: pip install anthropic")
        key = key or load_key()
        if not key and not configured():
            raise ResearchError(
                "No Anthropic credential. Run: ant auth login (no key to store), or "
                "set ANTHROPIC_API_KEY, or save a key to {}. Full help: python -m "
                "intuition.research_run --setup".format(key_path()))
        try:
            # With no explicit key, let the SDK resolve its own credentials - an
            # `ant auth login` profile counts, so a static key is never required.
            client = (anthropic.Anthropic(api_key=key) if key
                      else anthropic.Anthropic())
        except Exception as e:  # noqa: BLE001 - the SDK raises TypeError when unset
            raise ResearchError(
                "No Anthropic credential found ({}). Run: python -m "
                "intuition.research_run --setup".format(e))

    tools = ([{"type": WEB_SEARCH_TOOL, "name": "web_search",
               "max_uses": MAX_SEARCHES}] if web else [])
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        messages=[{"role": "user",
                   "content": build_prompt(item, courses, direction=direction)}],
    )
    if tools:
        kwargs["tools"] = tools

    try:
        # Streamed because web search plus adaptive thinking can run well past the
        # non-streaming timeout; only the final message is needed.
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except ResearchError:
        raise
    except Exception as e:  # noqa: BLE001 - surface the API's own words to the UI
        raise ResearchError(_explain(e))

    if getattr(message, "stop_reason", "") == "refusal":
        raise ResearchError("Claude declined to answer this one.")

    text = _text(message)
    if not text:
        raise ResearchError("Claude returned no text for this entry.")

    usage = getattr(message, "usage", None)
    return {
        "text": text,
        "model": getattr(message, "model", model) or model,
        "backend": BACKEND_API,
        # Materials are a CLI-backend feature: the API path has no filesystem to read
        # from, so nothing is shared here regardless of what was selected.
        "materials": [],
        "sources": _sources(message),
        "at": datetime.now().isoformat(timespec="seconds"),
        "tokens": {
            "in": getattr(usage, "input_tokens", 0) or 0,
            "out": getattr(usage, "output_tokens", 0) or 0,
        },
    }


def _explain(error: Exception) -> str:
    """Turn an SDK exception into something actionable, without echoing the key."""
    name = type(error).__name__
    text = str(error)
    if "Could not resolve authentication" in text:
        return ("No Anthropic credential the SDK can use. Run: ant auth login, or set "
                "ANTHROPIC_API_KEY, or save a key to {}".format(key_path()))
    if "Authentication" in name or "401" in text:
        return "That API key was rejected. Check ANTHROPIC_API_KEY or {}".format(
            key_path())
    if "RateLimit" in name:
        return "Rate limited by the API. Try again shortly."
    if "NotFound" in name:
        return "Model {} is not available to this key.".format(MODEL)
    if "Connection" in name:
        return "Could not reach the API. Check the network."
    return "{}: {}".format(name, text[:300])
