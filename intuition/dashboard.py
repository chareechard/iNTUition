"""iNTUition - local sync dashboard for iNTUition.

Run with::

    python -m intuition.dashboard --download_to NTU

It serves a single-page HUD on http://127.0.0.1:8384 that lets you paste a session
token, scan your courses, see exactly what is new or changed on Blackboard versus your
local folder, and download only what you pick.

Deliberately built on the standard library so the tool keeps its three runtime
dependencies. It binds to loopback only - the session token never leaves the machine.
"""
import argparse
import base64
import binascii
import difflib
import hashlib
import hmac
import html
import json
import mimetypes
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from intuition import auth
from intuition import build_info
from intuition import api as api_mod
from intuition import rest as rest_mod
from intuition import semester as semester_mod
from intuition.api import (
    get_courses,
    get_download_dir,
    get_file_download_link,
    get_recorded_lecture_download_link,
)
from intuition import drive
from intuition import grading as grading_mod
from intuition import solution_pairs as solution_pairs_mod
from intuition import transcribe as transcribe_mod
from intuition.contentcache import ContentCache
from intuition.ledger import Ledger
from intuition import summary as summary_mod
from intuition import research as research_mod
from intuition import inbound as inbound_mod
from intuition import triage_store as triage_store_mod
from intuition import triage as triage_mod
from intuition import triage_run as triage_run_mod
from intuition import academic_calendar as cal_mod
from intuition import schedule as schedule_mod
from intuition import todo as todo_mod
from intuition import announcements as announcements_mod
from intuition import ai_provider
from intuition import omniroute_provider
from intuition import lab as lab_mod
from intuition import lab_analysis as lab_analysis_mod
from intuition import ureca as ureca_mod
from intuition import profile as profile_mod
from intuition import faculty_db
from intuition import saved_topics as saved_topics_mod
from intuition import focus as focus_mod
from intuition.chat_memory import ChatMemory
from intuition.notes import Notebook, NoteConflict, html_to_text
from intuition.sync import PUSHABLE, build_plan, recover_restructured, summarize
from intuition.utils import bounded_filename, download, get_filename_from_url

DEFAULT_PORT = 8384
DEFAULT_HOST = "127.0.0.1"
# Compendium's summaries table and its .tex/.pdf files on disk both grow
# monotonically - see notes.py, which keeps even failed runs by design - so a quiet
# startup sweep is the only thing that ever reclaims that growth short of the user
# deleting summaries by hand. 0 (or negative) disables the sweep entirely.
SUMMARY_RETENTION_DAYS_DEFAULT = 60
ANNOUNCEMENT_SYNC_HOURS = (7, 23)
INBOUND_POLL_SECONDS = 12 * 60 * 60
DRIVE_LEARNING_SYSTEM = """You are FRIDAY, a careful university learning assistant. Answer from the supplied course material. Explain concepts clearly and distinguish what the material states from your own explanation. If the material does not support the answer, say so instead of inventing details. Default to a focused answer under 450 words with at most one worked example; expand only when the student explicitly requests depth. Compare the concepts the student actually names and correct a misleading premise tactfully. Allowed output is explanatory Markdown with headings, lists, compact tables, short quotations, code blocks, equations, worked examples, summaries, flashcards, and revision questions. Use blank lines around headings, quotations, lists, tables, and display equations. Write inline mathematics as \\( ... \\) and display mathematics as \\[ ... \\]; do not use dollar-sign delimiters. Never claim to modify files, submit coursework, browse private systems, or execute actions; you only return learning content."""

# The system prompt above explicitly invites the model past its ~450-word default
# ("expand only when the student explicitly requests depth" - tables, several worked
# examples, flashcards, revision questions), but the old 900-token budget was sized
# only for the default case and gave those expanded answers nowhere to go, cutting
# them off mid-sentence with no visible error. What the OpenAI-style (OmniRoute) and
# Anthropic-native completion paths each call the "the response was cut off by
# max_tokens, not because it was finished" signal - see summary.py's own copy of
# this set for the same reasoning applied to Compendium.
FRIDAY_MAX_TOKENS = 2000
_FRIDAY_TRUNCATED_FINISH_REASONS = ("length", "max_tokens")

LEARNING_SNAPSHOT_MAX_CHARS = 3_000_000
LEARNING_SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024
LEARNING_SNAPSHOT_PREFIXES = {
    "data:image/jpeg;base64,": b"\xff\xd8\xff",
    "data:image/png;base64,": b"\x89PNG\r\n\x1a\n",
}


