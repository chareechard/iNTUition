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
import hashlib
import json
import mimetypes
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
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
from intuition import saved_topics as saved_topics_mod
from intuition.chat_memory import ChatMemory
from intuition.notes import Notebook, NoteConflict, html_to_text
from intuition.sync import (
    DOWNLOADABLE, PUSHABLE, build_plan, recover_restructured, summarize)
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


# lab_analysis.py validates every field below before it reaches the page - a
# tighter prompt just means less for that validator to have to reject. The
# "steps" field is this app's own addition to the brief's contract: an
# "initialState" alone can only paint one static picture, not drive the
# Simulation tab's Play/Step/Speed timeline, so the model additionally
# mentally traces its own execution into a short operation log.
LAB_BLUEPRINT_SYSTEM = """You are FRIDAY, reading one source file open in a student's local Python/Java IDE. Identify the primary algorithm it implements and respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: {"detectedAlgorithm": "<name, e.g. Dijkstra's Shortest Path, QuickSort, Bubble Sort, Binary Search - or \\"Not identified\\" if the file implements no recognizable algorithm>", "paradigm": "<e.g. Greedy, Divide and Conquer, Dynamic Programming, Brute Force, Backtracking - or \\"Unknown\\">", "timeComplexity": "<Big-O in terms of the code's own variable names, e.g. O(N log N) - or \\"Unknown\\">", "spaceComplexity": "<Big-O - or \\"Unknown\\">", "criticalLines": [{"line": <1-based line number from the numbered source below>, "purpose": "<short explanation of that line's operational importance>"}] (at most 8, only the lines that matter most - never invent a line number outside the file), "simulationModel": {"type": "\\"array\\", \\"graph\\", \\"tree\\" or \\"none\\"", "initialState": <for "array": the JSON array of starting values the code operates on; for "graph"/"tree": {"nodes": [...], "edges": [{"from":..., "to":..., "weight":...}]}; use null with type "none" if nothing in the file has a structure worth animating>}, "steps": [<at most 60 steps tracing the algorithm's own execution against simulationModel.initialState, each either {"op":"compare","indices":[i,j],"line":<n>}, {"op":"swap","indices":[i,j],"line":<n>}, {"op":"set","indices":[i],"value":<v>,"line":<n>}, {"op":"visit","node":"<id>","line":<n>}, or {"op":"edge","from":"<id>","to":"<id>","weight":<w>,"line":<n>} - omit "steps" entirely (or leave it empty) if simulationModel.type is "none" or you cannot trace real execution>]}. Base every field only on what the code in front of you actually does; never invent an algorithm, complexity, or step the code does not support."""

# The model is not allowed to silently turn a guessed trace into a different
# algorithm: array indices are zero-based, and a dynamic/unknown input means
# no simulation rather than an invented static example. lab_analysis validates
# this contract before the canvas sees the response.
LAB_BLUEPRINT_SYSTEM += """ Use the complete numbered source, including helper functions and the entry point. For simulationModel.type "array", initialState must be a literal array the source actually creates, and every indices value is zero-based and must refer to that array. Only emit a step when that operation really occurs in the shown execution; do not fabricate a canonical textbook trace. If inputs are read from stdin, generated randomly, or otherwise cannot be known from the source, use type "none". For graph/tree steps, only reference nodes and edges present in initialState. If you cannot trace the concrete execution confidently, leave steps empty."""

# ureca.py validates every field below before it reaches the store - draft
# text is a starting point for the student to edit, never a finished
# proposal, so the model is told to stay modest and never invent specifics.
# Runs on the "scholar" tier (see ai_provider.TIERS) rather than "chat": this
# is a single deep autonomous pass over the whole proposal, not a quick
# reformat, so it gets the slower, stronger rung and a longer deadline.
URECA_DRAFT_SYSTEM = """You are doing autonomous research for an NTU undergraduate who gave you a one-line idea and needs a first-draft URECA (Undergraduate Research Experience on CAmpus) project proposal. URECA is NTU's self-proposed undergraduate research programme: a student drafts a proposal, a faculty supervisor accepts and registers it, and the student spends the August-to-June academic year on the project, finishing with an abstract, a poster and a final paper; consumable spending is capped at $500. Think like a researcher scoping a feasible undergraduate project, not a copywriter padding out a summary: reason about what makes this idea tractable in about 11 months, what a realistic method looks like, and what could plausibly go wrong or be out of scope. Respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: {"background": "<2-4 sentences: the problem or gap and why it matters, grounded only in what the student described>", "objectives": "<2-4 concrete, checkable research objectives, written as short sentences>", "methodology": "<3-5 sentences: a specific, realistic approach an undergraduate could carry out over about 11 months - name concrete methods, tools or data sources where you can>", "outcomes": "<2-3 sentences: the expected contribution, tied to URECA's own deliverables of an abstract, poster and final paper>", "budgetNotes": "<1-3 sentences: what consumables or small costs this plausibly needs, staying within the $500 cap - say \\"No consumables anticipated\\" if none>", "timelineNotes": "<2-4 sentences: a rough month-by-month or phase-by-phase plan spanning August to June>"}. Write a first draft the student can edit, not a finished document - stay concrete, and never invent citations, data, prior results, or specifics the student did not mention. It is fine, and expected, to reason from general domain knowledge about feasibility and method - that is the research; just do not fabricate sources or claim specific prior findings you cannot support."""