def validate_learning_snapshot(value: Any) -> str:
    """Validate the bounded data URL accepted by the FRIDAY vision route."""
    snapshot = str(value or "")
    if not snapshot:
        return ""
    prefix = next((candidate for candidate in LEARNING_SNAPSHOT_PREFIXES
                   if snapshot.startswith(candidate)), None)
    if prefix is None or len(snapshot) > LEARNING_SNAPSHOT_MAX_CHARS:
        raise ValueError("snapshot must be a valid PNG or JPEG under 2 MB")
    try:
        image_bytes = base64.b64decode(snapshot[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("snapshot must be a valid PNG or JPEG under 2 MB") from None
    if (not image_bytes or len(image_bytes) > LEARNING_SNAPSHOT_MAX_BYTES
            or not image_bytes.startswith(LEARNING_SNAPSHOT_PREFIXES[prefix])):
        raise ValueError("snapshot must be a valid PNG or JPEG under 2 MB")
    return snapshot


GRADE_UPLOAD_MAX_CHARS = 12_000_000
GRADE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
# A worked tutorial can be a text-based PDF export (grading.py extracts it with
# drive.extract_learning_text) or a photo of handwritten pages (sent to the
# scholar tier as a vision input instead) - the same two paths Ask FRIDAY's
# snapshot route and Compendium's PDF extraction already prove out separately.
GRADE_UPLOAD_PREFIXES = {
    "data:application/pdf;base64,": (b"%PDF", ".pdf"),
    "data:image/jpeg;base64,": (b"\xff\xd8\xff", ".jpg"),
    "data:image/png;base64,": (b"\x89PNG\r\n\x1a\n", ".png"),
}


def validate_grade_upload(value: Any) -> Tuple[bytes, str]:
    """Validate the bounded data URL a worked-tutorial upload arrives as.

    Returns ``(raw_bytes, extension)`` - the extension lets the caller stage a
    temp file with the right suffix so grading.extract_work_content can dispatch
    on it exactly like any other file on disk.
    """
    data_url = str(value or "")
    prefix = next((candidate for candidate in GRADE_UPLOAD_PREFIXES
                   if data_url.startswith(candidate)), None)
    if not data_url or prefix is None or len(data_url) > GRADE_UPLOAD_MAX_CHARS:
        raise ValueError("upload must be a PDF, PNG or JPEG under 8 MB")
    try:
        raw_bytes = base64.b64decode(data_url[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("upload must be a PDF, PNG or JPEG under 8 MB") from None
    magic, extension = GRADE_UPLOAD_PREFIXES[prefix]
    if (not raw_bytes or len(raw_bytes) > GRADE_UPLOAD_MAX_BYTES
            or not raw_bytes.startswith(magic)):
        raise ValueError("upload must be a PDF, PNG or JPEG under 8 MB")
    return raw_bytes, extension


# lab_analysis.py validates every field below before it reaches the page - a
# tighter prompt just means less for that validator to have to reject. The
# "steps" field is this app's own addition to the brief's contract: an
# "initialState" alone can only paint one static picture, not drive the
# Simulation tab's Play/Step/Speed timeline, so the model additionally
# mentally traces its own execution into a short operation log.
LAB_BLUEPRINT_SYSTEM = """You are FRIDAY, reading one source file open in a student's local Python/Java/C IDE. Identify the primary algorithm it implements and respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: {"detectedAlgorithm": "<name, e.g. Dijkstra's Shortest Path, QuickSort, Bubble Sort, Binary Search - or \\"Not identified\\" if the file implements no recognizable algorithm>", "paradigm": "<e.g. Greedy, Divide and Conquer, Dynamic Programming, Brute Force, Backtracking - or \\"Unknown\\">", "timeComplexity": "<Big-O in terms of the code's own variable names, e.g. O(N log N) - or \\"Unknown\\">", "spaceComplexity": "<Big-O - or \\"Unknown\\">", "criticalLines": [{"line": <1-based line number from the numbered source below>, "purpose": "<short explanation of that line's operational importance>"}] (at most 8, only the lines that matter most - never invent a line number outside the file), "simulationModel": {"type": "\\"array\\", \\"graph\\", \\"tree\\" or \\"none\\"", "initialState": <for "array": the JSON array of starting values the code operates on; for "graph"/"tree": {"nodes": [...], "edges": [{"from":..., "to":..., "weight":...}]}; use null with type "none" if nothing in the file has a structure worth animating>}, "steps": [<at most 60 steps tracing the algorithm's own execution against simulationModel.initialState, each either {"op":"compare","indices":[i,j],"line":<n>}, {"op":"swap","indices":[i,j],"line":<n>}, {"op":"set","indices":[i],"value":<v>,"line":<n>}, {"op":"visit","node":"<id>","line":<n>}, or {"op":"edge","from":"<id>","to":"<id>","weight":<w>,"line":<n>} - omit "steps" entirely (or leave it empty) if simulationModel.type is "none" or you cannot trace real execution>]}. Base every field only on what the code in front of you actually does; never invent an algorithm, complexity, or step the code does not support."""

# The model is not allowed to silently turn a guessed trace into a different
# algorithm: array indices are zero-based, and a dynamic/unknown input means
# no simulation rather than an invented static example. lab_analysis validates
# this contract before the canvas sees the response.
LAB_BLUEPRINT_SYSTEM += """ Use the complete numbered source, including helper functions and the entry point. For simulationModel.type "array", initialState must be a literal array the source actually creates, and every indices value is zero-based and must refer to that array. Only emit a step when that operation really occurs in the shown execution; do not fabricate a canonical textbook trace. If inputs are read from stdin, generated randomly, or otherwise cannot be known from the source, use type "none". For graph/tree steps, only reference nodes and edges present in initialState. If you cannot trace the concrete execution confidently, leave steps empty."""

# Replaces the old hardwired Java Coach curriculum (a fixed 8-stage lesson ladder with
# regex "structure checks") with a live chat grounded in the student's actual code and
# actual run output, instead of the same canned lessons for everyone.
LAB_COACH_SYSTEM = """You are FRIDAY, a live coding coach sitting beside a student's local Python/Java/C IDE (the Software Lab). You see the exact numbered source of the file they have open and, when available, the tail of their most recent Run's console output (stdout/stderr/compiler errors). Answer only from what is actually in front of you - the shown source and run output - never invent code, output, or errors that are not there. Keep answers short and concrete: 2-5 sentences for a direct question, 1-3 sentences for an unprompted live observation. When asked to explain, explain what the code actually does, in the order it executes. When asked what happens next or what to write next, recommend one concrete, small next step grounded in the code's current state, not a full rewritten solution - the student is learning to write it themselves. When the run output shows a compiler or runtime error, explain what it means and point at the specific line responsible. For an unprompted observation you will be told which lines are new or changed since you last looked, or that nothing changed - stay strictly on that delta and never re-explain or repeat feedback on lines you have already covered in the recent conversation below. Use Markdown; put inline mathematics inside \\( ... \\) and display mathematics inside \\[ ... \\] if any is needed."""


def _changed_line_ranges(old_text: str, new_text: str) -> str:
    """Line ranges in new_text that differ from old_text, e.g. "3-5, 9".

    Lets the Lab Coach's unprompted commentary (mode "live"/"run") point at what
    actually changed since it last looked, instead of re-reading - and re-explaining -
    lines it has already covered.
    """
    matcher = difflib.SequenceMatcher(
        a=old_text.splitlines(), b=new_text.splitlines(), autojunk=False)
    ranges = [(j1 + 1, j2) for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
              if tag != "equal" and j2 > j1]
    return ", ".join(str(a) if a == b else "{}-{}".format(a, b) for a, b in ranges)

# ureca.py validates every field below before it reaches the store - draft
# text is a starting point for the student to edit, never a finished
# proposal, so the model is told to stay modest and never invent specifics.
# Runs on the "scholar" tier (see ai_provider.TIERS) rather than "chat": this
# is a single deep autonomous pass over the whole proposal, not a quick
# reformat, so it gets the slower, stronger rung and a longer deadline.
URECA_DRAFT_SYSTEM = """You are doing autonomous research for an NTU undergraduate who gave you a one-line idea and needs a first-draft URECA (Undergraduate Research Experience on CAmpus) project proposal. URECA is NTU's self-proposed undergraduate research programme: a student drafts a proposal, a faculty supervisor accepts and registers it, and the student spends the August-to-June academic year on the project, finishing with an abstract, a poster and a final paper; consumable spending is capped at $500. Think like a researcher scoping a feasible undergraduate project, not a copywriter padding out a summary: reason about what makes this idea tractable in about 11 months, what a realistic method looks like, and what could plausibly go wrong or be out of scope. Respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: {"background": "<2-4 sentences: the problem or gap and why it matters, grounded only in what the student described>", "objectives": "<2-4 concrete, checkable research objectives, written as short sentences>", "methodology": "<3-5 sentences: a specific, realistic approach an undergraduate could carry out over about 11 months - name concrete methods, tools or data sources where you can>", "outcomes": "<2-3 sentences: the expected contribution, tied to URECA's own deliverables of an abstract, poster and final paper>", "budgetNotes": "<1-3 sentences: what consumables or small costs this plausibly needs, staying within the $500 cap - say \\"No consumables anticipated\\" if none>", "timelineNotes": "<2-4 sentences: a rough month-by-month or phase-by-phase plan spanning August to June>"}. Write a first draft the student can edit, not a finished document - stay concrete, and never invent citations, data, prior results, or specifics the student did not mention. It is fine, and expected, to reason from general domain knowledge about feasibility and method - that is the research; just do not fabricate sources or claim specific prior findings you cannot support."""

# Research suggestions must be driven by the profile supplied in the Research tab.
# No programme, year, department, course history, or supervisor preference is
# assumed when a new user installs the project.
RESEARCH_SUGGEST_ATTEMPTS = 2
RESEARCH_SUGGEST_SYSTEM = """You propose URECA (Undergraduate Research Experience on CAmpus) project ideas for an undergraduate, tailored only to the programme, year, field of study, and interests supplied in the user's research profile. URECA is NTU's self-proposed undergraduate research programme: a student drafts a proposal, a faculty supervisor accepts and registers it, and the student spends the August-to-June academic year on the project, finishing with an abstract, a poster and a final paper; consumable spending is capped at $500. Do not assume a specific programme, year, department, course list, prior knowledge, or research experience. If the profile is incomplete, keep ideas accessible and state what background would need to be learned. Propose ideas that pose a non-trivial, answerable research question and combine at least two skills such as proof and counterexample, mathematical modelling, algorithm design and complexity analysis, numerical implementation, or experimental evaluation. Make the challenge visible in the topic sentence through a concrete method, comparison, conjecture, or measurable criterion - not through buzzwords or an oversized application. Scope each project so the student can learn missing background, build a defensible baseline, investigate one focused extension, and produce a meaningful result in about 11 months; a good idea should be demanding but finishable, not a survey, a generic app, a toy coding exercise, training a large model from scratch, or an open-ended attempt at a famous unsolved problem. If the student supplied interests or keywords, use them as the anchor for every idea - each one should visibly grow out of a stated interest, and none should drift onto an unrelated theme. Aim for a spread across the theory-to-application range while keeping every idea within the supplied level and field. Use concrete methods and domain terms so each idea can be matched against the local faculty catalogue; never invent or name a supervisor unless one is supplied to you below as the target supervisor. If a target supervisor is supplied, with their own stated research interests, every idea must sit squarely inside that field and direction - reuse their own terms and methods, extend or apply their actual research rather than a generic idea decorated with their keywords, and write each idea so it would read to them as recognizably their kind of work. This is to maximise the realistic chance that supervisor would accept and register the proposal, so do not drift onto an adjacent-sounding but different subfield just for variety; the required spread across the theory-to-application range still applies within their field. Respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: a JSON array of 8 to 10 objects, each {"title": "<a short, concrete project title, under 12 words>", "topic": "<one sentence pitching the idea, specific enough to hand straight to a research pass - not a vague theme>"}. Make the ideas genuinely different from each other in approach or subfield, and do not pad the list with near-duplicates to reach the count - if the profile only genuinely supports fewer strong ideas, return fewer. Never invent a specific supervisor, dataset, or prior result; keep each idea grounded in the student's stated profile."""
OMNIROUTE_WATCH_SECONDS = 15


def research_suggest_prompt(state: "State", keywords: str,
                            professor: Optional[Dict] = None) -> str:
    """Build one reproducible prompt before the slow provider call starts.

    ``professor`` - when the student picked one from the faculty catalogue
    dropdown - is a public faculty_db record, so its research interests come
    from the same audited, provenance-tagged source as everything else the
    Research tab shows; nothing here is invented on the fly. Keywords and a
    professor are independent inputs: either, both, or neither may be set.
    """
    prompt = "Student profile: {}".format(state.profile.summary())
    codes = course_codes(state)
    if codes:
        prompt += "\nCourses this semester: {}".format(", ".join(codes))
    if keywords:
        prompt += "\nInterests / keywords to anchor ideas in: {}".format(keywords)
    if professor:
        prompt += "\nTarget supervisor: {} ({}, {}).".format(
            professor.get("name", ""), professor.get("title", ""),
            professor.get("school", ""))
        interests = professor.get("research_interests") or []
        if interests:
            prompt += "\nTheir stated research interests: {}.".format(
                "; ".join(interests))
        summary = (professor.get("profile_summary") or "").strip()
        if summary:
            prompt += "\nTheir profile summary: {}".format(summary[:600])
    return prompt + "\n---\nReport the JSON array of proposed ideas now."


def research_tab_backend(state: "State") -> Optional[str]:
    """Backend for the Research tab's scholar-tier passes (topic suggestions and
    the autonomous proposal draft).

    These are quality-first calls, so they are pinned to Claude Opus directly -
    the signed-in Claude CLI, or an Anthropic API key - rather than being left to
    route through the free OmniRoute tier. A backend the user has explicitly
    chosen still wins; the direct Claude route is only given up when neither the
    CLI login nor an API key is available, and then OmniRoute (which itself walks
    the scholar ladder to claude-opus-5) is the last resort.
    """
    if state.research_backend:
        return state.research_backend
    return research_mod.resolve_claude_backend() or research_mod.BACKEND_OMNIROUTE


def generate_research_suggestions(state: "State", prompt: str, keywords: str,
                                  professor: Optional[Dict] = None) -> Dict:
    """Run the provider pass and return the complete UI payload.

    A malformed answer is cheap to retry, but a provider failure is not: the
    scholar tier can have a minutes-long deadline, so retrying it would make one
    user action needlessly occupy two long-running workers.
    """
    suggestions, result, last_error = [], None, None
    for _attempt in range(RESEARCH_SUGGEST_ATTEMPTS):
        try:
            result = ai_provider.complete_tier(
                "scholar", prompt, RESEARCH_SUGGEST_SYSTEM,
                preferred=research_tab_backend(state), max_tokens=4500,
                download_root=state.download_root)
        except ai_provider.ProviderError as exc:
            last_error = exc
            result = None
            break
        suggestions = ureca_mod.parse_suggest_response(result.get("text") or "")
        if suggestions:
            break
    if result is None:
        raise ai_provider.ProviderError(str(last_error or "No research suggestions returned"))
    if professor:
        # The chosen professor is a guaranteed lead for every idea (that is
        # the point of picking them), with any other catalogue overlap
        # surfaced alongside it rather than hidden.
        faculty_matches = []
        for item in suggestions:
            others = [m for m in faculty_db.match_topic(
                          item.get("title", ""), item.get("topic", ""), keywords)
                      if m.get("id") != professor.get("id")]
            faculty_matches.append([professor] + others[:2])
    else:
        faculty_matches = faculty_db.match_suggestions(suggestions, keywords)
    return {
        "suggestions": suggestions,
        "faculty_matches": faculty_matches,
        "faculty_catalogue": faculty_db.metadata(),
        "backend": result.get("backend"),
        "model": result.get("model"),
        "professor": professor,
    }


def do_research_suggest(state: "State", job_id: str, prompt: str, keywords: str,
                        professor: Optional[Dict] = None):
    """Complete one suggestion job without holding the HTTP request open."""
    try:
        result = generate_research_suggestions(state, prompt, keywords, professor)
    except Exception as exc:  # noqa: BLE001 - hand the provider error to the UI
        with state.lock:
            job = state.research_suggest_job
            if job and job.get("id") == job_id:
                job.update({"status": "error", "done": True, "error": str(exc)})
        state.note("Research topic suggestions failed: {}".format(exc))
        return
    with state.lock:
        job = state.research_suggest_job
        if job and job.get("id") == job_id:
            job.update({"status": "complete", "done": True, **result})

TODO_BACKENDS = ("auto", research_mod.BACKEND_OMNIROUTE, research_mod.BACKEND_CLI)
TODO_MODEL_OPTIONS = {
    "auto": (("auto", "Auto model"),),
    research_mod.BACKEND_OMNIROUTE: (
        ("auto", "Auto route"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("claude-opus-5", "Claude Opus 5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("gemini-3.1-pro", "Gemini 3.1 Pro"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ),
    research_mod.BACKEND_CLI: (
        ("auto", "Auto model"),
        ("claude-opus-5", "Claude Opus 5"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ),
}
TODO_MODELS = tuple(dict.fromkeys(
    value for options in TODO_MODEL_OPTIONS.values() for value, _label in options))
TODO_SYSTEM_PROMPT = """You are an academic task assistant for an NTU undergraduate. \
Answer the active query directly and practically. If it is an assessment deadline, \
turn it into a concise preparation and completion plan; do not invent requirements \
that are not in the query. Use Markdown and stay under 500 words. Put inline mathematics \
inside \\( ... \\) and display mathematics inside \\[ ... \\] so the dashboard can \
typeset it; do not use single dollar signs as math delimiters."""


def todo_ai_choice(requested: str = "auto"):
    """Resolve the query pipeline without ever selecting the Anthropic API."""
    if requested == research_mod.BACKEND_OMNIROUTE:
        return requested, ai_provider.status(requested)
    if requested == research_mod.BACKEND_CLI:
        return requested, research_mod.status(requested)
    omni = ai_provider.status(research_mod.BACKEND_OMNIROUTE)
    if omni.get("ready"):
        return research_mod.BACKEND_OMNIROUTE, omni
    cli = research_mod.status(research_mod.BACKEND_CLI)
    return (research_mod.BACKEND_CLI if cli.get("ready") else None), cli


def todo_model_options():
    """Add OmniRoute's live automatic routes beneath the Auto model choice."""
    options = {backend: list(rows) for backend, rows in TODO_MODEL_OPTIONS.items()}
    # Called from snapshot() on every /api/state poll: keep the probe short and
    # lean on models()' own result/failure caching so a slow gateway cannot stall
    # the HUD. A cold cache pays this once, then the backoff takes over.
    routes = [model for model in omniroute_provider.models(timeout=4.0)
              if model.startswith("auto/")]
    options["auto"].extend(
        (model, "Auto · " + model[5:].replace("-", " ").replace(":", " · ").title())
        for model in routes)
    return options


class State:
    """Everything the UI needs, guarded by a lock since downloads run in a thread."""

    def __init__(self, download_root: str, prefer_rest: bool = True,
                 drive_folder: str = drive.DEFAULT_ROOT_FOLDER, move: bool = True,
                 scope: str = api_mod.SCOPE_SEMESTER,
                 transcribe_model: str = transcribe_mod.DEFAULT_MODEL,
                 inbound_db: Optional[str] = None):
        self.lock = threading.RLock()
        # The dashboard is loopback-bound, but loopback is not an authorization
        # boundary: another local process can still issue POST/PUT requests.
        # The browser receives this secret as a SameSite cookie from "/".
        self.local_secret = uuid.uuid4().hex + uuid.uuid4().hex
        self.download_root = download_root
        self.prefer_rest = prefer_rest
        self.scope = scope
        self.transcribe_model = transcribe_model
        self.transcribing = False
        self.transcribe_progress: Dict[str, Any] = {
            "done": 0, "total": 0, "current": "", "pct": 0
        }
        self._transcriber: Optional[transcribe_mod.Transcriber] = None
        # One Compendium generation at a time, like transcribing/pushing - a 60-120s
        # job with its own worker thread. summary_job holds the single most recent
        # job's live status; the job id lets a poll from a stale tab notice it is
        # looking at an older run rather than silently showing the wrong one.
        self.summarizing = False
        self.summary_job: Optional[Dict[str, Any]] = None
        # Same one-job-at-a-time, most-recent-status shape as summary_job, for
        # grading a student's uploaded worked tutorial against its solution_pairs
        # match. See do_grade_tutorial.
        self.grading = False
        self.grading_job: Optional[Dict[str, Any]] = None
        self.media_survey: List[Dict] = []
        self.identity: Optional[Dict] = None
        self.drive_folder = drive_folder
        self.move = move
        self.token: Optional[str] = auth.load_token()
        # Incremented whenever a token is installed so late worker results cannot
        # overwrite state belonging to a newer browser session.
        self.session_generation = 1
        self.session_rejected = False
        self.courses: List[Dict] = []
        self.plan: List[Dict] = []
        self.skipped: List[str] = []
        self.scanning = False
        self.scan_errors: List[str] = []
        self.downloading = False
        self.pushing = False
        self.drive_listing = False
        # OAuth consent flow (dashboard "Connect Google Drive" button) in flight.
        self.drive_linking = False
        self.drive_link_error = ""
        self.pulling = False
        self.drive_files: List[Dict] = []
        # Docs found by "Search my Drive" (outside the app's own mirrored tree),
        # keyed by id. do_drive_list's inventory refresh replaces drive_files
        # wholesale, so these are kept separately and re-merged in every time -
        # otherwise the next background "Index" run would silently drop them.
        self.external_drive_files: Dict[str, Dict] = {}
        # snapshot() derives two O(n) views from the inventory (transcribable
        # media, practice/solution pairs) on every ~1.2s /api/state poll. The
        # inventory is only ever replaced wholesale (never mutated in place), so
        # these are memoised against the exact list object and only recomputed
        # when an "Index"/pull/search actually swaps in a new one.
        self._drive_view_cache: Dict[str, Any] = {}
        self.pull_progress: Dict[str, Any] = {
            "done": 0, "total": 0, "current": "", "pct": 0
        }
        self.log: List[str] = []
        self.progress: Dict[str, Any] = {
            "done": 0, "total": 0, "current": "", "bytes": 0, "pct": 0
        }
        self.push_progress: Dict[str, Any] = {
            "done": 0, "total": 0, "current": "", "pct": 0
        }
        self.ledger = Ledger(download_root)
        self.cache = ContentCache(download_root)
        self.schedule = schedule_mod.Schedule(download_root)
        self.todo = todo_mod.Queue(download_root)
        self.todo_researching: set = set()
        self.todo_research_errors: Dict[str, str] = {}
        self.announcements = announcements_mod.Feed(download_root)
        self.chat_memory = ChatMemory(download_root)
        self.notebook = Notebook(download_root)
        self.lab_repos = lab_mod.LabRepos(download_root)
        self.lab_jobs = lab_mod.JobManager()
        self.ureca = ureca_mod.Store(download_root)
        self.profile = profile_mod.Store(download_root)
        self.saved_topics = saved_topics_mod.Store(download_root)
        self.focus = focus_mod.Store(download_root)
        # Research suggestions use the same background-job handoff as long
        # Compendium generations: the scholar provider can exceed the browser's
        # normal 20-second request deadline.
        self.research_suggest_job: Optional[Dict[str, Any]] = None
        self.announcements_syncing = False
        self.unified_syncing = False
        self.unified_sync_error = ""
        self.announcement_errors: List[str] = []
        self.announcements_summarizing = False
        self.announcement_summary_error = ""
        self.announcement_schedule_error = ""
        # None = pick whatever is usable, preferring the CLI's own login over a key.
        # Still read by todo research, the materials chat, and the Drive learning
        # chat - the R&D board was one consumer among several, not the owner of this.
        self.research_backend: Optional[str] = None
        # An explicit flag store to read. When unset, the reader falls back to
        # this project's own triage.db beneath the sync folder.
        self.inbound_db: Optional[str] = inbound_db
        self.inbound_syncing = False
        self.inbound_error = ""
        # Which academic calendar to resolve teaching weeks against.
        self.semester_key = semester_mod.format_semester(
            semester_mod.current_semester())

    def session_snapshot(self):
        """Return the token and generation atomically for a background pipeline."""
        with self.lock:
            return self.token, self.session_generation

    def install_token(self, token: str):
        """Install a fresh browser session and clear state owned by the old one."""
        with self.lock:
            self.token = token
            self.session_generation += 1
            self.session_rejected = False
            self.identity = None
            self.courses = []
            self.plan = []
            self.skipped = []
            self.scan_errors = []
            self.unified_sync_error = ""
            self.unified_syncing = False
            self.scanning = False
            self.downloading = False
            self.progress = {
                "done": 0, "total": 0, "current": "", "bytes": 0, "pct": 0
            }
            self.announcements_syncing = False
            self.announcement_errors = []
            self.announcement_schedule_error = ""
            self.announcement_summary_error = ""
            self.inbound_error = ""
            return self.session_generation

    def session_current(self, token: str, generation: int) -> bool:
        with self.lock:
            return (self.token == token
                    and self.session_generation == generation
                    and not self.session_rejected)

    def reject_session(self, token: str, generation: int, message: str) -> bool:
        """Stop the current Blackboard pipelines after a confirmed 401."""
        with self.lock:
            if self.token != token or self.session_generation != generation:
                return False
            self.session_rejected = True
            self.session_generation += 1
            self.identity = None
            self.courses = []
            self.plan = []
            self.skipped = []
            self.scan_errors = []
            self.announcement_errors = []
            self.unified_sync_error = message
            self.unified_syncing = False
            self.announcements_syncing = False
            return True
    def rebind_root(self, download_root: str):
        """Point every root-anchored store at a new download folder.

        The ledger, cache and the various boards each opened a file under the old
        root when this State was built. Reassigning ``download_root`` alone left them
        reading the old location while plans were keyed to the new one - so every
        archived file looked new and the whole course was downloaded again.

        Takes ``self.lock`` itself; do not call it while already holding the lock.
        """
        root = os.path.abspath(download_root)
        with self.lock:
            if root == self.download_root:
                return
            self.download_root = root
            self.ledger = Ledger(root)
            self.cache = ContentCache(root)
            self.schedule = schedule_mod.Schedule(root)
            self.todo = todo_mod.Queue(root)
            self.announcements = announcements_mod.Feed(root)
            self.chat_memory = ChatMemory(root)
            self.notebook = Notebook(root)
            self.lab_repos = lab_mod.LabRepos(root)
            self.ureca = ureca_mod.Store(root)
            self.profile = profile_mod.Store(root)
            self.saved_topics = saved_topics_mod.Store(root)
            self.focus = focus_mod.Store(root)
            # The plan describes files under the previous root; it means nothing here.
            self.plan = []
            self.media_survey = []
        self.note("Sync folder set to {}".format(root))

    def _schedule_snapshot(self) -> Dict:
        """Everything the Temporal Protocol panel needs, resolved to today."""
        # Course-document scanning may update schedule.json outside this long-lived
        # dashboard process. Pick that up before rendering instead of letting the
        # next announcement sync overwrite it with stale in-memory state.
        self.schedule.reload_if_changed()
        now = datetime.now()
        sem = self.semester_key
        teaching_week = cal_mod.week_of(now.date(), sem)
        monday = now.date() - schedule_mod.timedelta(days=now.weekday())
        semester = cal_mod.get(sem)
        teaching_weeks = []
        if semester:
            for number in range(1, cal_mod.TOTAL_TEACHING_WEEKS + 1):
                week_monday = semester.monday_of(number)
                if week_monday is None:
                    continue
                teaching_weeks.append({
                    "week": number,
                    "monday": week_monday.isoformat(),
                    "schedule": self.schedule.dynamic_week(week_monday, number),
                })
        return {
            "count": len(self.schedule),
            "imported_at": self.schedule.imported_at,
            "semester": self.schedule.semester,
            # The upload prompt is only warranted when there is nothing stored, or
            # when what is stored belongs to a semester that has since rolled over.
            # An unstamped schedule (imported before semesters were recorded) counts
            # as current rather than stale, so it does not nag on every launch.
            "needs_upload": bool(
                not len(self.schedule)
                or (self.schedule.semester and self.schedule.semester != sem)),
            "current_semester": sem,
            # Only sessions that actually run this teaching week: "Wk2-13" must not
            # show up in week 1, and an alternating lab must not show every week.
            "week": self.schedule.dynamic_week(monday, teaching_week),
            "teaching_weeks": teaching_weeks,
            "all_week": self.schedule.dynamic_week(monday),
            "changes": list(self.schedule.overrides),
            "important_dates": self.schedule.assessment_timeline(sem),
            "teaching_week": teaching_week,
            "phase": cal_mod.phase_of(now.date(), sem),
            "exams": self.schedule.exams,
            "today": now.strftime("%a %d %b %Y"),
            "day": schedule_mod.DAYS[now.weekday()],
            "clock": now.strftime("%H:%M:%S"),
        }

    def refresh_identity(self, token: Optional[str] = None, generation: Optional[int] = None):
        """Look up identity without allowing an old worker to clobber a new session."""
        if token is None or generation is None:
            token, generation = self.session_snapshot()
        if not token:
            with self.lock:
                if self.session_generation == generation:
                    self.identity = None
            return None
        try:
            from intuition import rest
            who = rest.get_me(token)
        except Exception:  # noqa: BLE001 - identity is cosmetic, never block on it
            who = None
        with self.lock:
            if self.token == token and self.session_generation == generation:
                self.identity = who
        return who

    def refresh_media(self):
        """Re-read which staged media has a transcript, and where it came from."""
        survey = (transcribe_mod.survey(self.download_root)
                  if os.path.isdir(self.download_root) else [])
        with self.lock:
            self.media_survey = survey
        return survey

    def note(self, message: str):
        with self.lock:
            self.log.append(message)
            del self.log[:-200]

    def snapshot(self) -> Dict:
        # Provider probes can touch local gateways or CLI processes. Resolve them
        # outside the state lock so /api/token and the other controls stay responsive.
        ai_status = ai_provider.status(self.research_backend)
        _todo_backend, todo_ai_status = todo_ai_choice()
        # Schedule reload/expansion reads disk and resolves every teaching week.
        # Do it before taking the state lock so a slow calendar or a large schedule
        # cannot stall token installation and worker progress updates.
        schedule_snapshot = self._schedule_snapshot()
        # The Drive-derived views are O(n) over the whole cached inventory, and
        # the announcement / inbound / todo snapshots each touch disk or a SQLite
        # file. snapshot() serves /api/state, which the dashboard polls every
        # 1.2s, so holding self.lock through this much work starves the one-shot
        # lock acquisition on other endpoints - a large Drive index was pushing
        # /api/drive/tree past its 20s client timeout while it waited behind a
        # run of these polls. Compute everything that does not need self.lock
        # up front - grabbing only the current inventory list, which is always
        # swapped in wholesale and never mutated in place - then hold the lock
        # just long enough to read the plain state fields.
        with self.lock:
            drive_files = self.drive_files
        drive_file_count = len(drive_files)
        cache = self._drive_view_cache
        if cache.get("src") is not drive_files:
            cache = {
                "src": drive_files,
                "media": transcribe_mod.classify_drive_media(drive_files),
                "pairs": solution_pairs_mod.pair_practice_with_solutions(drive_files),
            }
            self._drive_view_cache = cache
        drive_media = self.media_survey + cache["media"]
        drive_solution_pairs = cache["pairs"]
        announcements_snapshot = self.announcements.snapshot()
        inbound_snapshot = inbound_mod.snapshot(
            inbound_mod.resolve_path(self.download_root, self.inbound_db))
        todo_snapshot = self.todo.snapshot()
        focus_snapshot = self.focus.snapshot()
        todo_model_opts = {backend: [{"value": value, "label": label}
                                    for value, label in options]
                           for backend, options in todo_model_options().items()}
        with self.lock:
            token_expiry = None
            if self.token:
                expires = auth.expires_at(self.token)
                token_expiry = expires
            return {
                "download_root": self.download_root,
                # Which copy of the page is this? A frozen build serves the
                # snapshot it was packaged with, so the footer says so.
                "build": build_info.summary(),
                "prefer_rest": self.prefer_rest,
                "scope": self.scope,
                "semester": semester_mod.format_semester(
                    semester_mod.current_semester()),
                "has_token": bool(self.token),
                "session_rejected": self.session_rejected,
                "identity": self.identity,
                "token_expires": token_expiry,
                "courses": self.courses,
                "plan": self.plan,
                "skipped": self.skipped,
                "summary": summarize(self.plan),
                "scanning": self.scanning,
                "unified_sync": {"syncing": self.unified_syncing,
                                 "error": self.unified_sync_error},
                "scan_errors": list(self.scan_errors),
                "downloading": self.downloading,
                "pushing": self.pushing,
                "pulling": self.pulling,
                "transcribing": self.transcribing,
                "transcribe_progress": dict(self.transcribe_progress),
                "summarizing": self.summarizing,
                "summary_job": dict(self.summary_job) if self.summary_job else None,
                "grading": self.grading,
                "grading_job": dict(self.grading_job) if self.grading_job else None,
                "transcribe": {
                    "model": self.transcribe_model,
                    # Local survey plus whatever the already-cached Drive index
                    # (self.drive_files, refreshed by "Index"/do_drive_list) turns
                    # out to hold - move mode deletes a video locally once it is
                    # archived, so that is the only place left to detect it.
                    # Classified above, outside the lock.
                    "media": drive_media,
                },
                "progress": dict(self.progress),
                "push_progress": dict(self.push_progress),
                "pull_progress": dict(self.pull_progress),
                "drive": {
                    "folder": self.drive_folder,
                    "move": self.move,
                    "configured": drive.credentials_present(),
                    "linked": drive.token_present(),
                    "linking": self.drive_linking,
                    "link_error": self.drive_link_error,
                    "archived": len(self.ledger),
                    "listing": self.drive_listing,
                    "file_count": drive_file_count,
                    # Recomputed from the already-cached inventory above, outside
                    # the lock - no extra Drive calls.
                    "solution_pairs": drive_solution_pairs,
                },
                "schedule": schedule_snapshot,
                "announcements": dict(announcements_snapshot,
                                      syncing=self.announcements_syncing,
                                      errors=list(self.announcement_errors),
                                      summarizing=self.announcements_summarizing,
                                      summary_error=self.announcement_summary_error,
                                      schedule_error=self.announcement_schedule_error,
                                      ai=ai_status),
                "inbound": dict(inbound_snapshot,
                                syncing=self.inbound_syncing,
                                error=self.inbound_error),
                "todo": dict(todo_snapshot,
                             busy=sorted(self.todo_researching),
                             errors=dict(self.todo_research_errors),
                             ai=todo_ai_status,
                             model_options=todo_model_opts,
                             backends=list(TODO_BACKENDS)),
                "focus": focus_snapshot,
                "log": list(self.log[-40:]),
            }


def _pipeline_session(state: State):
    if hasattr(state, "session_snapshot"):
        return state.session_snapshot()
    return getattr(state, "token", None), 1

def _pipeline_session_current(state: State, token: str, generation: int) -> bool:
    if hasattr(state, "session_current"):
        return state.session_current(token, generation)
    return getattr(state, "token", None) == token

def do_scan(state: State, course_ids: List[str], token: Optional[str] = None,
            generation: Optional[int] = None):
    """Fetch content trees while ignoring results from an older session."""
    if token is None or generation is None:
        token, generation = _pipeline_session(state)
    try:
        state.cache.reset_stats()
        selected = [c for c in state.courses if c["id"] in course_ids]
        combined: List[Dict] = []
        skipped: List[str] = []
        errors: List[str] = []

        for course in selected:
            state.note("Scanning {}".format(course["name"]))
            try:
                tree = get_download_dir(
                    token,
                    course["name"],
                    course["id"],
                    prefer_rest=state.prefer_rest,
                    cache=state.cache,
                )
            except Exception as e:  # noqa: BLE001 - surface any backend failure in the UI
                message = "{}: {}".format(course["name"], e)
                errors.append(message)
                state.note("  failed: {}".format(message))
                continue

            try:
                entries = build_plan(tree, state.download_root, ledger=state.ledger)
            except Exception as e:  # noqa: BLE001 - one course must not erase the rest
                message = "{}: could not build sync plan: {}".format(course["name"], e)
                errors.append(message)
                state.note("  failed: {}".format(message))
                continue
            for entry in entries:
                entry["course"] = course["name"]
            combined.extend(entries)

        # Across every scanned course, so a record cannot be claimed twice. Only
        # matters for archives predating resource ids; normally a no-op.
        recovered = recover_restructured(combined, state.ledger, state.download_root)
        if recovered:
            state.ledger.save()
            state.note(
                "Recovered {} archived file(s) that moved to a new folder in Learn "
                "- not re-downloading them".format(len(recovered))
            )

        if not _pipeline_session_current(state, token, generation):
            state.note("Scan result discarded: Blackboard session changed")
            return
        with state.lock:
            state.plan = combined
            state.skipped = skipped
            state.scan_errors = errors
        state.cache.save()
        state.refresh_media()
        counts = summarize(combined)
        st = state.cache.stats()
        state.note(
            "Scan complete: {} new, {} updated, {} on disk, {} archived "
            "({} cached, {} refetched){}".format(
                counts["new"], counts["updated"], counts["current"],
                counts["archived"], st["hits"], st["misses"],
                "; {} course(s) failed".format(len(errors)) if errors else ""
            )
        )
    finally:
        with state.lock:
            if _pipeline_session_current(state, token, generation):
                state.scanning = False


def do_announcement_sync(state: State, token: Optional[str] = None,
                          generation: Optional[int] = None):
    if token is None or generation is None:
        token, generation = state.session_snapshot()
    if not token or not state.session_current(token, generation):
        return
    with state.lock:
        courses = list(state.courses)
    try:
        errors = state.announcements.sync(token, courses)
        if not state.session_current(token, generation):
            return
        with state.lock:
            state.announcement_errors = errors
        state.note("Announcements synced: {} across {} course(s){}".format(
            len(state.announcements.items), len(state.courses),
            "; {} failed".format(len(errors)) if errors else ""))
        # Fold in near-duplicates the deterministic cross-post collapse can't see
        # (same notice re-posted with light edits). Cached by cluster fingerprint,
        # so this only reaches the model when the feed actually changed.
        try:
            folded = state.announcements.resolve_duplicates_with_ai(state.research_backend)
            if folded:
                state.note("Announcements de-duplicated by AI: {} folded".format(folded))
        except Exception as exc:  # never let dedupe break a good sync
            state.note("Announcement AI de-duplication skipped: {}".format(exc))
        # Schedule extraction is a small, bounded classification job. Use the verified
        # local Claude login directly: a wedged OmniRoute can accept TCP connections
        # while never answering inference, which would double the scan latency before
        # falling back here anyway.
        _schedule_backend = research_mod.BACKEND_CLI
        schedule_ai = research_mod.status(_schedule_backend)
        if schedule_ai.get("ready"):
            try:
                events = state.announcements.detect_important_dates(
                    preferred=_schedule_backend)
                event_count = state.schedule.set_announcement_important_dates(events)
                state.schedule.save()
                state.note("Temporal Protocol updated from announcements: "
                           "{} important date(s)".format(event_count))
            except Exception as exc:
                with state.lock:
                    state.announcement_schedule_error = str(exc)
                state.note("Announcement important-date detection failed: {}".format(exc))
        if state.schedule.sessions and schedule_ai.get("ready"):
            try:
                try:
                    changes = state.announcements.detect_schedule_changes(
                        state.schedule.sessions, preferred=_schedule_backend)
                except Exception:
                    # A locally installed OmniRoute can be listening but unhealthy.
                    # Retry through the logged-in Claude CLI, never Anthropic API.
                    if (_schedule_backend != research_mod.BACKEND_OMNIROUTE
                            or not research_mod.status(
                                research_mod.BACKEND_CLI).get("ready")):
                        raise
                    state.note("OmniRoute unhealthy; retrying schedule detection via Claude CLI")
                    changes = state.announcements.detect_schedule_changes(
                        state.schedule.sessions,
                        preferred=research_mod.BACKEND_CLI)
                count = state.schedule.set_announcement_overrides(changes)
                state.schedule.save()
                with state.lock:
                    state.announcement_schedule_error = ""
                state.note("Temporal Protocol updated from announcements: {} change(s)"
                           .format(count))
            except Exception as exc:
                with state.lock:
                    state.announcement_schedule_error = str(exc)
                state.note("Announcement schedule detection failed: {}".format(exc))
        # Feed.sync invalidates the cached TL;DR only when its source set changes.
        # Refresh it here so the visible ribbon is useful without a second click.
        if state.announcements.items and not state.announcements.summary:
            try:
                summary = state.announcements.summarize(state.research_backend)
                with state.lock:
                    state.announcement_summary_error = ""
                state.note("Announcement TL;DR refreshed by {} {}".format(
                    summary.get("backend", "AI"), summary.get("model", "")))
            except Exception as exc:  # announcements remain usable without AI
                with state.lock:
                    state.announcement_summary_error = str(exc)
                state.note("Announcement TL;DR refresh failed: {}".format(exc))
    finally:
        with state.lock:
            if state.token == token and state.session_generation == generation:
                state.announcements_syncing = False


def seconds_until_next_announcement_sync(now: Optional[datetime] = None) -> float:
    """Return the delay to the next local 07:00 or 23:00 sync window."""
    now = now or datetime.now()
    targets = [now.replace(hour=hour, minute=0, second=0, microsecond=0)
               for hour in ANNOUNCEMENT_SYNC_HOURS]
    target = next((candidate for candidate in targets if candidate > now), None)
    if target is None:
        target = (now + timedelta(days=1)).replace(
            hour=ANNOUNCEMENT_SYNC_HOURS[0], minute=0, second=0, microsecond=0)
    return max(1.0, (target - now).total_seconds())


def run_scheduled_announcement_sync(state: State):
    """Refresh courses and announcements for one stable Blackboard session."""
    if hasattr(state, "session_snapshot"):
        token, generation = state.session_snapshot()
    else:  # lightweight scheduler test doubles and older integrations
        token, generation = getattr(state, "token", None), 1
    if not token:
        state.note("Scheduled announcement sync skipped: no Blackboard session")
        return
    with state.lock:
        courses_empty = not state.courses
    if courses_empty:
        try:
            courses = get_courses(
                token, prefer_rest=state.prefer_rest, scope=state.scope,
                download_root=state.download_root)
        except auth.AuthenticationError as exc:
            if _is_session_rejection(exc):
                state.reject_session(token, generation, str(exc))
            state.note("Scheduled announcement sync could not load courses: {}".format(exc))
            return
        except Exception as exc:  # noqa: BLE001
            state.note("Scheduled announcement sync could not load courses: {}".format(exc))
            return
        if not state.session_current(token, generation):
            return
        with state.lock:
            state.courses = [{"name": name, "id": course_id}
                             for name, course_id in courses]
    with state.lock:
        already_syncing = state.announcements_syncing
        if not already_syncing:
            state.announcements_syncing = True
            state.announcement_errors = []
    if already_syncing:
        state.note("Scheduled announcement sync skipped: sync already running")
        return
    try:
        do_announcement_sync(state, token=token, generation=generation)
    except Exception as exc:  # noqa: BLE001
        state.note("Scheduled announcement sync failed: {}".format(exc))

def announcement_sync_scheduler(state: State, stop: Optional[threading.Event] = None):
    """Poll announcements on startup, then at the start and end of every local day.

    This thread only runs while the app is open, so without the startup sync a
    post made while it was closed sits unsynced until the next 07:00/23:00
    window fires *after* the app happens to be running - up to 16h late, or a
    whole window missed if it's closed again by then. Mirrors
    inbound_poll_scheduler's startup poll for mail.
    """
    waiter = stop or threading.Event()
    run_scheduled_announcement_sync(state)
    while not waiter.wait(seconds_until_next_announcement_sync()):
        run_scheduled_announcement_sync(state)


def do_announcement_summary(state: State):
    try:
        summary = state.announcements.summarize(state.research_backend)
        state.note("Announcement TL;DR generated by {} {}".format(
            summary.get("backend", "AI"), summary.get("model", "")))
        with state.lock:
            state.announcement_summary_error = ""
    except Exception as exc:  # noqa: BLE001 - provider words belong in the panel
        with state.lock:
            state.announcement_summary_error = str(exc)
        state.note("Announcement TL;DR failed: {}".format(exc))
    finally:
        with state.lock:
            state.announcements_summarizing = False


def do_todo_sync(state: State, token: Optional[str] = None,
                  generation: Optional[int] = None):
    """Refresh Active Neural Queries without crossing browser sessions."""
    if token is None or generation is None:
        token, generation = state.session_snapshot()
    if not token or not state.session_current(token, generation):
        return
    try:
        with state.lock:
            courses = list(state.courses)
        items = rest_mod.get_todo_items(token, courses)
        if not state.session_current(token, generation):
            return
        count = state.todo.sync_ntulearn(items)
        state.todo.save()
        state.note("iNTUition To Do synced: {} active neural query(s)".format(count))
    except rest_mod.RestSessionExpired as exc:
        message = "Your NTU Learn session token was rejected while reading To Do ({}).".format(exc)
        state.reject_session(token, generation, message)
        raise auth.AuthenticationError(message)
    except Exception as exc:  # keep course-list sync useful if calendars fail
        state.note("iNTUition To Do sync failed: {}".format(exc))


def do_download(state: State, paths: List[str], token: Optional[str] = None,
                generation: Optional[int] = None):
    """Download selected entries without using a token from a newer session."""
    if token is None or generation is None:
        token, generation = _pipeline_session(state)
    try:
        wanted = [e for e in state.plan if e["path"] in set(paths)]
        with state.lock:
            state.progress = {
                "done": 0,
                "total": len(wanted),
                "current": "",
                "bytes": 0,
                "pct": 0,
            }

        for entry in wanted:
            if not _pipeline_session_current(state, token, generation):
                state.note("Download stopped: Blackboard session changed")
                return
            with state.lock:
                state.progress["current"] = entry["rel_path"]

            try:
                if entry["type"] == "file":
                    link = get_file_download_link(
                        token, entry["predownload_link"]
                    )
                    filename = entry.get("filename") or get_filename_from_url(link)
                    if not filename:
                        state.note("Skipped (no filename): {}".format(entry["name"]))
                        continue
                    target = os.path.join(
                        os.path.dirname(entry["path"]),
                        bounded_filename(os.path.dirname(entry["path"]), filename),
                    )
                else:
                    link = get_recorded_lecture_download_link(
                        token, entry["predownload_link"]
                    )
                    target = entry["path"]

                # An updated file must replace the stale copy; download() refuses to
                # overwrite, so clear it first.
                if entry["status"] == "updated" and os.path.isfile(target):
                    os.remove(target)

                def on_progress(downloaded, total, _entry=entry):
                    with state.lock:
                        state.progress["bytes"] = downloaded
                        state.progress["pct"] = (
                            round(downloaded / total * 100) if total else 0
                        )

                download(token, link, target, callback=on_progress)
                if not _pipeline_session_current(state, token, generation):
                    return
                entry["status"] = "current"
                state.note("Downloaded {}".format(entry["rel_path"]))
            except Exception as e:  # noqa: BLE001 - one bad item must not stop the run
                state.note("Failed {}: {}".format(entry["rel_path"], e))
            finally:
                with state.lock:
                    state.progress["done"] += 1

        state.refresh_media()
        state.note("Download finished")
    finally:
        with state.lock:
            if _pipeline_session_current(state, token, generation):
                state.downloading = False
                state.progress["current"] = ""



def do_transcribe(state: State, paths: Optional[List[str]] = None):
    """Generate transcripts for the selected staged media.

    Must run while the files are still local: move mode deletes each video once Drive
    confirms it, so a transcript produced afterwards would have no source. Anything the
    course already supplies a transcript for is never a candidate.
    """
    try:
        wanted = set(paths or [])
        media = [
            e["path"] for e in state.refresh_media()
            if e["status"] != transcribe_mod.PROVIDED
            and (not wanted or e["path"] in wanted)
        ]
        with state.lock:
            state.transcribe_progress = {
                "done": 0, "total": len(media), "current": "", "pct": 0
            }
        if not media:
            state.note("No untranscribed media staged")
            return

        if state._transcriber is None:
            state.note("Loading Whisper {} (first run downloads the model)".format(
                state.transcribe_model))
            state._transcriber = transcribe_mod.Transcriber(state.transcribe_model)
        transcriber = state._transcriber
        if transcriber is None:
            raise RuntimeError("transcriber could not be initialized")

        ok = failed = 0
        for path in media:
            rel = os.path.relpath(path, state.download_root)
            with state.lock:
                state.transcribe_progress["current"] = rel
                state.transcribe_progress["pct"] = 0

            def on_progress(frac, _text):
                with state.lock:
                    state.transcribe_progress["pct"] = round(frac * 100)

            try:
                transcriber.transcribe(path, progress=on_progress)
                state.note("Transcribed {}".format(rel))
                ok += 1
            except Exception as e:  # noqa: BLE001 - one bad file must not end the run
                state.note("Transcribe failed {}: {}".format(rel, e))
                failed += 1
            finally:
                with state.lock:
                    state.transcribe_progress["done"] += 1

        state.refresh_media()
        state.note("Transcription complete: {} done, {} failed".format(ok, failed))
    finally:
        with state.lock:
            state.transcribing = False
            state.transcribe_progress["current"] = ""


def do_transcribe_drive(state: State, drive_ids: Optional[List[str]] = None):
    """Generate transcripts for media that only exists in Drive: pull each one to a
    throwaway temp copy, transcribe it, upload the result beside it, then delete the
    temp copy. Mirrors transcribe_run.backfill(), but drives progress through the
    dashboard's state the same way do_transcribe() does for local media.

    This is the only transcription path the dashboard needs for a move-mode setup,
    where nothing sticks around locally long enough for do_transcribe() to reach it.
    """
    try:
        try:
            service = drive.build_service()
        except drive.DriveError as e:
            state.note("Drive unavailable: {}".format(e))
            return

        mirror = drive.DriveMirror(service, root_folder=state.drive_folder)
        listed = mirror.list_files()
        with state.lock:
            state.drive_files = listed

        wanted = set(drive_ids or [])
        media = [
            e for e in transcribe_mod.classify_drive_media(listed)
            if e["status"] != transcribe_mod.PROVIDED
            and (not wanted or e["drive_id"] in wanted)
        ]
        with state.lock:
            state.transcribe_progress = {
                "done": 0, "total": len(media), "current": "", "pct": 0
            }
        if not media:
            state.note("No untranscribed media found in Drive")
            return

        if state._transcriber is None:
            state.note("Loading Whisper {} (first run downloads the model)".format(
                state.transcribe_model))
            state._transcriber = transcribe_mod.Transcriber(state.transcribe_model)
        transcriber = state._transcriber
        if transcriber is None:
            raise RuntimeError("transcriber could not be initialized")

        from googleapiclient.http import MediaIoBaseDownload

        ok = failed = 0
        with tempfile.TemporaryDirectory(prefix="intuition_transcribe_") as tmp:
            for entry in media:
                with state.lock:
                    state.transcribe_progress["current"] = entry["rel_path"]
                    state.transcribe_progress["pct"] = 0
                local = os.path.join(tmp, entry["name"])
                try:
                    with open(local, "wb") as fh:
                        downloader = MediaIoBaseDownload(
                            fh, service.files().get_media(
                                fileId=entry["drive_id"], supportsAllDrives=True),
                            chunksize=8 * 1024 * 1024)
                        done = False
                        while not done:
                            _status, done = downloader.next_chunk()

                    def on_progress(frac, _text, _entry=entry):
                        with state.lock:
                            state.transcribe_progress["pct"] = round(frac * 100)

                    written = transcriber.transcribe(local, progress=on_progress)
                    parent_id = mirror.ensure_path(
                        [p for p in os.path.dirname(entry["rel_path"]).split("/") if p])
                    for kind in ("vtt", "txt"):
                        mirror.upload(written[kind], parent_id)
                    state.note("Backfilled {}".format(entry["rel_path"]))
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one bad file must not end the run
                    state.note("Backfill failed {}: {}".format(entry["rel_path"], e))
                    failed += 1
                finally:
                    # Reclaim the temp copy immediately; these are large.
                    for p in (local,) + tuple(
                            transcribe_mod.transcript_paths(local).values()):
                        if os.path.exists(p):
                            os.remove(p)
                    with state.lock:
                        state.transcribe_progress["done"] += 1

        state.note("Drive transcription complete: {} done, {} failed".format(ok, failed))
    finally:
        with state.lock:
            state.transcribing = False
            state.transcribe_progress["current"] = ""


def do_generate_summary(state: State, job_id: str, item_id: str, prompt: str,
                        scope: str, include_notes: bool, session_id: str):
    """Compendium worker thread: build_order step 5. Pulls the material fresh into
    a throwaway temp copy (the same pattern /api/drive/learn already uses), hands it
    to summary.generate(), then files the result - PDF and .tex beside the course's
    other material, a row in the notes database, either way, success or failure.
    """
    def set_stage(text: str):
        with state.lock:
            if state.summary_job and state.summary_job["id"] == job_id:
                state.summary_job["stage"] = text

    material_name = item_id
    try:
        with state.lock:
            item = next((dict(entry) for entry in state.drive_files
                        if entry["id"] == item_id), None)
        if not item:
            raise summary_mod.SummaryError(
                "material is not in the current Drive index")

        material_name = item.get("rel_path") or item.get("name") or item_id
        # Storage stays beside the material, under its actual top-level folder.
        # Sibling matching is different: materials.select() matches its course
        # argument as a short code (SC2005) against folder names, not the folder
        # name itself - the raw top segment (e.g. "26S1-SC2005-OPERATING SYSTEMS")
        # would only self-match that exact folder, missing siblings filed under any
        # differently-worded variant of the same course.
        top_folder = (item.get("rel_path") or "").split("/", 1)[0]
        codes = course_codes_in(top_folder)
        course_code = codes[0] if codes else top_folder

        note_text = ""
        chat_turns: List[Dict] = []
        if include_notes:
            note = state.notebook.get(item_id)
            # The editor stores rich HTML now, not markdown source - the prompt
            # wants the student's words, not their tag soup.
            note_text = html_to_text((note or {}).get("markdown", ""))
            chat_turns = state.chat_memory.recent(
                session_id, item_id, limit=summary_mod.CHAT_TURNS_LIMIT)

        set_stage("Connecting to Drive")
        service = drive.build_service(interactive=False)

        with tempfile.TemporaryDirectory(prefix="intuition-compendium-") as tmp:
            set_stage("Downloading material")
            material_path = drive.pull_file(service, item, tmp)
            result = summary_mod.generate(
                material_path, material_name, prompt, state.download_root,
                scope=scope, note_text=note_text, chat_turns=chat_turns,
                course=course_code, ledger=state.ledger, drive_service=service,
                preferred_backend=state.research_backend, on_stage=set_stage)

        if not result.ok:
            state.notebook.save_summary(
                job_id, document_id=item_id, material_name=material_name,
                prompt=prompt, scope=scope, backend=result.backend or "",
                model=result.model or "", rung=result.rung or "",
                page_anchors=result.pages_cited, ok=False,
                error="; ".join(result.errors)[:2000])
            with state.lock:
                job = state.summary_job
                if job is not None:
                    job.update({
                        "stage": "Failed", "ok": False, "done": True,
                        "error_stage": result.stage, "errors": result.errors,
                        "tex": result.tex,
                    })
            state.note("Compendium failed for {}: {}".format(
                material_name, "; ".join(result.errors[:2]) or result.stage))
            return

        if result.tex is None or result.pdf is None:
            raise summary_mod.SummaryError(
                "summary backend returned no TeX/PDF for a successful job")
        set_stage("Saving")
        summaries_dir = os.path.join(state.download_root, top_folder, "summaries")
        os.makedirs(summaries_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = os.path.splitext(os.path.basename(material_name))[0]
        tex_path = os.path.join(
            summaries_dir, bounded_filename(summaries_dir, "{}-{}.tex".format(stem, stamp)))
        pdf_path = os.path.join(
            summaries_dir, bounded_filename(summaries_dir, "{}-{}.pdf".format(stem, stamp)))
        with open(tex_path, "w", encoding="utf-8") as tex_file:
            tex_file.write(result.tex)
        with open(pdf_path, "wb") as pdf_file:
            pdf_file.write(result.pdf)

        state.notebook.save_summary(
            job_id, document_id=item_id, material_name=material_name,
            prompt=prompt, scope=scope, backend=result.backend or "",
            model=result.model or "", rung=result.rung or "",
            tex_path=tex_path, pdf_path=pdf_path,
            page_anchors=result.pages_cited, ok=True, error="", report=result.report)

        with state.lock:
            job = state.summary_job
            if job is not None:
                job.update({
                    "stage": "Done", "ok": True, "done": True,
                    "pdf_path": pdf_path, "tex_path": tex_path,
                    "backend": result.backend, "model": result.model, "rung": result.rung,
                    "pages_cited": result.pages_cited, "report": result.report,
                })
        state.note("Compendium summary saved: {}".format(
            os.path.relpath(pdf_path, state.download_root)))
    except Exception as exc:  # noqa: BLE001 - a thread dying silently is worse
        try:
            state.notebook.save_summary(
                job_id, document_id=item_id, material_name=material_name,
                prompt=prompt, scope=scope, ok=False, error=str(exc)[:2000])
        except Exception:  # noqa: BLE001 - the job status below is the real record
            pass
        with state.lock:
            if state.summary_job and state.summary_job["id"] == job_id:
                # This except only ever catches a failure *before* summary_mod.generate()
                # returns a result of its own - build_service()/pull_file() above, most
                # often - so its own result.stage never gets a chance to run. The stage
                # set_stage() last wrote is the only record of where it actually broke;
                # a bare "generate" here would misreport a Drive failure as an AI one.
                error_stage = state.summary_job.get("stage") or "generate"
                state.summary_job.update({
                    "stage": "Failed", "ok": False, "done": True,
                    "error_stage": error_stage, "errors": [str(exc)],
                })
        state.note("Compendium failed: {}".format(exc))
    finally:
        with state.lock:
            state.summarizing = False


def do_grade_tutorial(state: State, job_id: str, item_id: str, work_path: str,
                      work_filename: str):
    """Grading worker thread. Finds the open material's paired solution via
    solution_pairs, pulls it (and, if present, the tutorial's own question
    sibling) fresh into a throwaway temp copy the same way Compendium does, then
    hands all of it plus the student's already-staged upload to grading.grade().

    ``work_path`` was staged by the /api/drive/grade handler before this thread
    started; removing it is this function's responsibility either way.
    """
    def set_stage(text: str):
        with state.lock:
            if state.grading_job and state.grading_job["id"] == job_id:
                state.grading_job["stage"] = text

    material_name = item_id
    try:
        with state.lock:
            files = list(state.drive_files)
        item = next((dict(entry) for entry in files if entry["id"] == item_id), None)
        if not item:
            raise grading_mod.GradingError("material is not in the current Drive index")
        material_name = item.get("rel_path") or item.get("name") or item_id

        set_stage("Finding solution")
        pairs = solution_pairs_mod.pair_practice_with_solutions(files)
        pair = next((p for p in pairs if (p["practice"] or {}).get("id") == item_id), None)
        if pair is None:
            pair = next((p for p in pairs if p["solution"]["id"] == item_id), None)
        if pair is None:
            raise grading_mod.GradingError(
                "No professor solution has been matched to this material yet.")
        solution_ref, practice_ref = pair["solution"], pair["practice"]
        solution_entry = next((f for f in files if f["id"] == solution_ref["id"]), None)
        if solution_entry is None:
            raise grading_mod.GradingError("Matched solution is no longer in the Drive index")

        set_stage("Connecting to Drive")
        service = drive.build_service(interactive=False)

        with tempfile.TemporaryDirectory(prefix="intuition-grade-") as tmp:
            set_stage("Downloading solution")
            solution_path = drive.pull_file(service, solution_entry, tmp)
            solution_text = drive.extract_learning_text(solution_path)

            practice_text = None
            if practice_ref and practice_ref["id"] == item_id:
                # The material already open *is* the practice file - reuse it
                # rather than pulling the same file twice.
                practice_text = drive.extract_learning_text(
                    drive.pull_file(service, item, tmp))
            elif practice_ref:
                practice_entry = next(
                    (f for f in files if f["id"] == practice_ref["id"]), None)
                if practice_entry:
                    try:
                        practice_text = drive.extract_learning_text(
                            drive.pull_file(service, practice_entry, tmp))
                    except Exception:  # noqa: BLE001 - tutorial text is a nice-to-have
                        practice_text = None

            set_stage("Grading")
            result = grading_mod.grade(
                work_path, solution_text, solution_ref["name"], practice_text,
                material_name, preferred_backend=state.research_backend,
                download_root=state.download_root)

        state.notebook.save_grading(
            job_id, document_id=item_id, solution_document_id=solution_ref["id"],
            material_name=material_name, work_filename=work_filename,
            backend=result.backend, model=result.model, rung=result.rung,
            feedback=result.feedback, ok=result.ok, error=result.error)

        with state.lock:
            job = state.grading_job
            if job is not None and job["id"] == job_id:
                job.update({
                    "stage": "Done" if result.ok else "Failed", "ok": result.ok,
                    "done": True, "feedback": result.feedback,
                    "backend": result.backend, "model": result.model,
                    "rung": result.rung, "error": result.error,
                    "solution_name": solution_ref["name"],
                })
        if result.ok:
            state.note("Grading done for {}: {}".format(
                material_name, result.model or result.backend))
        else:
            state.note("Grading failed for {}: {}".format(material_name, result.error))
    except Exception as exc:  # noqa: BLE001 - a thread dying silently is worse
        try:
            state.notebook.save_grading(
                job_id, document_id=item_id, material_name=material_name,
                work_filename=work_filename, ok=False, error=str(exc)[:2000])
        except Exception:  # noqa: BLE001 - the job status below is the real record
            pass
        with state.lock:
            job = state.grading_job
            if job is not None and job["id"] == job_id:
                job.update({"stage": "Failed", "ok": False, "done": True,
                           "error": str(exc)})
        state.note("Grading failed for {}: {}".format(material_name, exc))
    finally:
        with state.lock:
            state.grading = False
        try:
            os.remove(work_path)
        except OSError:
            pass


def delete_summary_files(row: Dict) -> None:
    """Best-effort removal of one summary's .tex/.pdf pair - a file already gone
    (moved, or a failed run that never wrote one) is not an error here."""
    for key in ("tex_path", "pdf_path"):
        path = row.get(key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def sweep_old_summaries(state: State, max_age_days: int):
    """Startup housekeeping: run once, off the request path, so a slow or huge
    notebook never delays the dashboard binding its port (same reasoning as the
    Drive listing / identity refresh threads already started from serve()).
    """
    if max_age_days <= 0:
        return
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        removed = state.notebook.delete_summaries_older_than(cutoff)
        for row in removed:
            delete_summary_files(row)
        if removed:
            state.note("Compendium sweep: removed {} summary(ies) older than {} "
                      "day(s)".format(len(removed), max_age_days))
    except Exception as exc:  # noqa: BLE001 - a housekeeping sweep must never crash startup
        state.note("Compendium sweep failed: {}".format(exc))


def course_codes_in(name: str) -> List[str]:
    """Course codes embedded in one name, e.g. "26S1-SC2002-..." -> ["SC2002"].

    The academic year in the verbose form - "AY2026-2027, Semester 1, MH2100
    (Calculus III)" - has the exact shape of a course code and comes first, so it is
    excluded explicitly. Without that, every run told the model it was taking a
    course called AY2026.
    """
    import re
    return [c for c in re.findall(r"[A-Z]{2,4}\d{4}", (name or "").upper())
            if not c.startswith("AY")]


def course_codes(state: State) -> List[str]:
    """Course codes from the scanned course list, e.g. 26S1-SC2002-... -> SC2002."""
    with state.lock:
        names = [c.get("name", "") for c in state.courses]
    codes = []
    for name in names:
        codes += course_codes_in(name)
    return sorted(set(codes))


def do_todo_research(state: State, item_id: str, backend: Optional[str],
                     model: Optional[str]):
    item = state.todo.get(item_id)
    title = (item or {}).get("title", item_id)
    try:
        if item is None:
            raise ai_provider.ProviderError("That query no longer exists")
        details = str(item.get("details") or "").strip()
        prompt = "Active query: {}".format(title)
        if item.get("course"):
            prompt += "\nCourse: {}".format(item["course"])
        if details:
            prompt += "\nContext: {}".format(details)
        state.note("Running neural query: {}".format(title))
        finding = ai_provider.complete(
            prompt, TODO_SYSTEM_PROMPT, preferred=backend, model=model,
            max_tokens=1600, download_root=state.download_root)
        finding["at"] = datetime.now().isoformat(timespec="seconds")
        state.todo.set_research(item_id, finding)
        state.todo.save()
        with state.lock:
            state.todo_research_errors.pop(item_id, None)
        state.note("Neural query complete: {} via {} / {}".format(
            title, finding.get("backend", "?"), finding.get("model", "?")))
    except Exception as exc:  # provider errors should remain visible on the query
        with state.lock:
            state.todo_research_errors[item_id] = str(exc)
        state.note("Neural query failed for {}: {}".format(title, exc))
    finally:
        with state.lock:
            state.todo_researching.discard(item_id)


def do_push(state: State):
    """Mirror every locally-held plan entry into Drive, then reclaim the disk."""
    try:
        from intuition.drive_push import collect_files

        with state.lock:
            targets = []
            seen_paths = set()

            # 1. Targets from state.plan
            for e in state.plan:
                p = e.get("path", "")
                rel_p = e.get("rel_path", "")
                real_p = None
                if p and os.path.isfile(p):
                    real_p = p
                elif rel_p and os.path.isfile(os.path.join(state.download_root, rel_p)):
                    real_p = os.path.join(state.download_root, rel_p)

                if real_p and e.get("status") in PUSHABLE:
                    norm = os.path.abspath(real_p)
                    if norm not in seen_paths:
                        e["path"] = norm
                        targets.append(e)
                        seen_paths.add(norm)

            # 2. Add any remaining physical files on disk
            disk_files = collect_files(state.download_root)
            for f in disk_files:
                norm = os.path.abspath(f["path"])
                if norm not in seen_paths:
                    matched = None
                    # collect_files() reports the on-disk relpath (backslash-separated
                    # on Windows); state.plan carries sync.py's logical, forward-slash
                    # rel_path. Comparing them raw never matches on Windows, which
                    # silently orphaned every disk-only push target from its plan
                    # entry - losing both the status update after push and the
                    # entry's real (untruncated) logical name.
                    disk_rel = f["rel_path"].replace("\\", "/")
                    for e in state.plan:
                        if (e.get("rel_path") or "").replace("\\", "/") == disk_rel:
                            e["path"] = norm
                            e["status"] = "current"
                            matched = e
                            break
                    if matched:
                        targets.append(matched)
                    else:
                        targets.append({
                            "path": norm,
                            "rel_path": f["rel_path"],
                            "status": "current",
                            "size": f["size"],
                            "modified": f["modified"],
                        })
                    seen_paths.add(norm)

            state.push_progress = {
                "done": 0, "total": len(targets), "current": "", "pct": 0
            }

        if not targets:
            state.note("Nothing on disk to push")
            return

        try:
            service = drive.build_service()
        except drive.DriveError as e:
            state.note("Drive unavailable: {}".format(e))
            return

        try:
            # A scheduled `drive_push` run and this dashboard push must never touch
            # the same Drive folder at once - Drive's existence check for
            # create-vs-update is only eventually consistent, and two overlapping
            # pushes can each miss the other's fresh upload and duplicate it. See
            # drive.push_lock for the full reasoning.
            with drive.push_lock():
                mirror = drive.DriveMirror(service, root_folder=state.drive_folder)
                pushed = failed = 0

                for entry in targets:
                    with state.lock:
                        state.push_progress["current"] = entry["rel_path"]
                        state.push_progress["pct"] = 0

                    def on_chunk(fraction):
                        with state.lock:
                            state.push_progress["pct"] = round(fraction * 100)

                    try:
                        result = drive.push_file(
                            mirror, entry, state.download_root, state.ledger,
                            move=state.move, progress=on_chunk,
                        )
                        entry["status"] = "archived" if state.move else "current"
                        entry["drive_id"] = result["drive_id"]
                        pushed += 1
                        state.note("Drive <- {}".format(entry["rel_path"]))
                    except Exception as e:  # noqa: BLE001 - one bad file must not end the run
                        failed += 1
                        state.note("Push failed {}: {}".format(entry["rel_path"], e))
                    finally:
                        # Persist after each file so an interrupted run keeps its record.
                        state.ledger.save()
                        with state.lock:
                            state.push_progress["done"] += 1

                state.note("Push complete: {} moved, {} failed".format(pushed, failed))
        except drive.PushLockError as e:
            state.note(str(e))
    finally:
        with state.lock:
            state.pushing = False
            state.push_progress["current"] = ""


def _drive_roots(state: State) -> List[str]:
    """Return the roots this dashboard session is allowed to browse and pull."""
    roots = [state.drive_folder]
    if state.drive_folder == drive.DEFAULT_ROOT_FOLDER:
        roots.extend(root for root in drive.LEGACY_ROOT_FOLDERS if root not in roots)
    return roots


def _load_drive_inventory(service, state: State):
    """Refresh Drive metadata, retaining folder ancestry for every file."""
    return drive.list_files_from_roots(service, _drive_roots(state))


def _auto_tag_new_solutions(state: State, files: List[Dict]):
    """Note-tag any solution document that showed up since the last listing.

    Runs on every inventory refresh so a professor posting a new tutorial and
    its solution key gets the same "SOL" treatment the initial manual pass
    applied, with no separate step to remember. Only ever touches documents
    with no note row yet, so it can never overwrite a student's own notes or a
    tag from an earlier run.
    """
    fresh = solution_pairs_mod.new_solutions(files, state.notebook)
    tagged = 0
    for f in fresh:
        try:
            state.notebook.save(f["id"], "SOL", rel_path=f.get("rel_path") or f["name"],
                                 mime_type=f.get("mime_type") or "",
                                 drive_modified=f.get("modified") or "")
            tagged += 1
        except Exception as exc:  # noqa: BLE001 - one bad tag write must not break a sync
            state.note("Auto-tag failed for {}: {}".format(
                f.get("rel_path") or f["name"], exc))
    if tagged:
        state.note("Auto-tagged {} new solution document(s) with SOL".format(tagged))


def _merge_external_drive_files(state: State, files: List[Dict]) -> List[Dict]:
    """Fold "Search my Drive" hits back into a freshly-listed inventory.

    Must be called with state.lock held - reads state.external_drive_files.
    """
    known_ids = {f["id"] for f in files}
    merged = files + [f for fid, f in state.external_drive_files.items()
                       if fid not in known_ids]
    return sorted(merged, key=lambda item: item["rel_path"].lower())


def do_drive_list(state: State):
    try:
        service = drive.build_service()
        files, count_by_root = _load_drive_inventory(service, state)
        with state.lock:
            state.drive_files = _merge_external_drive_files(state, files)
        state.note("Drive inventory: {} file(s) ({})".format(
            len(files), ", ".join("{}: {}".format(root, count)
                                  for root, count in count_by_root.items())))
        _auto_tag_new_solutions(state, files)
    except Exception as exc:  # noqa: BLE001 - expose optional Drive failures in console
        state.note("Drive inventory failed: {}".format(exc))
    finally:
        with state.lock:
            state.drive_listing = False


def do_drive_link(state: State):
    """Run the Google OAuth consent flow, then refresh the Drive inventory.

    ``drive.link_interactively`` opens the system browser and blocks on a
    throwaway loopback server until Google redirects back (or the 5-minute
    timeout fires), so this must run on its own thread.
    """
    try:
        state.note("Drive: opening browser for Google authorisation")
        drive.link_interactively(open_browser=True)
        with state.lock:
            state.drive_link_error = ""
        state.note("Drive connected")
    except Exception as exc:  # noqa: BLE001 - surface consent/timeout failures in the UI
        message = str(exc) or exc.__class__.__name__
        with state.lock:
            state.drive_link_error = message
        state.note("Drive connection failed: {}".format(message))
        with state.lock:
            state.drive_linking = False
        return
    with state.lock:
        state.drive_linking = False
        if not state.drive_listing:
            state.drive_listing = True
            should_list = True
        else:
            should_list = False
    if should_list:
        do_drive_list(state)


def do_drive_pull(state: State, ids: List[str]):
    try:
        try:
            service = drive.build_service()
        except drive.DriveError as e:
            state.note("Drive unavailable: {}".format(e))
            return
        with state.lock:
            allowed = {item["id"]: dict(item) for item in state.drive_files}

        # The browser sends IDs, while the destination is determined by the
        # inventory's rel_path.  A cached inventory can be stale (for example after
        # a file was moved in Drive), so refresh before resolving an unknown ID.
        # Never use the filename-only metadata fallback here: it is the exact path
        # by which a file from any module gets dumped at the sync root.
        missing_ids = [item_id for item_id in ids if item_id not in allowed]
        if missing_ids:
            try:
                refreshed, _counts = _load_drive_inventory(service, state)
                with state.lock:
                    state.drive_files = refreshed
                allowed = {item["id"]: dict(item) for item in refreshed}
            except Exception as exc:  # noqa: BLE001 - report and skip unsafe targets
                state.note("Drive inventory refresh failed before pull: {}".format(exc))

        targets = []
        for item_id in ids:
            if item_id in allowed:
                targets.append(allowed[item_id])
            else:
                state.note(
                    "Drive file {} was not found under the configured Drive roots; "
                    "skipped to protect folder structure".format(item_id)
                )
        if not targets:
            state.note("No valid Drive files resolved for pull")
            return
        with state.lock:
            state.pull_progress = {"done": 0, "total": len(targets),
                                   "current": "", "pct": 0}
        pulled = failed = 0

        for item in targets:
            with state.lock:
                state.pull_progress.update(current=item["rel_path"], pct=0)
            def on_chunk(fraction):
                with state.lock:
                    state.pull_progress["pct"] = round(fraction * 100)
            try:
                drive.pull_file(service, item, state.download_root, progress=on_chunk)
                pulled += 1
                state.note("Drive -> {}".format(item["rel_path"]))
            except Exception as exc:  # noqa: BLE001 - continue with remaining priorities
                failed += 1
                state.note("Pull failed {}: {}".format(item["rel_path"], exc))
            finally:
                with state.lock:
                    state.pull_progress["done"] += 1
        state.note("Priority pull complete: {} restored, {} failed".format(pulled, failed))
        state.refresh_media()
    finally:
        with state.lock:
            state.pulling = False
            state.pull_progress["current"] = ""


def do_inbound_sync(state: State):
    """Pull unread OWA mail through iNTUition triage in the background."""
    try:
        state.note("Inbound sync: checking incoming mail")
        config = triage_mod.load_config(state.download_root)
        limit = int(config.get("max_emails_per_run", 50))
        result = triage_run_mod._scan(
            state.download_root, "", unread_only=True, limit=limit)
        if result:
            with state.lock:
                state.inbound_error = (
                    "Mailbox session expired; run triage_run --login"
                    if result == 2 else "Inbound sync failed")
            state.note("Inbound sync failed")
        else:
            state.note("Inbound sync complete")
    except Exception as exc:  # noqa: BLE001 - surface background failure in the HUD
        with state.lock:
            state.inbound_error = str(exc)
        state.note("Inbound sync failed: {}".format(exc))
    finally:
        inbound_mod._CACHE.clear()
        with state.lock:
            state.inbound_syncing = False


def _is_session_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    if "session token" in text and ("rejected" in text or "expired" in text):
        return True
    # api.get_courses raises this wording when Learn bounces the request to the
    # SSO login page: the cookie is structurally valid (it passed auth.validate)
    # but the server no longer honours it - the same "paste a fresh one" case,
    # only phrased differently. Without this the session is never flagged
    # rejected, so the dashboard's Authorisation panel stays hidden and there is
    # nowhere to enter a new BbRouter cookie.
    if "login page" in text and ("redirect" in text or "refresh the bbrouter" in text):
        return True
    return False

def do_unified_sync(state: State):
    """Refresh Blackboard-backed feeds using one stable session generation."""
    token, generation = state.session_snapshot()
    errors = []
    try:
        if not token:
            errors.append("Course nodes: no Blackboard session")
            return
        try:
            courses = get_courses(token, prefer_rest=state.prefer_rest,
                                  scope=state.scope, download_root=state.download_root)
            if not state.session_current(token, generation):
                return
            with state.lock:
                state.courses = [{"name": name, "id": course_id}
                                 for name, course_id in courses]
            do_todo_sync(state, token=token, generation=generation)
            state.note("Unified sync refreshed {} course nodes".format(len(courses)))
        except auth.AuthenticationError as exc:
            message = "Course nodes: {}".format(exc)
            if _is_session_rejection(exc):
                state.reject_session(token, generation, message)
            errors.append(message)
            return
        except Exception as exc:  # noqa: BLE001 - keep the failure visible and stop stale fan-out
            errors.append("Course nodes: {}".format(exc))
            return

        if not state.session_current(token, generation):
            return
        with state.lock:
            has_courses = bool(state.courses)
            if has_courses:
                state.announcements_syncing = True
                state.announcement_errors = []
        if has_courses:
            try:
                do_announcement_sync(state, token=token, generation=generation)
            except Exception as exc:  # noqa: BLE001
                errors.append("Announcements: {}".format(exc))
        else:
            errors.append("Announcements: no course nodes available")

        if not state.session_current(token, generation):
            return
        if state.inbound_db:
            errors.append("Inbound: configured store cannot be synced")
        else:
            with state.lock:
                state.inbound_syncing = True
                state.inbound_error = ""
            try:
                do_inbound_sync(state)
            except Exception as exc:  # noqa: BLE001
                errors.append("Inbound: {}".format(exc))
    finally:
        with state.lock:
            if state.session_generation == generation:
                state.unified_sync_error = " · ".join(errors)
                state.unified_syncing = False
        state.note("Unified sync finished" + (" with errors" if errors else ""))

def run_scheduled_inbound_poll(state: State):
    """Poll Outlook unless Inbound is pointed at an external read-only store."""
    if state.inbound_db:
        return
    with state.lock:
        already_syncing = state.inbound_syncing
        if not already_syncing:
            state.inbound_syncing = True
            state.inbound_error = ""
    if already_syncing:
        state.note("Scheduled Inbound poll skipped: poll already running")
        return
    do_inbound_sync(state)


def inbound_poll_scheduler(state: State, stop: Optional[threading.Event] = None):
    """Poll Outlook on startup and every twelve hours thereafter."""
    waiter = stop or threading.Event()
    run_scheduled_inbound_poll(state)
    while not waiter.wait(INBOUND_POLL_SECONDS):
        run_scheduled_inbound_poll(state)


class Handler(BaseHTTPRequestHandler):
    state: Optional[State] = None  # set by serve()
    MAX_JSON_BODY = 2 * 1024 * 1024
    MAX_DRIVE_LEARN_BODY = 4 * 1024 * 1024
    MAX_SCHEDULE_BODY = 25 * 1024 * 1024
    MAX_PREVIEW_BODY = 50 * 1024 * 1024
    MAX_GRADE_UPLOAD_JSON_BODY = 12 * 1024 * 1024

    def log_message(self, *args):
        pass  # keep the console clean; the UI has its own log

    def _send(self, payload: Dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_preview_error(self, message: str, status: int = 500,
                            request_id: str = ""):
        # /api/drive/content is loaded straight into the material drawer's
        # <iframe> by the browser, never read back as JSON. A JSON error body
        # here renders as Chrome's pretty-printed JSON viewer inside the drawer;
        # serve a plain styled page so the drawer shows a legible reason instead.
        detail = "<p class=\"rid\">Reference {}</p>".format(html.escape(request_id)) \
            if request_id else ""
        page = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<style>html,body{{margin:0;height:100%}}"
            "body{{display:flex;align-items:center;justify-content:center;"
            "font:14px/1.7 system-ui,-apple-system,Segoe UI,sans-serif;"
            "color:#8b93a7;background:#0f1117;padding:24px;text-align:center}}"
            ".rid{{margin-top:10px;font-size:12px;opacity:.7}}</style>"
            "<div><p>{}</p>{}</div>"
        ).format(html.escape(message), detail)
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _authorized(self) -> bool:
        expected = getattr(self.state, "local_secret", None)
        # Unit callers that provide a lightweight fake State do not have the
        # dashboard session cookie; real State instances always do.
        if not expected:
            return True
        supplied = self.headers.get("X-iNTUition-Session", "")
        if not supplied:
            cookie_header = self.headers.get("Cookie", "")
            for cookie in cookie_header.split(";"):
                name, separator, value = cookie.strip().partition("=")
                if separator and name == "intuition_session":
                    supplied = value
                    break
        if not supplied:
            # The material drawer loads /api/drive/content as an <iframe>/<img>/
            # <video> element src, which carries neither the X-iNTUition-Session
            # header (only the fetch() wrapper adds that) nor, in the packaged
            # WebView2 shell, the SameSite cookie. openMaterial() appends the same
            # secret as ?s= for these element-driven GETs.
            query = urlparse(getattr(self, "path", "")).query
            supplied = (parse_qs(query).get("s") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        # /api/drive/content is rendered straight into the drawer's <iframe>; a
        # JSON 401 body there shows up as Chrome's pretty-printed JSON viewer.
        # Serve the styled HTML error so the drawer stays legible.
        if urlparse(self.path).path == "/api/drive/content":
            self._send_preview_error(
                "The dashboard session could not be verified for this preview. "
                "Reload the dashboard and open the file again.", status=401)
        else:
            self._send({"error": "local dashboard session required"}, status=401)
        return False

    def _body(self, max_bytes: int = MAX_JSON_BODY) -> Dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length")
        if length < 0 or length > max_bytes:
            raise ValueError("request body is too large")
        if not length:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type and "application/json" not in content_type.lower():
            raise ValueError("expected application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client went away mid-response; not a real error
        except Exception as exc:  # noqa: BLE001 - last-resort dashboard telemetry
            request_id = self._request_id()
            self.state.note("GET {} failed [{}]: {}".format(self.path, request_id, exc))
            try:
                self._send({"error": "request failed", "request_id": request_id}, status=500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            secret = getattr(self.state, "local_secret", "")
            # Two ways for the page to prove it is the page: the cookie (used by a
            # real browser via --browser) and the rewritten <meta> the script
            # echoes back as a header. The desktop WebView2 shell does not send
            # the SameSite=Strict cookie on fetch()es to http:// loopback, so the
            # header is what actually authorises its API calls.
            body = load_page().replace(
                "__INTUITION_SESSION__", secret).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                "intuition_session={}; Path=/; HttpOnly; SameSite=Strict".format(
                    secret),
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        asset = static_asset_path(path)
        if asset:
            try:
                with open(asset, "rb") as f:
                    body = f.read()
            except OSError:
                self._send({"error": "not found"}, status=404)
                return
            content_type = mimetypes.guess_type(asset)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send(self.state.snapshot())
            return
        if path == "/api/health":
            # Startup probes must not call snapshot(): it deliberately aggregates
            # optional integrations and may be slow while they initialise.
            self._send({"ok": True})
            return
        if path.startswith("/api/") and not self._require_auth():
            return
        if path == "/api/drive/memory":
            query = parse_qs(parsed.query)
            session_id = (query.get("session") or [""])[0]
            item_id = (query.get("id") or [""])[0]
            if not session_id or not item_id:
                self._send({"error": "session and material id are required"}, status=400)
                return
            self._send({"items": self.state.chat_memory.recent(session_id, item_id)})
            return
        if path == "/api/lab/coach/history":
            query = parse_qs(parsed.query)
            session_id = (query.get("session") or [""])[0]
            rel_path = (query.get("path") or [""])[0]
            if not session_id or not rel_path:
                self._send({"error": "session and path are required"}, status=400)
                return
            _ws, repo = self.state.lab_repos.resolve((query.get("repo") or [""])[0])
            self._send({"items": self.state.chat_memory.recent(
                session_id, "lab:" + repo + "/" + rel_path)})
            return
        if path == "/api/lab/repos":
            self._send({"repos": self.state.lab_repos.list()})
            return
        if path == "/api/lab/tree":
            ws, repo = self.state.lab_repos.resolve(
                (parse_qs(parsed.query).get("repo") or [""])[0])
            self._send({
                "tree": ws.tree(),
                "inputKinds": ws.input_kinds(),
                "repos": self.state.lab_repos.list(),
                "repo": repo,
            })
            return
        if path == "/api/lab/output":
            query = parse_qs(parsed.query)
            job_id = (query.get("job") or [""])[0]
            since = (query.get("since") or ["0"])[0]
            try:
                seq = int(since)
            except ValueError:
                seq = 0
            result = self.state.lab_jobs.output_since(job_id, seq)
            if result is None:
                self._send({"error": "no such job"}, status=404)
                return
            self._send(result)
            return
        if path == "/api/lab/read":
            query = parse_qs(parsed.query)
            rel_path = (query.get("path") or [""])[0]
            try:
                ws, _repo = self.state.lab_repos.resolve((query.get("repo") or [""])[0])
                content = ws.read(rel_path)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=404)
                return
            self._send({"content": content})
            return
        if path == "/api/research":
            self._send(self.state.ureca.snapshot())
            return
        if path == "/api/research/suggest":
            job_id = (parse_qs(parsed.query).get("job") or [""])[0]
            if not job_id:
                self._send({"error": "job id is required"}, status=400)
                return
            with self.state.lock:
                job = dict(self.state.research_suggest_job or {})
            if not job or job.get("id") != job_id:
                self._send({"error": "no such research suggestion job"}, status=404)
                return
            self._send({"job": job})
            return
        if path == "/api/research/faculty":
            query = parse_qs(parsed.query)
            self._send({"faculty": faculty_db.directory((query.get("q") or [""])[0],
                                    (query.get("school") or [""])[0]),
                        "catalogue": faculty_db.metadata()})
            return
        if path == "/api/profile":
            self._send({"profile": self.state.profile.get()})
            return
        if path == "/api/research/saved":
            self._send(self.state.saved_topics.snapshot())
            return
        if path == "/api/notes":
            query = parse_qs(parsed.query)
            item_id = (query.get("id") or [""])[0]
            search = (query.get("q") or [""])[0]
            if search:
                self._send({"items": self.state.notebook.search(search)})
            elif item_id:
                self._send({"note": self.state.notebook.get(item_id)})
            else:
                self._send({"error": "material id or search query is required"}, status=400)
            return
        if path == "/api/study/summary":
            job_id = (parse_qs(parsed.query).get("job") or [""])[0]
            with self.state.lock:
                job = dict(self.state.summary_job) if self.state.summary_job else None
            if not job or job["id"] != job_id:
                self._send({"error": "no such job (it may have been superseded by "
                                     "a newer generation)"}, status=404)
                return
            self._send({"job": job})
            return
        if path == "/api/study/summaries":
            item_id = (parse_qs(parsed.query).get("id") or [""])[0]
            if not item_id:
                self._send({"error": "material id is required"}, status=400)
                return
            self._send({"items": self.state.notebook.list_summaries(item_id)})
            return
        if path == "/api/study/gradings":
            item_id = (parse_qs(parsed.query).get("id") or [""])[0]
            if not item_id:
                self._send({"error": "material id is required"}, status=400)
                return
            self._send({"items": self.state.notebook.list_gradings(item_id)})
            return
        if path == "/api/study/summary/pdf":
            job_id = (parse_qs(parsed.query).get("job") or [""])[0]
            with self.state.lock:
                job = dict(self.state.summary_job) if self.state.summary_job else None
            # A job id that isn't the one currently in memory is history - it still
            # has a real .tex/.pdf on disk, just recorded via the notebook instead
            # of state.summary_job (which only ever holds the most recent run).
            if not job or job["id"] != job_id:
                job = self.state.notebook.get_summary(job_id)
            if not job or not job.get("ok") or not job.get("pdf_path"):
                self._send({"error": "no finished PDF for this job"}, status=404)
                return
            try:
                with open(job["pdf_path"], "rb") as f:
                    body = f.read()
            except OSError as exc:
                self._send({"error": str(exc)}, status=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "inline")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/study/summary/tex":
            job_id = (parse_qs(parsed.query).get("job") or [""])[0]
            with self.state.lock:
                job = dict(self.state.summary_job) if self.state.summary_job else None
            if not job or job["id"] != job_id:
                job = self.state.notebook.get_summary(job_id)
            if not job:
                self._send({"error": "no such job"}, status=404)
                return
            # A finished job's document lives on disk; a failed one only ever had
            # its text held in memory for exactly this handback - "the user gets
            # the .tex, the log, and a plain statement of what failed."
            if job.get("tex_path"):
                try:
                    with open(job["tex_path"], "r", encoding="utf-8") as f:
                        body = f.read().encode("utf-8")
                except OSError as exc:
                    self._send({"error": str(exc)}, status=500)
                    return
            elif job.get("tex"):
                body = job["tex"].encode("utf-8")
            else:
                self._send({"error": "no .tex available for this job"}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "inline")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/drive/content":
            item_id = (parse_qs(parsed.query).get("id") or [""])[0]
            with self.state.lock:
                item = next((dict(entry) for entry in self.state.drive_files
                             if entry["id"] == item_id), None)
            if not item:
                self._send_preview_error(
                    "This file is no longer in the loaded Drive listing. "
                    "Refresh Drive and select it again.", status=404)
                return
            if int(item.get("size") or 0) > self.MAX_PREVIEW_BODY:
                self._send_preview_error(
                    "This file is larger than the 50 MB preview limit. "
                    "Open it in Google Drive instead.", status=413)
                return
            try:
                service = drive.build_service(interactive=False)
                with tempfile.TemporaryDirectory(prefix="intuition-preview-") as tmp:
                    target = drive.pull_file(service, item, tmp)
                    is_docx = (target.lower().endswith(".docx")
                               or item.get("mime_type") == drive.DOCX_MIME)
                    if is_docx:
                        # No browser renders a .docx natively; convert it to a
                        # styled HTML page the drawer's <iframe> can display.
                        try:
                            body = drive.docx_to_html(target).encode("utf-8")
                        except drive.DriveError as exc:
                            self.state.note("Drive .docx preview: {}".format(exc))
                            self._send_preview_error(
                                "This Word document could not be rendered for "
                                "preview. Open it in Google Drive instead.",
                                status=422)
                            return
                        content_type = "text/html; charset=utf-8"
                    else:
                        with open(target, "rb") as stream:
                            body = stream.read()
                        # mimetypes.guess_type doesn't know source-code extensions like
                        # .java or .cpp (returns None), which would otherwise fall
                        # through to application/octet-stream below - Drive already
                        # told us the real type in item["mime_type"] (that's what the
                        # frontend used to pick the <iframe> rendering path in the
                        # first place), so trust that first.
                        content_type = (mimetypes.guess_type(target)[0]
                                        or item.get("mime_type"))
                        if item.get("mime_type") in drive.GOOGLE_EXPORTS:
                            content_type = drive.GOOGLE_EXPORTS[item["mime_type"]][0]
                self.send_response(200)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", "inline")
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001 - report Drive preview failures
                request_id = self._request_id()
                self.state.note("Drive preview failed [{}]: {}".format(request_id, exc))
                self._send_preview_error(
                    "This material could not be fetched from Google Drive for "
                    "preview. Try refreshing Drive, or open it in Google Drive.",
                    status=500, request_id=request_id)
            return

        if path == "/api/drive/tree":
            prefix = (parse_qs(parsed.query).get("path") or [""])[0]
            with self.state.lock:
                files = list(self.state.drive_files)
                listing = self.state.drive_listing
            level = drive.tree_level(files, prefix)
            level["listing"] = listing
            level["file_count"] = len(files)
            self._send(level)
            return
        self._send({"error": "not found"}, status=404)

    def do_PUT(self):
        try:
            self._do_PUT()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client went away mid-response; not a real error
        except Exception as exc:  # noqa: BLE001 - last-resort dashboard telemetry
            request_id = self._request_id()
            self.state.note("PUT {} failed [{}]: {}".format(self.path, request_id, exc))
            try:
                self._send({"error": "request failed", "request_id": request_id}, status=500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _do_PUT(self):
        """Schedule upload. The body is the raw file; ?name= gives its filename."""
        parsed = urlparse(self.path)
        if parsed.path != "/api/schedule":
            self._send({"error": "not found"}, status=404)
            return
        state = self.state
        params = parse_qs(parsed.query)
        name = (params.get("name") or ["upload.txt"])[0]
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send({"error": "invalid Content-Length"}, status=400)
            return
        if not length:
            self._send({"error": "empty upload"}, status=400)
            return
        if length > self.MAX_SCHEDULE_BODY:
            self._send({"error": "schedule upload is too large"}, status=413)
            return

        suffix = os.path.splitext(name)[1] or ".txt"
        safe_name = os.path.basename(name) or "upload.txt"
        fd, tmp = tempfile.mkstemp(
            prefix="intuition_schedule_upload.", suffix=suffix,
        )
        try:
            with os.fdopen(fd, "wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("incomplete schedule upload")
                    f.write(chunk)
                    remaining -= len(chunk)
            result = schedule_mod.parse_file(tmp)
        except schedule_mod.ScheduleError as e:
            self._send({"error": str(e)}, status=400)
            return
        except Exception as e:  # noqa: BLE001 - report any parse failure to the UI
            self._send({"error": "Could not read {}: {}".format(safe_name, e)}, status=400)
            return
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

        if not result["sessions"]:
            self._send({"error": "No classes found in {}. Expected a STARS "
                                 "'Course(s) Registered' PDF, a copied timetable "
                                 "or an .ics export.".format(name)}, status=400)
            return

        state.schedule.replace(result["sessions"], exams=result.get("exams"),
                               courses=result.get("courses"),
                               semester=result.get("semester"))
        state.schedule.save()
        state.note("Schedule imported from {}: {} session(s)".format(
            name, len(result["sessions"])))
        self._send({"ok": True, "sessions": len(result["sessions"]),
                    "exams": len(result.get("exams") or [])})

    def do_POST(self):
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client went away mid-response; not a real error
        except Exception as exc:  # noqa: BLE001 - no interface failure should be silent
            request_id = self._request_id()
            self.state.note("POST {} failed [{}]: {}".format(self.path, request_id, exc))
            try:
                self._send({"error": "request failed", "request_id": request_id}, status=500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _do_POST(self):
        path = urlparse(self.path).path
        state = self.state

        # Token exchange is the bootstrap endpoint; every other API operation
        # requires the cookie issued by GET "/".
        if path != "/api/token" and path.startswith("/api/") and not self._require_auth():
            return

        try:
            body_limit = (self.MAX_DRIVE_LEARN_BODY if path == "/api/drive/learn"
                          else self.MAX_GRADE_UPLOAD_JSON_BODY if path == "/api/study/grade"
                          else self.MAX_JSON_BODY)
            payload = self._body(body_limit)
        except (TypeError, ValueError):
            self._send({"error": "bad json"}, status=400)
            return
        if not isinstance(payload, dict):
            self._send({"error": "request body must be a JSON object"}, status=400)
            return

        if path == "/api/client-log":
            message = str(payload.get("message") or "unknown browser failure")
            # Browser diagnostics are untrusted input even on localhost. Keep the
            # console single-line, bounded, and useful without accepting a log flood.
            message = " ".join(message.split())[:1000]
            state.note("Interface failure: {}".format(message))
            self._send({"ok": True})
            return

        if path == "/api/notes":
            item_id = str(payload.get("id") or "")
            if not item_id:
                self._send({"error": "material id is required"}, status=400)
                return
            with state.lock:
                item = next((dict(entry) for entry in state.drive_files
                             if entry["id"] == item_id), None)
            if not item:
                self._send({"error": "material is not in the current Drive index"}, status=404)
                return
            try:
                note = state.notebook.save(
                    item_id, payload.get("markdown", ""),
                    rel_path=item.get("rel_path") or item.get("name") or "",
                    mime_type=item.get("mime_type") or "",
                    drive_modified=item.get("modified") or "",
                    last_page=payload.get("last_page") or 1,
                    expected_updated_at=str(payload.get("expected_updated_at") or ""))
            except NoteConflict as exc:
                self._send({"error": str(exc), "note": state.notebook.get(item_id)},
                           status=409)
                return
            except (TypeError, ValueError) as exc:
                self._send({"error": str(exc)}, status=400)
                return
            self._send({"note": note})
            return

        if path == "/api/token":
            try:
                token = auth.resolve(payload.get("token", ""))
            except auth.AuthenticationError as e:
                self._send({"error": str(e)}, status=400)
                return
            generation = state.install_token(token)
            threading.Thread(target=state.refresh_identity,
                             args=(token, generation), daemon=True).start()
            state.note("Session token accepted")
            self._send({"ok": True})
            return

        if path == "/api/settings":
            with state.lock:
                busy = any((
                    state.scanning, state.downloading, state.pushing,
                    state.drive_listing, state.pulling, state.transcribing,
                    state.summarizing, state.unified_syncing,
                    state.announcements_syncing, state.inbound_syncing,
                ))
                if "prefer_rest" in payload:
                    state.prefer_rest = bool(payload["prefer_rest"])
            if payload.get("download_root"):
                # Moving the root mid-run would leave the ledger and the in-flight
                # plan describing two different folders.
                if busy:
                    self._send(
                        {"error": "cannot change the sync folder while a scan, "
                                  "download, Drive, or background job is running"},
                        status=409)
                    return
                state.rebind_root(payload["download_root"])
            self._send({"ok": True})
            return

        if path == "/api/lab/repos":
            action = payload.get("action")
            name = str(payload.get("name") or "")
            try:
                if action == "create":
                    state.lab_repos.create(name)
                elif action == "delete":
                    state.lab_repos.delete(name)
                elif action == "rename":
                    state.lab_repos.rename(name, str(payload.get("newName") or ""))
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            self._send({"ok": True, "repos": state.lab_repos.list()})
            return

        if path == "/api/lab/file":
            action = payload.get("action")
            rel_path = str(payload.get("path") or "")
            updated_content = None
            try:
                ws, _repo = state.lab_repos.resolve(payload.get("repo"))
                if action == "create":
                    ws.create(rel_path, str(payload.get("kind") or "file"), str(payload.get("inputKind") or "none"))
                elif action == "write":
                    content = payload.get("content", "")
                    if not isinstance(content, str):
                        self._send({"error": "content must be text"}, status=400)
                        return
                    if len(content) > 1_000_000:
                        self._send({"error": "file content is limited to 1 MB"}, status=413)
                        return
                    ws.write(rel_path, content)
                elif action == "scaffold":
                    updated_content = ws.apply_input_kind(
                        rel_path, str(payload.get("inputKind") or "none"))
                elif action == "delete":
                    ws.delete(rel_path)
                elif action == "rename":
                    ws.rename(rel_path, str(payload.get("newPath") or ""))
                elif action == "move":
                    state.lab_repos.move_file(
                        _repo, rel_path, str(payload.get("toRepo") or ""))
                elif action == "reorder":
                    order = payload.get("order")
                    if not isinstance(order, list) or not all(
                            isinstance(name, str) for name in order):
                        self._send({"error": "order must be a list of names"},
                                   status=400)
                        return
                    ws.reorder(str(payload.get("parent") or ""), order)
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            response = {
                "ok": True,
                "tree": ws.tree(),
                "inputKinds": ws.input_kinds(),
                "repos": state.lab_repos.list(),
            }
            if updated_content is not None:
                response["content"] = updated_content
            self._send(response)
            return

        if path == "/api/lab/run":
            rel_path = str(payload.get("path") or "")
            language = str(payload.get("language") or "")
            input_kind = str(payload.get("inputKind") or "none")
            if os.path.splitext(rel_path)[1].lower() not in lab_mod.CODE_EXTENSIONS:
                self._send({"error": "only .py, .java and .c files can be run"},
                           status=400)
                return
            try:
                ws, _repo = state.lab_repos.resolve(payload.get("repo"))
                autofilled_source = None
                if language == "python":
                    # A file that only defines its algorithm and never calls it
                    # runs "successfully" with nothing to trace - fix that before
                    # Run rather than let Simulation quietly come up empty. Cheap
                    # to call every time: the ast check below returns fast when
                    # the file already has a real entry point, so this only ever
                    # reaches the model on the file that actually needs it.
                    autofilled_source = lab_mod.autofill_entry_point(
                        ws, rel_path, state.research_backend, state.download_root)
                ws.set_input_kind(rel_path, input_kind)
                job = state.lab_jobs.start(ws, rel_path, language, input_kind)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            response = {"ok": True, "job": job.id, "inputKind": job.input_kind,
                        "inputPreview": job.input_preview, "sourceHash": job.source_hash}
            if autofilled_source is not None:
                response["autofilledSource"] = autofilled_source
            self._send(response)
            return

        if path == "/api/lab/kill":
            ok = state.lab_jobs.kill(str(payload.get("job") or ""))
            self._send({"ok": ok})
            return

        if path == "/api/lab/analyze":
            rel_path = str(payload.get("path") or "")
            try:
                ws, _repo = state.lab_repos.resolve(payload.get("repo"))
                content = ws.read(rel_path)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            if not content.strip():
                self._send({"error": "file is empty"}, status=400)
                return
            source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            expected_hash = str(payload.get("sourceRevision") or "")
            if expected_hash and expected_hash != source_hash:
                self._send({"error": "file changed before analysis; analyze the current source again",
                            "sourceHash": source_hash}, status=409)
                return
            lines = content.splitlines()
            line_count = len(lines)
            # Numbered so the model's "line" fields land on the same lines
            # the editor shows - lab_analysis then refuses anything outside
            # [1, line_count] regardless of what the model claims.
            numbered = "\n".join("{}: {}".format(i + 1, line) for i, line in enumerate(lines))
            prompt = "Source file ({}):\n{}\n---\nReport the JSON blueprint now.".format(
                rel_path, numbered)
            try:
                result = ai_provider.complete_tier(
                    "chat", prompt, LAB_BLUEPRINT_SYSTEM, preferred=state.research_backend,
                    max_tokens=1200, download_root=state.download_root)
            except ai_provider.ProviderError as exc:
                self._send({"error": str(exc)}, status=502)
                return
            blueprint = lab_analysis_mod.parse_blueprint_response(
                result.get("text") or "", line_count)
            self._send({"blueprint": blueprint, "backend": result.get("backend"),
                        "model": result.get("model"), "sourceHash": source_hash})
            return

        if path == "/api/lab/coach":
            rel_path = str(payload.get("path") or "")
            session_id = str(payload.get("session") or "default")[:80]
            question = str(payload.get("question") or "").strip()[:2000]
            mode = str(payload.get("mode") or "ask")  # "ask" | "live" | "run"
            run_context = str(payload.get("runContext") or "")[:4000]
            previous_source = str(payload.get("previousSource") or "")
            try:
                ws, repo = state.lab_repos.resolve(payload.get("repo"))
                content = ws.read(rel_path)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            if not content.strip():
                self._send({"error": "file is empty"}, status=400)
                return
            if mode == "ask" and not question:
                self._send({"error": "enter a question"}, status=400)
                return
            source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            expected_hash = str(payload.get("sourceRevision") or "")
            if expected_hash and expected_hash != source_hash:
                self._send({"error": "file changed before asking; ask again",
                            "sourceHash": source_hash}, status=409)
                return
            lines = content.splitlines()
            numbered = "\n".join("{}: {}".format(i + 1, line) for i, line in enumerate(lines))
            material_id = "lab:" + repo + "/" + rel_path
            try:
                history = state.chat_memory.recent(session_id, material_id, limit=8)
            except Exception as memory_exc:  # noqa: BLE001 - answer even if local history is damaged
                state.note("Lab coach history unavailable: {}".format(memory_exc))
                history = []
            conversation = "\n".join(
                "{}: {}".format(turn["role"].title(), turn["content"][:2000])
                for turn in history)
            header = ("Source file ({}):\n{}\n---\nRecent run output (if any):\n{}\n---\n"
                      "Recent conversation:\n{}\n---\n").format(
                rel_path, numbered, run_context or "(none)", conversation or "(none)")
            if mode == "ask":
                prompt = header + "Student question: {}".format(question)
            else:
                # Unprompted (live/run) commentary is grounded in the delta since the
                # coach last looked, not the whole file again - see _changed_line_ranges.
                if not previous_source:
                    delta_note = "This is your first look at this file."
                elif previous_source == content:
                    delta_note = "The visible code is unchanged since you last looked."
                else:
                    changed = _changed_line_ranges(previous_source, content)
                    delta_note = (
                        "New or changed lines since you last looked: {}. Comment only on "
                        "those - do not re-explain or repeat feedback on lines you already "
                        "discussed.".format(changed) if changed
                        else "The visible code is unchanged since you last looked.")
                if mode == "run":
                    prompt = header + delta_note + (
                        " The student just ran this file. Give a brief real-time comment "
                        "on the result above.")
                else:
                    prompt = header + delta_note + (
                        " The student is actively editing. Give a brief real-time "
                        "observation about the new/changed lines only - a spotted issue, "
                        "or a natural small next step.")
            try:
                result = ai_provider.complete_tier(
                    "chat", prompt, LAB_COACH_SYSTEM, preferred=state.research_backend,
                    max_tokens=700, download_root=state.download_root)
            except ai_provider.ProviderError as exc:
                self._send({"error": str(exc)}, status=502)
                return
            answer = (result.get("text") or "").strip()
            try:
                if question:
                    state.chat_memory.add(session_id, material_id, "user", question)
                state.chat_memory.add(session_id, material_id, "assistant", answer,
                                      backend=result.get("backend") or "",
                                      model=result.get("model") or "")
            except Exception as memory_exc:  # noqa: BLE001 - the answer still reaches the student
                state.note("Lab coach history save failed: {}".format(memory_exc))
            self._send({"answer": answer, "mode": mode, "backend": result.get("backend"),
                        "model": result.get("model"), "sourceHash": source_hash})
            return

        if path == "/api/research":
            action = payload.get("action")
            try:
                if action == "create":
                    item = state.ureca.add(payload.get("title", ""))
                elif action == "update":
                    item = state.ureca.update(payload.get("id", ""), **{
                        k: payload.get(k) for k in
                        ("title", "category", "status") + ureca_mod.TEXT_FIELDS + ("deliverables",)})
                    if item is None:
                        self._send({"error": "no such proposal"}, status=404)
                        return
                elif action == "delete":
                    if not state.ureca.remove(payload.get("id", "")):
                        self._send({"error": "no such proposal"}, status=404)
                        return
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except ValueError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            state.ureca.save()
            self._send({"ok": True, "items": state.ureca.items})
            return

        if path == "/api/research/suggest":
            # Typed fresh for this run when given, so it takes effect immediately -
            # otherwise the profile's saved value, which the debounced autosave in
            # rpKeywords may not have persisted yet if the student typed and pressed
            # "Suggest topics" in the same breath.
            keywords = str(payload.get("keywords") or "").strip()[:200] or state.profile.get()["keywords"]
            professor_id = str(payload.get("professor") or "").strip()
            professor = faculty_db.get(professor_id) if professor_id else None
            if professor_id and professor is None:
                self._send({"error": "unknown professor"}, status=400)
                return
            prompt = research_suggest_prompt(state, keywords, professor)
            if payload.get("async"):
                with state.lock:
                    current = state.research_suggest_job
                    if current and current.get("status") == "running":
                        self._send({"error": "a research suggestion pass is already running"},
                                   status=409)
                        return
                    job_id = uuid.uuid4().hex[:16]
                    state.research_suggest_job = {
                        "id": job_id, "status": "running", "done": False,
                        "started_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"),
                    }
                threading.Thread(target=do_research_suggest,
                                 args=(state, job_id, prompt, keywords, professor),
                                 daemon=True).start()
                with state.lock:
                    job = dict(state.research_suggest_job)
                self._send({"job": job}, status=202)
                return
            # Keep the synchronous response for small external clients that still
            # use the original endpoint contract. The dashboard itself uses the
            # async handoff above so it never waits on the provider over HTTP.
            try:
                self._send(generate_research_suggestions(state, prompt, keywords, professor))
            except ai_provider.ProviderError as exc:
                self._send({"error": str(exc)}, status=502)
            return

        if path == "/api/profile":
            item = state.profile.update(**{k: payload.get(k) for k in profile_mod.FIELDS})
            state.profile.save()
            self._send({"profile": item})
            return

        if path == "/api/research/saved":
            action = payload.get("action")
            try:
                if action == "star":
                    state.saved_topics.add(payload.get("title", ""), payload.get("topic", ""))
                elif action == "unstar":
                    if not state.saved_topics.remove(payload.get("id", "")):
                        self._send({"error": "no such saved topic"}, status=404)
                        return
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except ValueError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            state.saved_topics.save()
            self._send(state.saved_topics.snapshot())
            return

        if path == "/api/research/draft":
            item_id = str(payload.get("id") or "")
            topic = str(payload.get("topic") or "").strip()[:600]
            item = state.ureca.get(item_id)
            if item is None:
                self._send({"error": "no such proposal"}, status=404)
                return
            if not topic:
                self._send({"error": "describe the idea in a sentence first"}, status=400)
                return
            prompt = "Project title: {}\nStudent's rough idea: {}\n---\nReport the JSON draft now.".format(
                item.get("title") or "(untitled)", topic)
            try:
                result = ai_provider.complete_tier(
                    "scholar", prompt, URECA_DRAFT_SYSTEM,
                    preferred=research_tab_backend(state),
                    max_tokens=1800, download_root=state.download_root)
            except ai_provider.ProviderError as exc:
                self._send({"error": str(exc)}, status=502)
                return
            draft = ureca_mod.parse_draft_response(result.get("text") or "")
            # Fills in only what the student hasn't already written - a
            # second draft pass never clobbers an edit they've since made.
            fill = {k: v for k, v in draft.items() if v and not (item.get(k) or "").strip()}
            updated = state.ureca.update(item_id, **fill) if fill else item
            state.ureca.save()
            self._send({"item": updated, "backend": result.get("backend"),
                        "model": result.get("model")})
            return

        if path == "/api/todo":
            action = payload.get("action")
            try:
                if action == "add":
                    state.todo.add(payload.get("title", ""),
                                   direction=payload.get("direction", "push"),
                                   priority=payload.get("priority", "normal"),
                                   details=payload.get("details", ""),
                                   course=payload.get("course", ""))
                elif action == "update":
                    item = state.todo.update(payload.get("id", ""),
                        **{k: payload.get(k) for k in
                           ("title", "details", "direction", "priority", "status", "course")})
                    if item is None:
                        self._send({"error": "no such request"}, status=404)
                        return
                elif action == "remove":
                    if not state.todo.remove(payload.get("id", "")):
                        self._send({"error": "no such request"}, status=404)
                        return
                elif action == "research":
                    item_id = str(payload.get("id") or "")
                    backend = str(payload.get("backend") or "auto")
                    model = str(payload.get("model") or "auto")
                    if state.todo.get(item_id) is None:
                        self._send({"error": "no such query"}, status=404)
                        return
                    if backend not in TODO_BACKENDS:
                        self._send({"error": "unknown AI backend"}, status=400)
                        return
                    compatible = {value for value, _label
                                  in todo_model_options()[backend]}
                    if model not in compatible:
                        self._send({"error": "model is not available for selected backend"},
                                   status=400)
                        return
                    preferred, provider_status = todo_ai_choice(backend)
                    if preferred is None or not provider_status.get("ready"):
                        self._send({"error": "selected AI backend is unavailable"},
                                   status=400)
                        return
                    with state.lock:
                        if item_id in state.todo_researching:
                            self._send({"error": "already running"}, status=409)
                            return
                        state.todo_researching.add(item_id)
                        state.todo_research_errors.pop(item_id, None)
                    threading.Thread(
                        target=do_todo_research,
                        args=(state, item_id, preferred,
                              None if model == "auto" else model),
                        daemon=True).start()
                    self._send({"ok": True, "researching": item_id})
                    return
                elif action == "clear_research":
                    if state.todo.set_research(payload.get("id", ""), None) is None:
                        self._send({"error": "no such query"}, status=404)
                        return
                elif action == "chat":
                    item_id = str(payload.get("id") or "")
                    session_id = str(payload.get("session") or "default")[:80]
                    question = str(payload.get("question") or "").strip()[:2000]
                    item = state.todo.get(item_id)
                    if item is None:
                        self._send({"error": "no such query"}, status=404)
                        return
                    if not question:
                        self._send({"error": "enter a question"}, status=400)
                        return
                    chat_key = "todo:" + item_id
                    try:
                        history = state.chat_memory.recent(session_id, chat_key, limit=8)
                        conversation = "\n".join(
                            "{}: {}".format(turn["role"].title(), turn["content"][:4000])
                            for turn in history)
                        finding = item.get("research") or {}
                        prompt = "Active query: {}".format(item.get("title") or item_id)
                        if item.get("course"):
                            prompt += "\nCourse: {}".format(item["course"])
                        if item.get("details"):
                            prompt += "\nContext: {}".format(item["details"])
                        if finding.get("text"):
                            prompt += "\n\nPrior AI finding on this query:\n{}".format(
                                finding["text"][:4000])
                        prompt += "\n\nRecent conversation:\n{}\n\nStudent question: {}".format(
                            conversation or "(none)", question)
                        result = ai_provider.complete_tier(
                            "chat", prompt, TODO_SYSTEM_PROMPT, preferred=state.research_backend,
                            max_tokens=900, download_root=state.download_root)
                        answer = result.get("text") or ""
                        state.chat_memory.add(session_id, chat_key, "user", question)
                        state.chat_memory.add(session_id, chat_key, "assistant", answer,
                                              result.get("backend"), result.get("model"))
                        self._send({"answer": answer, "backend": result.get("backend"),
                                    "model": result.get("model")})
                    except ai_provider.ProviderError as exc:
                        self._send({"error": str(exc)}, status=500)
                    return
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except ValueError as e:
                self._send({"error": str(e)}, status=400)
                return
            state.todo.save()
            self._send({"ok": True, "count": len(state.todo)})
            return

        if path == "/api/focus":
            # The Focus bar runs the timer client-side and posts one row when a
            # stint finishes. Needs the dashboard session only, like "read" -
            # no Blackboard token, works offline.
            if payload.get("action") != "log":
                self._send({"error": "unknown action"}, status=400)
                return
            try:
                minutes = int(payload.get("minutes"))
            except (TypeError, ValueError):
                self._send({"error": "minutes must be an integer"}, status=400)
                return
            if not 1 <= minutes <= 180:
                self._send({"error": "minutes out of range"}, status=400)
                return
            kind = str(payload.get("kind") or "focus")
            if kind not in ("focus", "short_break", "long_break"):
                self._send({"error": "invalid kind"}, status=400)
                return
            state.focus.log(minutes, kind, str(payload.get("task") or ""))
            self._send({"ok": True, **state.focus.snapshot()})
            return

        if path == "/api/announcements":
            action = payload.get("action")
            if action == "sync":
                if not state.token:
                    self._send({"error": "no session token"}, status=401)
                    return
                with state.lock:
                    if state.announcements_syncing:
                        self._send({"error": "already syncing"}, status=409)
                        return
                # The manual control must work immediately after startup, before the
                # separate course-list control has been used. This path loads courses
                # when needed and then uses the same sync routine as the scheduler.
                threading.Thread(target=run_scheduled_announcement_sync, args=(state,),
                                 daemon=True).start()
                self._send({"ok": True})
                return
            if action == "summarize":
                if not ai_provider.status(state.research_backend).get("ready"):
                    self._send({"error": "No AI provider is available"}, status=400)
                    return
                with state.lock:
                    if state.announcements_summarizing:
                        self._send({"error": "already summarizing"}, status=409)
                        return
                    state.announcements_summarizing = True
                    state.announcement_summary_error = ""
                threading.Thread(target=do_announcement_summary, args=(state,),
                                 daemon=True).start()
                self._send({"ok": True})
                return
            if action == "purge":
                # Collapse cross-posts in the stored feed now - no Blackboard
                # token needed, so it works offline like "read" and "add".
                folded = state.announcements.purge_duplicates()
                state.note("Announcements purged: {} duplicate(s) folded".format(folded))
                self._send({"ok": True, "folded": folded})
                return
            if action == "read":
                if not state.announcements.mark_read(str(payload.get("id") or ""),
                                                     bool(payload.get("read", True))):
                    self._send({"error": "no such announcement"}, status=404)
                    return
                self._send({"ok": True})
                return
            if action == "add":
                # A personal reminder - needs the dashboard session, not a
                # Blackboard token, so it works offline like "read".
                text = " ".join(str(payload.get("text") or "").split())
                if not text:
                    self._send({"error": "announcement text is required"}, status=400)
                    return
                if len(text) > 240:
                    self._send({"error": "announcement text must be 240 characters or fewer"},
                               status=400)
                    return
                priority = str(payload.get("priority") or "info")
                if priority not in ("info", "warning", "urgent"):
                    self._send({"error": "invalid priority"}, status=400)
                    return
                item = state.announcements.add_local(text, priority)
                self._send({"ok": True, "id": item["id"]})
                return
            if action == "delete":
                item_id = str(payload.get("id") or "")
                if not item_id.startswith("local-"):
                    self._send({"error": "only personal announcements can be deleted"},
                               status=404)
                    return
                if not state.announcements.remove_local(item_id):
                    self._send({"error": "no such announcement"}, status=404)
                    return
                self._send({"ok": True})
                return
            self._send({"error": "unknown action"}, status=400)
            return

        if path == "/api/inbound":
            action = payload.get("action")
            if action == "sync":
                if state.inbound_db:
                    self._send({"error": "configured inbound store cannot be synced"},
                               status=400)
                    return
                with state.lock:
                    if state.inbound_syncing:
                        self._send({"error": "already syncing"}, status=409)
                        return
                    state.inbound_syncing = True
                    state.inbound_error = ""
                threading.Thread(target=do_inbound_sync, args=(state,),
                                 daemon=True).start()
                self._send({"ok": True})
                return
            if action != "done":
                self._send({"error": "unknown action"}, status=400)
                return
            configured = inbound_mod.resolve_path(state.download_root, state.inbound_db)
            own_path = triage_store_mod.db_path(state.download_root)
            if os.path.abspath(configured) != os.path.abspath(own_path):
                self._send({"error": "configured inbound store is read-only"}, status=400)
                return
            ids = payload.get("ids") or [payload.get("id", "")]
            ids = [str(item) for item in ids if item][:20]
            store = triage_store_mod.TriageStore(state.download_root)
            changed = sum(store.mark_done(item) for item in ids)
            inbound_mod._CACHE.clear()
            self._send({"ok": True, "done": changed})
            return

        # Pushing operates purely on files already on disk, so it must work even when
        # the iNTUition session has expired. Handle it before the session gate below.
        if path == "/api/schedule":
            # Body is the raw file; the query string carries the original name so the
            # extension can pick the parser.
            self._send({"error": "use PUT with the file body"}, status=405)
            return

        if path == "/api/transcribe":
            with state.lock:
                if state.transcribing:
                    self._send({"error": "already transcribing"}, status=409)
                    return
                state.transcribing = True
            threading.Thread(
                target=do_transcribe, args=(state, payload.get("paths")),
                daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/transcribe/drive":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            raw_ids = payload.get("drive_ids") or []
            if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
                self._send({"error": "drive_ids must be a list of Drive file IDs"}, status=400)
                return
            with state.lock:
                if state.transcribing:
                    self._send({"error": "already transcribing"}, status=409)
                    return
                state.transcribing = True
            threading.Thread(
                target=do_transcribe_drive, args=(state, list(dict.fromkeys(raw_ids))),
                daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/study/summary":
            item_id = str(payload.get("id") or "").strip()
            prompt = str(payload.get("prompt") or "").strip()
            scope = str(payload.get("scope") or summary_mod.SCOPE_NOTES)
            if not item_id or not prompt:
                self._send({"error": "choose a material and enter a prompt"}, status=400)
                return
            if scope not in summary_mod.SCOPES:
                self._send({"error": "scope must be one of {}".format(
                    summary_mod.SCOPES)}, status=400)
                return
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            include_notes = bool(payload.get("include_notes", True))
            session_id = str(payload.get("session") or "default")[:80]
            with state.lock:
                if state.summarizing:
                    self._send({"error": "a Compendium generation is already running"},
                               status=409)
                    return
                item = next((entry for entry in state.drive_files
                            if entry["id"] == item_id), None)
                material_name = (item or {}).get("rel_path") or (item or {}).get("name") or item_id
                state.summarizing = True
                job_id = uuid.uuid4().hex[:16]
                state.summary_job = {
                    "id": job_id, "document_id": item_id, "material_name": material_name,
                    "prompt": prompt[:4000], "scope": scope, "stage": "Queued",
                    "ok": None, "done": False,
                }
            threading.Thread(
                target=do_generate_summary,
                args=(state, job_id, item_id, prompt, scope, include_notes, session_id),
                daemon=True).start()
            self._send({"ok": True, "job": job_id})
            return

        if path == "/api/study/grade":
            item_id = str(payload.get("id") or "").strip()
            filename = os.path.basename(str(payload.get("filename") or "upload"))[:200]
            if not item_id:
                self._send({"error": "choose a material first"}, status=400)
                return
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            with state.lock:
                item = next((entry for entry in state.drive_files
                            if entry["id"] == item_id), None)
            # Mirrors the frontend's own gate (only a Tutorial folder's material
            # shows the Grade tab at all) so a direct API call can't bypass it -
            # grading is scoped to weekly tutorial practice, not past-year exam
            # papers, even though those also get a solution_pairs match.
            if not item or not solution_pairs_mod.is_tutorial_material(
                    item.get("rel_path") or ""):
                self._send({"error": "Grading is only available for tutorial "
                                     "material"}, status=400)
                return
            try:
                raw_bytes, extension = validate_grade_upload(payload.get("data"))
            except ValueError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            with state.lock:
                if state.grading:
                    self._send({"error": "a grading run is already in progress"},
                               status=409)
                    return
                item = next((entry for entry in state.drive_files
                            if entry["id"] == item_id), None)
                material_name = (item or {}).get("rel_path") or (item or {}).get("name") or item_id
                state.grading = True
                job_id = uuid.uuid4().hex[:16]
                state.grading_job = {
                    "id": job_id, "document_id": item_id, "material_name": material_name,
                    "work_filename": filename, "stage": "Queued",
                    "ok": None, "done": False,
                }
            fd, work_path = tempfile.mkstemp(
                prefix="intuition_grade_upload.", suffix=extension)
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
            threading.Thread(
                target=do_grade_tutorial,
                args=(state, job_id, item_id, work_path, filename),
                daemon=True).start()
            self._send({"ok": True, "job": job_id})
            return

        if path == "/api/study/summary/delete":
            summary_id = str(payload.get("id") or "").strip()
            if not summary_id:
                self._send({"error": "summary id is required"}, status=400)
                return
            row = state.notebook.delete_summary(summary_id)
            if not row:
                self._send({"error": "no such summary"}, status=404)
                return
            delete_summary_files(row)
            with state.lock:
                # The job just deleted may still be the one shown as "last result" -
                # clear it so the UI doesn't keep offering a PDF that no longer exists.
                if state.summary_job and state.summary_job.get("id") == summary_id:
                    state.summary_job = None
            self._send({"ok": True})
            return

        if path == "/api/push":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            with state.lock:
                if state.pushing:
                    self._send({"error": "already pushing"}, status=409)
                    return
                state.pushing = True
            threading.Thread(target=do_push, args=(state,), daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/drive/client-secret":
            raw = payload.get("json")
            if not isinstance(raw, str) or not raw.strip():
                self._send({"error": "paste the downloaded OAuth client JSON"},
                           status=400)
                return
            if len(raw) > 64 * 1024:
                self._send({"error": "that file is too large to be an OAuth client JSON"},
                           status=400)
                return
            verdict = drive.save_client_secret(raw)
            if not verdict.get("ok"):
                self._send({"error": verdict.get("problem") or "invalid client JSON"},
                           status=400)
                return
            state.note("Drive OAuth client stored ({})".format(
                verdict.get("project") or verdict.get("client_id") or "desktop app"))
            self._send({"ok": True, "client_id": verdict.get("client_id"),
                        "project": verdict.get("project")})
            return

        if path == "/api/drive/connect":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            with state.lock:
                if state.drive_linking:
                    self._send({"error": "a Drive authorisation is already in progress"},
                               status=409)
                    return
                state.drive_linking = True
                state.drive_link_error = ""
            threading.Thread(target=do_drive_link, args=(state,), daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/drive/disconnect":
            with state.lock:
                linking = state.drive_linking
            if linking:
                self._send({"error": "a Drive authorisation is in progress"}, status=409)
                return
            drive.disconnect()
            with state.lock:
                state.drive_files = []
                state.external_drive_files = {}
                state.drive_link_error = ""
            state.note("Drive disconnected")
            self._send({"ok": True})
            return

        if path == "/api/drive/list":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            with state.lock:
                if state.drive_listing:
                    self._send({"error": "already refreshing Drive"}, status=409)
                    return
                state.drive_listing = True
            threading.Thread(target=do_drive_list, args=(state,), daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/drive/pull":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            raw_ids = payload.get("ids") or []
            if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
                self._send({"error": "ids must be a list of Drive file IDs"}, status=400)
                return
            ids = list(dict.fromkeys(raw_ids))
            if not ids:
                self._send({"error": "no Drive files selected"}, status=400)
                return
            with state.lock:
                if state.pulling:
                    self._send({"error": "already pulling"}, status=409)
                    return
                state.pulling = True
            threading.Thread(target=do_drive_pull, args=(state, ids), daemon=True).start()
            self._send({"ok": True})
            return


        if path == "/api/drive/search":
            query = str(payload.get("query") or "").strip()[:200]
            limit = drive.SEARCH_LIMIT
            with state.lock:
                files = list(state.drive_files)
                listing = state.drive_listing
            if listing:
                self._send({"error": "Drive inventory is still refreshing"}, status=409)
                return
            try:
                service = drive.build_service(interactive=False)
                items = drive.native_search(service, files, query, limit=limit)
            except Exception as exc:  # noqa: BLE001 - keep metadata search usable offline
                state.note("Drive full-text search unavailable: {}".format(exc))
                items = drive.semantic_search(files, query, limit=limit)
            self._send({"items": items, "total": len(files), "query": query,
                        "engine": "Google Drive content index"})
            return

        if path == "/api/drive/search_mydrive":
            if not drive.credentials_present():
                self._send({"error": drive.SETUP_HELP}, status=400)
                return
            query = str(payload.get("query") or "").strip()[:200]
            if not query:
                self._send({"error": "query must not be empty"}, status=400)
                return
            try:
                service = drive.build_service(interactive=False)
                results = drive.search_my_drive(service, query)
            except drive.DriveError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            except Exception as exc:  # noqa: BLE001 - report Drive search failures
                state.note("Drive search (My Drive) failed: {}".format(exc))
                self._send({"error": "Drive search failed"}, status=500)
                return
            with state.lock:
                state.external_drive_files.update({f["id"]: f for f in results})
                state.drive_files = _merge_external_drive_files(state, state.drive_files)
            self._send({"files": results})
            return

        if path == "/api/drive/learn":
            item_id = str(payload.get("id") or "")
            session_id = str(payload.get("session") or "default")[:80]
            question = str(payload.get("question") or "").strip()[:2000]
            try:
                snapshot = validate_learning_snapshot(payload.get("snapshot"))
            except ValueError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            if not item_id or not question:
                self._send({"error": "choose a material and enter a question"}, status=400)
                return
            with state.lock:
                item = next((dict(entry) for entry in state.drive_files
                             if entry["id"] == item_id), None)
            if not item:
                self._send({"error": "material is not in the current Drive index"}, status=404)
                return
            try:
                try:
                    history = state.chat_memory.recent(session_id, item_id, limit=8)
                except Exception as memory_exc:  # noqa: BLE001 - answer even if local history is damaged
                    state.note("FRIDAY chat history unavailable: {}".format(memory_exc))
                    history = []
                service = drive.build_service(interactive=False)
                with tempfile.TemporaryDirectory(prefix="intuition-learn-") as tmp:
                    target = drive.pull_file(service, item, tmp)
                    material = drive.extract_learning_text(target)
                conversation = "\n".join(
                    "{}: {}".format(turn["role"].title(), turn["content"][:4000])
                    for turn in history)
                prompt = "Material: {}\n\n---\n{}\n---\n\nRecent conversation:\n{}\n\nStudent question: {}".format(
                        item.get("rel_path") or item.get("name"), material,
                        conversation or "(none)", question)
                snapshot_note = ""
                if snapshot:
                    try:
                        result = omniroute_provider.complete_image(
                            prompt, DRIVE_LEARNING_SYSTEM, snapshot,
                            max_tokens=FRIDAY_MAX_TOKENS, timeout=180)
                    except Exception as vision_exc:  # noqa: BLE001 - text fallback keeps vision failures non-fatal
                        state.note("FRIDAY vision unavailable: {}".format(vision_exc))
                        snapshot_note = "Snapshot vision is temporarily unavailable. FRIDAY answered from the document text instead."
                        result = ai_provider.complete_tier(
                            "chat",
                            prompt + "\n\nThe selected snapshot could not be decoded by the vision provider. Answer from the extracted material and state briefly if the requested visual detail cannot be verified.",
                            DRIVE_LEARNING_SYSTEM, preferred=state.research_backend,
                            max_tokens=FRIDAY_MAX_TOKENS, download_root=state.download_root)
                else:
                    result = ai_provider.complete_tier(
                        "chat", prompt, DRIVE_LEARNING_SYSTEM, preferred=state.research_backend,
                        max_tokens=FRIDAY_MAX_TOKENS, download_root=state.download_root)
                answer = result.get("text") or ""
                if not answer.strip():
                    raise RuntimeError("FRIDAY returned an empty answer")
                # The chat surfaces' budget is generous but finite, and the system
                # prompt invites the model to expand well past its 450-word default
                # when a student asks for depth (tables, several worked examples,
                # flashcards). Silently returning a reply chopped mid-sentence is
                # worse than telling the reader it was cut off - matches the
                # truncation check summary.py's _extract_body already does for
                # Compendium, applied here instead of failing the whole answer.
                if result.get("finish_reason") in _FRIDAY_TRUNCATED_FINISH_REASONS:
                    answer += "\n\n*(Answer cut off by length limit - ask to continue for more.)*"
                try:
                    state.chat_memory.add(session_id, item_id, "user", question)
                    state.chat_memory.add(session_id, item_id, "assistant", answer,
                                          result.get("backend"), result.get("model"))
                except Exception as memory_exc:  # noqa: BLE001 - local memory must not discard a valid answer
                    state.note("FRIDAY chat memory unavailable: {}".format(memory_exc))
                self._send({"answer": answer, "backend": result.get("backend"),
                            "model": result.get("model"), "memory": "local",
                            "snapshot_note": snapshot_note})
            except Exception as exc:  # noqa: BLE001 - surface material/provider failures
                self._send({"error": str(exc)}, status=500)
            return

        if not state.token:
            self._send({"error": "no session token"}, status=401)
            return
        with state.lock:
            if state.session_rejected:
                self._send({"error": "session rejected; paste a fresh BbRouter cookie"},
                           status=401)
                return

        if path == "/api/sync":
            with state.lock:
                if state.unified_syncing:
                    self._send({"error": "sync already running"}, status=409)
                    return
                state.unified_syncing = True
                state.unified_sync_error = ""
            threading.Thread(target=do_unified_sync, args=(state,), daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/courses":
            token, generation = state.session_snapshot()
            try:
                courses = get_courses(
                    token, prefer_rest=state.prefer_rest,
                    scope=state.scope, download_root=state.download_root,
                )
            except Exception as e:  # noqa: BLE001
                self._send({"error": str(e)}, status=500)
                return
            if not state.session_current(token, generation):
                self._send({"error": "session changed while loading courses"}, status=409)
                return
            with state.lock:
                state.courses = [{"name": n, "id": i} for n, i in courses]
            threading.Thread(target=do_todo_sync, args=(state,), daemon=True).start()
            state.note("Found {} {}".format(
                len(courses),
                "course(s) for " + semester_mod.format_semester(
                    semester_mod.current_semester())
                if state.scope == api_mod.SCOPE_SEMESTER else "course(s)"))
            with state.lock:
                if not state.announcements_syncing:
                    state.announcements_syncing = True
                    state.announcement_errors = []
                    threading.Thread(target=do_announcement_sync, args=(state,),
                                     daemon=True).start()
            self._send({"ok": True})
            return

        if path == "/api/scan":
            with state.lock:
                if state.scanning:
                    self._send({"error": "already scanning"}, status=409)
                    return
                state.scanning = True
                state.scan_errors = []
                state.log.append("Starting scan")
            token, generation = state.session_snapshot()
            threading.Thread(
                target=do_scan,
                args=(state, payload.get("course_ids", []), token, generation),
                daemon=True,
            ).start()
            self._send({"ok": True})
            return

        if path == "/api/download":
            with state.lock:
                if state.downloading:
                    self._send({"error": "already downloading"}, status=409)
                    return
                state.downloading = True
            token, generation = state.session_snapshot()
            threading.Thread(
                target=do_download,
                args=(state, payload.get("paths", []), token, generation), daemon=True
            ).start()
            self._send({"ok": True})
            return

        self._send({"error": "not found"}, status=404)


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PAGE_PATH = os.path.join(STATIC_DIR, "dashboard.html")


def static_asset_path(url_path: str) -> Optional[str]:
    """Resolve a packaged dashboard asset without allowing directory traversal."""
    prefix = "/static/"
    if not url_path.startswith(prefix):
        return None
    relative = url_path[len(prefix):].replace("/", os.sep)
    candidate = os.path.realpath(os.path.join(STATIC_DIR, relative))
    root = os.path.realpath(STATIC_DIR)
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    return candidate if os.path.isfile(candidate) else None


def load_page() -> str:
    """Read the HUD page off disk.

    Kept as a separate file rather than a giant string literal so the markup, CSS and
    JS stay editable with normal tooling. Read per request (it is a few KB) so design
    tweaks show up on refresh without restarting the server.
    """
    with open(PAGE_PATH, encoding="utf-8") as f:
        return f.read()


def omniroute_watchdog(state: State, stop: threading.Event,
                       interval: float = OMNIROUTE_WATCH_SECONDS):
    """Keep the dashboard-owned local gateway healthy for the dashboard lifetime."""
    while not stop.wait(interval):
        try:
            omniroute_provider.ensure_running()
        except omniroute_provider.OmniRouteError as exc:
            state.note("OmniRoute watchdog: {}".format(exc))


def serve(download_root: str, port: int = DEFAULT_PORT, prefer_rest: bool = True,
          open_browser: bool = True, drive_folder: str = drive.DEFAULT_ROOT_FOLDER,
          move: bool = True, scope: str = api_mod.SCOPE_SEMESTER,
          transcribe_model: str = transcribe_mod.DEFAULT_MODEL,
          inbound_db: Optional[str] = None,
          summary_retention_days: int = SUMMARY_RETENTION_DAYS_DEFAULT):
    Handler.state = State(
        os.path.abspath(download_root), prefer_rest=prefer_rest,
        drive_folder=drive_folder, move=move, scope=scope,
        transcribe_model=transcribe_model,
    )
    Handler.state.inbound_db = inbound_db
    # Populate the transcript survey up front so the UI is accurate before any scan.
    Handler.state.refresh_media()
    # Bind the local listener before starting optional background integrations. A slow
    # or broken Outlook/Drive/OmniRoute probe must never delay dashboard availability.
    server = ThreadingHTTPServer((DEFAULT_HOST, port), Handler)
    server.daemon_threads = True
    service_stop = threading.Event()
    threading.Thread(target=Handler.state.refresh_identity, daemon=True).start()
    # Probe the Claude CLI version once, up front. snapshot() reads it on every
    # /api/state poll; doing it here keeps that subprocess off the request path.
    threading.Thread(target=research_mod.warm_cli_version, daemon=True).start()
    threading.Thread(target=announcement_sync_scheduler, args=(Handler.state,),
                     kwargs={"stop": service_stop},
                     daemon=True).start()
    threading.Thread(target=inbound_poll_scheduler, args=(Handler.state,),
                     kwargs={"stop": service_stop},
                     daemon=True).start()
    threading.Thread(target=sweep_old_summaries,
                     args=(Handler.state, summary_retention_days), daemon=True).start()
    # Drive search should be ready without making the user understand or manually
    # build an index. Only start silently when an existing OAuth token can be reused.
    if drive.credentials_present() and drive.token_present():
        Handler.state.drive_listing = True
        threading.Thread(target=do_drive_list, args=(Handler.state,), daemon=True).start()
    if omniroute_provider.managed_locally():
        threading.Thread(target=omniroute_watchdog,
                         args=(Handler.state, service_stop), daemon=True).start()
    url = "http://{}:{}/".format(DEFAULT_HOST, port)
    print("iNTUition: {}".format(url))
    print("Staging to:  {}".format(Handler.state.download_root))
    print("Drive root:  {}/  ({})".format(
        drive_folder, "move" if move else "copy"))
    print("Scope:       {}".format(
        "current semester (" + semester_mod.format_semester(
            semester_mod.current_semester()) + ")"
        if scope == api_mod.SCOPE_SEMESTER else scope))
    if not drive.credentials_present():
        print("Drive not configured - run: "
              "python -m intuition.drive_push --setup")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        service_stop.set()
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(
        description="iNTUition - local sync dashboard for iNTUition"
    )
    parser.add_argument(
        "--download_to", default="NTU", help="Local sync folder (default: NTU)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--inbound_db",
        help="Path to a triage flag store, to show flagged NTU mail. Defaults to "
             "this folder's own triage.db; the panel hides itself until the first "
             "scan writes one (python -m intuition.triage_run --scan).")
    parser.add_argument(
        "--legacy", action="store_true", help="Force the Original-view HTML scraper"
    )
    parser.add_argument(
        "--no_browser", action="store_true", help="Do not open a browser window"
    )
    parser.add_argument(
        "--drive_folder",
        default=drive.DEFAULT_ROOT_FOLDER,
        help="Destination root folder in Google Drive (default: %(default)s)",
    )
    parser.add_argument(
        "--keep_local",
        action="store_true",
        help="Copy to Drive instead of moving: keep local files after upload",
    )
    parser.add_argument(
        "--scope",
        default=api_mod.SCOPE_SEMESTER,
        choices=api_mod.SCOPES,
        help="Which courses to read: semester (default, derived from today's date) "
             "or favourites (Ultra stars)",
    )
    parser.add_argument(
        "--transcribe_model",
        default=transcribe_mod.DEFAULT_MODEL,
        help="Whisper model for transcripts (default: %(default)s)",
    )
    parser.add_argument(
        "--summary_retention_days",
        type=int,
        default=SUMMARY_RETENTION_DAYS_DEFAULT,
        help="Delete Compendium summaries (DB row and .tex/.pdf) older than this "
             "many days, swept once at startup. 0 disables the sweep (default: "
             "%(default)s)",
    )
    args = parser.parse_args()
    serve(
        args.download_to,
        port=args.port,
        prefer_rest=not args.legacy,
        open_browser=not args.no_browser,
        drive_folder=args.drive_folder,
        move=not args.keep_local,
        scope=args.scope,
        transcribe_model=args.transcribe_model,
        inbound_db=args.inbound_db,
        summary_retention_days=args.summary_retention_days,
    )


if __name__ == "__main__":
    main()