# The first, cheap step of the Research tab's autonomous flow: propose a short
# list of candidate topics before any deep research runs. The student still
# picks one explicitly (see /api/research action=suggest and the "Research
# this" cards in the UI) - this only replaces having to type a title and a
# one-line idea by hand, not the explicit press itself.
RESEARCH_SUGGEST_ATTEMPTS = 2
# Research suggestions must be driven by the profile supplied in the Research tab.
# No programme, year, department, course history, or supervisor preference is
# assumed when a new user installs the project.
RESEARCH_SUGGEST_ATTEMPTS = 2
RESEARCH_SUGGEST_SYSTEM = """You propose URECA (Undergraduate Research Experience on CAmpus) project ideas for an undergraduate, tailored only to the programme, year, field of study, and interests supplied in the user's research profile. URECA is NTU's self-proposed undergraduate research programme: a student drafts a proposal, a faculty supervisor accepts and registers it, and the student spends the August-to-June academic year on the project, finishing with an abstract, a poster and a final paper; consumable spending is capped at $500. Do not assume a specific programme, year, department, course list, prior knowledge, or research experience. If the profile is incomplete, keep ideas accessible and state what background would need to be learned. Propose ideas that pose a non-trivial, answerable research question and combine at least two skills such as proof and counterexample, mathematical modelling, algorithm design and complexity analysis, numerical implementation, or experimental evaluation. Make the challenge visible in the topic sentence through a concrete method, comparison, conjecture, or measurable criterion - not through buzzwords or an oversized application. Scope each project so the student can learn missing background, build a defensible baseline, investigate one focused extension, and produce a meaningful result in about 11 months; a good idea should be demanding but finishable, not a survey, a generic app, a toy coding exercise, training a large model from scratch, or an open-ended attempt at a famous unsolved problem. If the student supplied interests or keywords, use them as the anchor for every idea - each one should visibly grow out of a stated interest, and none should drift onto an unrelated theme. Aim for a spread across the theory-to-application range while keeping every idea within the supplied level and field. Use concrete methods and domain terms so each idea is actionable and easy for the student to evaluate; never invent or name a supervisor. Respond with strict JSON only - no prose, no markdown fences, no explanation. Schema: a JSON array of 3 to 5 objects, each {"title": "<a short, concrete project title, under 12 words>", "topic": "<one sentence pitching the idea, specific enough to hand straight to a research pass - not a vague theme>"}. Make the ideas genuinely different from each other in approach or subfield. Never invent a specific supervisor, dataset, or prior result; keep each idea grounded in the student's stated profile."""
OMNIROUTE_WATCH_SECONDS = 15

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
    routes = [model for model in omniroute_provider.models()
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
        self.lock = threading.Lock()
        self.download_root = download_root
        self.prefer_rest = prefer_rest
        self.scope = scope
        self.transcribe_model = transcribe_model
        self.transcribing = False
        self.transcribe_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self._transcriber = None
        # One Compendium generation at a time, like transcribing/pushing - a 60-120s
        # job with its own worker thread. summary_job holds the single most recent
        # job's live status; the job id lets a poll from a stale tab notice it is
        # looking at an older run rather than silently showing the wrong one.
        self.summarizing = False
        self.summary_job: Optional[Dict] = None
        self.media_survey: List[Dict] = []
        self.identity: Optional[Dict] = None
        self.drive_folder = drive_folder
        self.move = move
        self.token: Optional[str] = auth.load_token()
        self.courses: List[Dict] = []
        self.plan: List[Dict] = []
        self.skipped: List[str] = []
        self.scanning = False
        self.scan_errors: List[str] = []
        self.downloading = False
        self.pushing = False
        self.drive_listing = False
        self.pulling = False
        self.drive_files: List[Dict] = []
        self.pull_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self.log: List[str] = []
        self.progress = {"done": 0, "total": 0, "current": "", "bytes": 0}
        self.push_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self.ledger = Ledger(download_root)
        self.cache = ContentCache(download_root)
        self.schedule = schedule_mod.Schedule(download_root)
        self.todo = todo_mod.Queue(download_root)
        self.todo_researching: set = set()
        self.todo_research_errors: Dict[str, str] = {}
        self.announcements = announcements_mod.Feed(download_root)
        self.chat_memory = ChatMemory(download_root)
        self.notebook = Notebook(download_root)
        self.lab_workspace = lab_mod.Workspace(download_root)
        self.lab_jobs = lab_mod.JobManager()
        self.ureca = ureca_mod.Store(download_root)
        self.profile = profile_mod.Store(download_root)
        self.saved_topics = saved_topics_mod.Store(download_root)
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
            self.lab_workspace = lab_mod.Workspace(root)
            self.ureca = ureca_mod.Store(root)
            self.profile = profile_mod.Store(root)
            self.saved_topics = saved_topics_mod.Store(root)
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

    def refresh_identity(self):
        """Look up who the session belongs to. Cached: it never changes mid-session."""
        if not self.token:
            with self.lock:
                self.identity = None
            return None
        try:
            from intuition import rest
            who = rest.get_me(self.token)
        except Exception:  # noqa: BLE001 - identity is cosmetic, never block on it
            who = None
        with self.lock:
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
        with self.lock:
            token_expiry = None
            if self.token:
                expires = auth.expires_at(self.token)
                token_expiry = expires
            ai_status = ai_provider.status(self.research_backend)
            _todo_backend, todo_ai_status = todo_ai_choice()
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
                "transcribe": {
                    "model": self.transcribe_model,
                    # Local survey plus whatever the already-cached Drive index
                    # (self.drive_files, refreshed by "Index"/do_drive_list) turns
                    # out to hold - move mode deletes a video locally once it is
                    # archived, so that is the only place left to detect it. Pure
                    # in-memory classification, no extra Drive calls on every poll.
                    "media": self.media_survey
                             + transcribe_mod.classify_drive_media(self.drive_files),
                },
                "progress": dict(self.progress),
                "push_progress": dict(self.push_progress),
                "pull_progress": dict(self.pull_progress),
                "drive": {
                    "folder": self.drive_folder,
                    "move": self.move,
                    "configured": drive.credentials_present(),
                    "linked": drive.token_present(),
                    "archived": len(self.ledger),
                    "listing": self.drive_listing,
                    "file_count": len(self.drive_files),
                },
                "schedule": self._schedule_snapshot(),
                "announcements": dict(self.announcements.snapshot(),
                                      syncing=self.announcements_syncing,
                                      errors=list(self.announcement_errors),
                                      summarizing=self.announcements_summarizing,
                                      summary_error=self.announcement_summary_error,
                                      schedule_error=self.announcement_schedule_error,
                                      ai=ai_status),
                "inbound": dict(inbound_mod.snapshot(
                    inbound_mod.resolve_path(self.download_root, self.inbound_db)),
                    syncing=self.inbound_syncing, error=self.inbound_error),
                "todo": dict(self.todo.snapshot(),
                             busy=sorted(self.todo_researching),
                             errors=dict(self.todo_research_errors),
                             ai=todo_ai_status,
                             model_options={backend: [{"value": value, "label": label}
                                                     for value, label in options]
                                            for backend, options
                                            in todo_model_options().items()},
                             backends=list(TODO_BACKENDS)),
                "log": list(self.log[-40:]),
            }


def do_scan(state: State, course_ids: List[str]):
    """Fetch content trees for the chosen courses and diff them against disk."""
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
                    state.token,
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
            state.scanning = False


def do_announcement_sync(state: State):
    try:
        errors = state.announcements.sync(state.token, list(state.courses))
        with state.lock:
            state.announcement_errors = errors
        state.note("Announcements synced: {} across {} course(s){}".format(
            len(state.announcements.items), len(state.courses),
            "; {} failed".format(len(errors)) if errors else ""))
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
    """Refresh courses if necessary, then pull announcements from Blackboard."""
    if not state.token:
        state.note("Scheduled announcement sync skipped: no Blackboard session")
        return
    if not state.courses:
        try:
            courses = get_courses(
                state.token, prefer_rest=state.prefer_rest, scope=state.scope,
                download_root=state.download_root)
        except Exception as exc:  # noqa: BLE001
            state.note("Scheduled announcement sync could not load courses: {}".format(exc))
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
        do_announcement_sync(state)
    except Exception as exc:  # noqa: BLE001
        state.note("Scheduled announcement sync failed: {}".format(exc))


def announcement_sync_scheduler(state: State):
    """Poll announcements on startup, then at the start and end of every local day.

    This thread only runs while the app is open, so without the startup sync a
    post made while it was closed sits unsynced until the next 07:00/23:00
    window fires *after* the app happens to be running - up to 16h late, or a
    whole window missed if it's closed again by then. Mirrors
    inbound_poll_scheduler's startup poll for mail.
    """
    waiter = threading.Event()
    run_scheduled_announcement_sync(state)
    while True:
        waiter.wait(seconds_until_next_announcement_sync())
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


def do_todo_sync(state: State):
    """Refresh Active Neural Queries from Learn's in-scope course To Do feed."""
    try:
        items = rest_mod.get_todo_items(state.token, list(state.courses))
        count = state.todo.sync_ntulearn(items)
        state.todo.save()
        state.note("iNTUition To Do synced: {} active neural query(s)".format(count))
    except Exception as exc:  # keep course-list sync useful if calendars fail
        state.note("iNTUition To Do sync failed: {}".format(exc))


def do_download(state: State, paths: List[str]):
    """Download the selected plan entries."""
    try:
        wanted = [e for e in state.plan if e["path"] in set(paths)]
        with state.lock:
            state.progress = {
                "done": 0,
                "total": len(wanted),
                "current": "",
                "bytes": 0,
            }

        for entry in wanted:
            with state.lock:
                state.progress["current"] = entry["rel_path"]

            try:
                if entry["type"] == "file":
                    link = get_file_download_link(
                        state.token, entry["predownload_link"]
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
                        state.token, entry["predownload_link"]
                    )
                    target = entry["path"]

                # An updated file must replace the stale copy; download() refuses to
                # overwrite, so clear it first.
                if entry["status"] == "updated" and os.path.isfile(target):
                    os.remove(target)

                def on_progress(downloaded, total, _entry=entry):
                    with state.lock:
                        state.progress["bytes"] = downloaded

                download(state.token, link, target, callback=on_progress)
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
            state.downloading = False
            state.progress["current"] = ""



def do_transcribe(state: State, paths: List[str] = None):
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
                state._transcriber.transcribe(path, progress=on_progress)
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


def do_transcribe_drive(state: State, drive_ids: List[str] = None):
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

                    written = state._transcriber.transcribe(local, progress=on_progress)
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
                state.summary_job.update({
                    "stage": "Failed", "ok": False, "done": True,
                    "error_stage": result.stage, "errors": result.errors,
                    "tex": result.tex,
                })
            state.note("Compendium failed for {}: {}".format(
                material_name, "; ".join(result.errors[:2]) or result.stage))
            return

        set_stage("Saving")
        summaries_dir = os.path.join(state.download_root, top_folder, "summaries")
        os.makedirs(summaries_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = os.path.splitext(os.path.basename(material_name))[0]
        tex_path = os.path.join(
            summaries_dir, bounded_filename(summaries_dir, "{}-{}.tex".format(stem, stamp)))
        pdf_path = os.path.join(
            summaries_dir, bounded_filename(summaries_dir, "{}-{}.pdf".format(stem, stamp)))
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(result.tex)
        with open(pdf_path, "wb") as f:
            f.write(result.pdf)

        state.notebook.save_summary(
            job_id, document_id=item_id, material_name=material_name,
            prompt=prompt, scope=scope, backend=result.backend or "",
            model=result.model or "", rung=result.rung or "",
            tex_path=tex_path, pdf_path=pdf_path,
            page_anchors=result.pages_cited, ok=True, error="", report=result.report)

        with state.lock:
            state.summary_job.update({
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


def do_drive_list(state: State):
    try:
        service = drive.build_service()
        roots = [state.drive_folder]
        if state.drive_folder == drive.DEFAULT_ROOT_FOLDER:
            roots.extend(root for root in drive.LEGACY_ROOT_FOLDERS
                         if root not in roots)
        by_id = {}
        counts = []
        for root in roots:
            listed = drive.DriveMirror(service, root_folder=root).list_files()
            counts.append("{}: {}".format(root, len(listed)))
            for item in listed:
                by_id.setdefault(item["id"], item)
        files = sorted(by_id.values(), key=lambda item: item["rel_path"].lower())
        with state.lock:
            state.drive_files = files
        state.note("Drive inventory: {} file(s) ({})".format(
            len(files), ", ".join(counts)))
    except Exception as exc:  # noqa: BLE001 - expose optional Drive failures in console
        state.note("Drive inventory failed: {}".format(exc))
    finally:
        with state.lock:
            state.drive_listing = False


def do_drive_pull(state: State, ids: List[str]):
    try:
        try:
            service = drive.build_service()
        except drive.DriveError as e:
            state.note("Drive unavailable: {}".format(e))
            return
        with state.lock:
            allowed = {item["id"]: dict(item) for item in state.drive_files}
        targets = []
        for item_id in ids:
            if item_id in allowed:
                targets.append(allowed[item_id])
            else:
                try:
                    res = service.files().get(
                        fileId=item_id,
                        fields="id,name,mimeType,size,modifiedTime",
                        supportsAllDrives=True
                    ).execute()
                    item = {
                        "id": res["id"],
                        "name": res.get("name", item_id),
                        "rel_path": res.get("name", item_id),
                        "mime_type": res.get("mimeType") or "application/octet-stream",
                        "size": int(res.get("size") or 0),
                        "modified": res.get("modifiedTime"),
                    }
                    targets.append(item)
                    with state.lock:
                        state.drive_files.append(item)
                except Exception as exc:  # noqa: BLE001
                    state.note("Could not resolve metadata for Drive file {}: {}".format(item_id, exc))
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


def do_unified_sync(state: State):
    """Refresh course nodes, announcements, and inbound mail from one action."""
    errors = []
    try:
        try:
            courses = get_courses(state.token, prefer_rest=state.prefer_rest,
                                  scope=state.scope, download_root=state.download_root)
            with state.lock:
                state.courses = [{"name": name, "id": course_id}
                                 for name, course_id in courses]
            do_todo_sync(state)
            state.note("Unified sync refreshed {} course nodes".format(len(courses)))
        except Exception as exc:  # noqa: BLE001
            errors.append("Course nodes: {}".format(exc))

        if state.courses:
            with state.lock:
                state.announcements_syncing = True
                state.announcement_errors = []
            try:
                do_announcement_sync(state)
            except Exception as exc:  # noqa: BLE001
                errors.append("Announcements: {}".format(exc))
        else:
            errors.append("Announcements: no course nodes available")

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


def inbound_poll_scheduler(state: State):
    """Poll Outlook on startup and every twelve hours thereafter."""
    waiter = threading.Event()
    run_scheduled_inbound_poll(state)
    while True:
        waiter.wait(INBOUND_POLL_SECONDS)
        run_scheduled_inbound_poll(state)


class Handler(BaseHTTPRequestHandler):
    state: State = None  # set by serve()

    def log_message(self, *args):
        pass  # keep the console clean; the UI has its own log

    def _send(self, payload: Dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client went away mid-response; not a real error
        except Exception as exc:  # noqa: BLE001 - last-resort dashboard telemetry
            self.state.note("GET {} failed: {}".format(self.path, exc))
            try:
                self._send({"error": str(exc) or "request failed"}, status=500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = load_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
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
        if path == "/api/drive/memory":
            query = parse_qs(parsed.query)
            session_id = (query.get("session") or [""])[0]
            item_id = (query.get("id") or [""])[0]
            if not session_id or not item_id:
                self._send({"error": "session and material id are required"}, status=400)
                return
            self._send({"items": self.state.chat_memory.recent(session_id, item_id)})
            return
        if path == "/api/lab/tree":
            self._send({
                "tree": self.state.lab_workspace.tree(),
                "inputKinds": self.state.lab_workspace.input_kinds(),
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
                content = self.state.lab_workspace.read(rel_path)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=404)
                return
            self._send({"content": content})
            return
        if path == "/api/research":
            self._send(self.state.ureca.snapshot())
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
                self._send({"error": "refresh Drive and select a listed file"}, status=404)
                return
            try:
                service = drive.build_service(interactive=False)
                with tempfile.TemporaryDirectory(prefix="intuition-preview-") as tmp:
                    target = drive.pull_file(service, item, tmp)
                    with open(target, "rb") as stream:
                        body = stream.read()
                # mimetypes.guess_type doesn't know source-code extensions like .java or
                # .cpp (returns None), which would otherwise fall through to
                # application/octet-stream below - Drive already told us the real type
                # in item["mime_type"] (that's what the frontend used to pick the
                # <iframe> rendering path in the first place), so trust that first.
                content_type = mimetypes.guess_type(target)[0] or item.get("mime_type")
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
                self._send({"error": str(exc)}, status=500)
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
            self.state.note("PUT {} failed: {}".format(self.path, exc))
            try:
                self._send({"error": str(exc) or "request failed"}, status=500)
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
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            self._send({"error": "empty upload"}, status=400)
            return
        blob = self.rfile.read(length)

        suffix = os.path.splitext(name)[1] or ".txt"
        tmp = os.path.join(tempfile.gettempdir(),
                           "intuition_schedule_upload" + suffix)
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
            result = schedule_mod.parse_file(tmp)
        except schedule_mod.ScheduleError as e:
            self._send({"error": str(e)}, status=400)
            return
        except Exception as e:  # noqa: BLE001 - report any parse failure to the UI
            self._send({"error": "Could not read {}: {}".format(name, e)}, status=400)
            return
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

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
            self.state.note("POST {} failed: {}".format(self.path, exc))
            try:
                self._send({"error": str(exc) or "request failed"}, status=500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _do_POST(self):
        path = urlparse(self.path).path
        state = self.state

        try:
            payload = self._body()
        except ValueError:
            self._send({"error": "bad json"}, status=400)
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
            with state.lock:
                state.token = token
                # Every one of these describes the *previous* session. Left latched,
                # a 401 from an expired token keeps the UI reporting that the link
                # was rejected however many good tokens are pasted after it.
                state.unified_sync_error = ""
                state.announcement_errors = []
                state.inbound_error = ""
                state.scan_errors = []
            threading.Thread(target=state.refresh_identity, daemon=True).start()
            state.note("Session token accepted")
            self._send({"ok": True})
            return

        if path == "/api/settings":
            with state.lock:
                busy = state.scanning or state.downloading or state.pushing
                if "prefer_rest" in payload:
                    state.prefer_rest = bool(payload["prefer_rest"])
            if payload.get("download_root"):
                # Moving the root mid-run would leave the ledger and the in-flight
                # plan describing two different folders.
                if busy:
                    self._send(
                        {"error": "cannot change the sync folder while a scan, "
                                  "download or push is running"}, status=409)
                    return
                state.rebind_root(payload["download_root"])
            self._send({"ok": True})
            return

        if path == "/api/lab/file":
            action = payload.get("action")
            rel_path = str(payload.get("path") or "")
            updated_content = None
            try:
                if action == "create":
                    state.lab_workspace.create(rel_path, str(payload.get("kind") or "file"), str(payload.get("inputKind") or "none"))
                elif action == "write":
                    state.lab_workspace.write(rel_path, payload.get("content", ""))
                elif action == "scaffold":
                    updated_content = state.lab_workspace.apply_input_kind(
                        rel_path, str(payload.get("inputKind") or "none"))
                elif action == "delete":
                    state.lab_workspace.delete(rel_path)
                elif action == "rename":
                    state.lab_workspace.rename(rel_path, str(payload.get("newPath") or ""))
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            response = {
                "ok": True,
                "tree": state.lab_workspace.tree(),
                "inputKinds": state.lab_workspace.input_kinds(),
            }
            if updated_content is not None:
                response["content"] = updated_content
            self._send(response)
            return

        if path == "/api/lab/run":
            rel_path = str(payload.get("path") or "")
            language = str(payload.get("language") or "")
            input_kind = str(payload.get("inputKind") or "none")
            try:
                state.lab_workspace.set_input_kind(rel_path, input_kind)
                job = state.lab_jobs.start(state.lab_workspace, rel_path, language, input_kind)
            except lab_mod.WorkspaceError as exc:
                self._send({"error": str(exc)}, status=400)
                return
            self._send({"ok": True, "job": job.id, "inputKind": job.input_kind,
                        "inputPreview": job.input_preview, "sourceHash": job.source_hash})
            return

        if path == "/api/lab/kill":
            ok = state.lab_jobs.kill(str(payload.get("job") or ""))
            self._send({"ok": ok})
            return

        if path == "/api/lab/analyze":
            rel_path = str(payload.get("path") or "")
            try:
                content = state.lab_workspace.read(rel_path)
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
            prompt = "Student profile: {}".format(state.profile.summary())
            codes = course_codes(state)
            if codes:
                prompt += "\nCourses this semester: {}".format(", ".join(codes))
            # Typed fresh for this run when given, so it takes effect immediately -
            # otherwise the profile's saved value, which the debounced autosave in
            # rpKeywords may not have persisted yet if the student typed and pressed
            # "Suggest topics" in the same breath.
            keywords = str(payload.get("keywords") or "").strip()[:200] or state.profile.get()["keywords"]
            if keywords:
                prompt += "\nInterests / keywords to anchor ideas in: {}".format(keywords)
            prompt += "\n---\nReport the JSON array of proposed ideas now."
            # The "scholar" tier never ladders or falls back (see ai_provider.TIERS),
            # so one off-format or degenerate reply would otherwise surface as "no
            # topics found" after the student's single press. Unlike the deep draft
            # pass this call is short and has no side effects until a suggestion is
            # picked, so retrying it here a couple of times is cheap and safe.
            suggestions, result, last_error = [], None, None
            for _attempt in range(RESEARCH_SUGGEST_ATTEMPTS):
                try:
                    result = ai_provider.complete_tier(
                        "scholar", prompt, RESEARCH_SUGGEST_SYSTEM,
                        preferred=state.research_backend, max_tokens=3000,
                        download_root=state.download_root)
                except ai_provider.ProviderError as exc:
                    last_error = exc
                    result = None
                    continue
                suggestions = ureca_mod.parse_suggest_response(result.get("text") or "")
                if suggestions:
                    break
            if result is None:
                self._send({"error": str(last_error)}, status=502)
                return
            self._send({"suggestions": suggestions,
                        "backend": result.get("backend"),
                        "model": result.get("model")})
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
                    "scholar", prompt, URECA_DRAFT_SYSTEM, preferred=state.research_backend,
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
            if action == "read":
                if not state.announcements.mark_read(str(payload.get("id") or ""),
                                                     bool(payload.get("read", True))):
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

        if path == "/api/drive/learn":
            item_id = str(payload.get("id") or "")
            session_id = str(payload.get("session") or "default")[:80]
            question = str(payload.get("question") or "").strip()[:2000]
            snapshot = str(payload.get("snapshot") or "")
            if snapshot and (not snapshot.startswith("data:image/jpeg;base64,")
                             or len(snapshot) > 3_000_000):
                self._send({"error": "snapshot must be a JPEG under 2 MB"}, status=400)
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
                history = state.chat_memory.recent(session_id, item_id, limit=8)
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
                            max_tokens=900, timeout=180)
                    except omniroute_provider.OmniRouteError as vision_exc:
                        state.note("FRIDAY vision unavailable: {}".format(vision_exc))
                        snapshot_note = "Snapshot vision is temporarily unavailable. FRIDAY answered from the document text instead."
                        result = ai_provider.complete_tier(
                            "chat",
                            prompt + "\n\nThe selected snapshot could not be decoded by the vision provider. Answer from the extracted material and state briefly if the requested visual detail cannot be verified.",
                            DRIVE_LEARNING_SYSTEM, preferred=state.research_backend,
                            max_tokens=900, download_root=state.download_root)
                else:
                    result = ai_provider.complete_tier(
                        "chat", prompt, DRIVE_LEARNING_SYSTEM, preferred=state.research_backend,
                        max_tokens=900, download_root=state.download_root)
                answer = result.get("text") or ""
                state.chat_memory.add(session_id, item_id, "user", question)
                state.chat_memory.add(session_id, item_id, "assistant", answer,
                                      result.get("backend"), result.get("model"))
                self._send({"answer": answer, "backend": result.get("backend"),
                            "model": result.get("model"), "memory": "local",
                            "snapshot_note": snapshot_note})
            except Exception as exc:  # noqa: BLE001 - surface material/provider failures
                self._send({"error": str(exc)}, status=500)
            return

        if not state.token:
            self._send({"error": "no session token"}, status=401)
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
            try:
                courses = get_courses(
                    state.token, prefer_rest=state.prefer_rest,
                    scope=state.scope, download_root=state.download_root,
                )
            except Exception as e:  # noqa: BLE001
                self._send({"error": str(e)}, status=500)
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
            threading.Thread(
                target=do_scan,
                args=(state, payload.get("course_ids", [])),
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
            threading.Thread(
                target=do_download, args=(state, payload.get("paths", [])), daemon=True
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
    threading.Thread(target=Handler.state.refresh_identity, daemon=True).start()
    threading.Thread(target=announcement_sync_scheduler, args=(Handler.state,),
                     daemon=True).start()
    threading.Thread(target=inbound_poll_scheduler, args=(Handler.state,),
                     daemon=True).start()
    threading.Thread(target=sweep_old_summaries,
                     args=(Handler.state, summary_retention_days), daemon=True).start()
    # Drive search should be ready without making the user understand or manually
    # build an index. Only start silently when an existing OAuth token can be reused.
    if drive.credentials_present() and drive.token_present():
        Handler.state.drive_listing = True
        threading.Thread(target=do_drive_list, args=(Handler.state,), daemon=True).start()
    omniroute_stop = threading.Event()
    if omniroute_provider.managed_locally():
        threading.Thread(target=omniroute_watchdog,
                         args=(Handler.state, omniroute_stop), daemon=True).start()
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
        omniroute_stop.set()
        server.shutdown()


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
