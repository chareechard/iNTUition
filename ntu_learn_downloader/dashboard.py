"""iNTUition - local sync dashboard for NTULearn.

Run with::

    python -m ntu_learn_downloader.dashboard --download_to NTU

It serves a single-page HUD on http://127.0.0.1:8384 that lets you paste a session
token, scan your courses, see exactly what is new or changed on Blackboard versus your
local folder, and download only what you pick.

Deliberately built on the standard library so the tool keeps its three runtime
dependencies. It binds to loopback only - the session token never leaves the machine.
"""
import argparse
import json
import os
import tempfile
from datetime import datetime
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ntu_learn_downloader import auth
from ntu_learn_downloader import api as api_mod
from ntu_learn_downloader import semester as semester_mod
from ntu_learn_downloader.api import (
    get_courses,
    get_download_dir,
    get_file_download_link,
    get_recorded_lecture_download_link,
)
from ntu_learn_downloader import drive
from ntu_learn_downloader import transcribe as transcribe_mod
from ntu_learn_downloader.contentcache import ContentCache
from ntu_learn_downloader.ledger import Ledger
from ntu_learn_downloader import rnd as rnd_mod
from ntu_learn_downloader import research as research_mod
from ntu_learn_downloader import materials as materials_mod
from ntu_learn_downloader import inbound as inbound_mod
from ntu_learn_downloader import academic_calendar as cal_mod
from ntu_learn_downloader import schedule as schedule_mod
from ntu_learn_downloader.sync import DOWNLOADABLE, PUSHABLE, build_plan, summarize
from ntu_learn_downloader.utils import download, get_filename_from_url, sanitise_filename

DEFAULT_PORT = 8384
DEFAULT_HOST = "127.0.0.1"


class State:
    """Everything the UI needs, guarded by a lock since downloads run in a thread."""

    def __init__(self, download_root: str, prefer_rest: bool = True,
                 drive_folder: str = drive.DEFAULT_ROOT_FOLDER, move: bool = True,
                 scope: str = api_mod.SCOPE_SEMESTER,
                 transcribe_model: str = transcribe_mod.DEFAULT_MODEL,
                 cerberus_db: Optional[str] = None):
        self.lock = threading.Lock()
        self.download_root = download_root
        self.prefer_rest = prefer_rest
        self.scope = scope
        self.transcribe_model = transcribe_model
        self.transcribing = False
        self.transcribe_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self._transcriber = None
        self.media_survey: List[Dict] = []
        self.identity: Optional[Dict] = None
        self.drive_folder = drive_folder
        self.move = move
        self.token: Optional[str] = auth.load_token()
        self.courses: List[Dict] = []
        self.plan: List[Dict] = []
        self.skipped: List[str] = []
        self.scanning = False
        self.downloading = False
        self.pushing = False
        self.log: List[str] = []
        self.progress = {"done": 0, "total": 0, "current": "", "bytes": 0}
        self.push_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self.ledger = Ledger(download_root)
        self.cache = ContentCache(download_root)
        self.schedule = schedule_mod.Schedule(download_root)
        self.rnd = rnd_mod.Board(download_root)
        # Ids currently out at the Claude backend, and the last error per id. Research
        # is per-entry and on demand, so several can be in flight at once.
        self.researching: set = set()
        self.research_errors: Dict[str, str] = {}
        # None = pick whatever is usable, preferring the CLI's own login over a key.
        self.research_backend: Optional[str] = None
        # The one setting that sends course content off the machine. On by request;
        # the panel toggles it and every finding records what was actually shared.
        self.research_materials = True
        # An explicit flag store to read. When unset, the reader prefers iNTUition's
        # own triage.db and falls back to a sibling Cerberus checkout.
        self.cerberus_db: Optional[str] = cerberus_db
        # Which academic calendar to resolve teaching weeks against.
        self.semester_key = semester_mod.format_semester(
            semester_mod.current_semester())

    def _schedule_snapshot(self) -> Dict:
        """Everything the Temporal Protocol panel needs, resolved to today."""
        now = datetime.now()
        sem = self.semester_key
        teaching_week = cal_mod.week_of(now.date(), sem)
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
            "week": self.schedule.week(teaching_week),
            "all_week": self.schedule.week(),
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
            from ntu_learn_downloader import rest
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
            return {
                "download_root": self.download_root,
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
                "downloading": self.downloading,
                "pushing": self.pushing,
                "transcribing": self.transcribing,
                "transcribe_progress": dict(self.transcribe_progress),
                "transcribe": {
                    "model": self.transcribe_model,
                    "media": self.media_survey,
                },
                "progress": dict(self.progress),
                "push_progress": dict(self.push_progress),
                "drive": {
                    "folder": self.drive_folder,
                    "move": self.move,
                    "configured": drive.credentials_present(),
                    "linked": drive.token_present(),
                    "archived": len(self.ledger),
                },
                "schedule": self._schedule_snapshot(),
                "inbound": inbound_mod.snapshot(
                    inbound_mod.resolve_path(self.download_root, self.cerberus_db)),
                "rnd": self.rnd.snapshot(),
                "research": dict(
                    research_mod.status(self.research_backend),
                    busy=sorted(self.researching),
                    errors=dict(self.research_errors),
                    materials=self.research_materials,
                    material_caps={"files": materials_mod.MAX_FILES,
                                   "mb": materials_mod.MAX_TOTAL_BYTES // 1048576},
                    sandbox=os.path.join(self.download_root,
                                         research_mod.STORAGE_DIR,
                                         research_mod.SANDBOX_DIRNAME),
                ),
                "log": list(self.log[-40:]),
            }


def do_scan(state: State, course_ids: List[str]):
    """Fetch content trees for the chosen courses and diff them against disk."""
    try:
        state.cache.reset_stats()
        selected = [c for c in state.courses if c["id"] in course_ids]
        combined: List[Dict] = []
        skipped: List[str] = []

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
                state.note("  failed: {}".format(e))
                continue

            entries = build_plan(tree, state.download_root, ledger=state.ledger)
            for entry in entries:
                entry["course"] = course["name"]
            combined.extend(entries)

        with state.lock:
            state.plan = combined
            state.skipped = skipped
        state.cache.save()
        state.refresh_media()
        counts = summarize(combined)
        st = state.cache.stats()
        state.note(
            "Scan complete: {} new, {} updated, {} on disk, {} archived "
            "({} cached, {} refetched)".format(
                counts["new"], counts["updated"], counts["current"],
                counts["archived"], st["hits"], st["misses"]
            )
        )
    finally:
        with state.lock:
            state.scanning = False


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
                        os.path.dirname(entry["path"]), sanitise_filename(filename)
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


def course_codes(state: State) -> List[str]:
    """Course codes from the scanned course list, e.g. 26S1-SC2002-... -> SC2002."""
    import re
    codes = []
    with state.lock:
        names = [c.get("name", "") for c in state.courses]
    for name in names:
        codes += re.findall(r"[A-Z]{2,4}\d{4}", name.upper())
    return sorted(set(codes))


def do_research(state: State, item_id: str):
    """Send one board entry to Claude and store the finding on it.

    Only ever reached from an explicit press of Research on that entry. What is sent is
    built in research.build_prompt - the entry text plus the enrolled course codes, and
    nothing from the download folder.
    """
    item = state.rnd.get(item_id)
    title = (item or {}).get("title", item_id)
    try:
        if item is None:
            raise research_mod.ResearchError("That entry no longer exists")
        # Course material is shared only with sharing on, only for this entry's own
        # course tag, and only for the length of the run.
        specs, service = [], None
        if state.research_materials and item.get("course"):
            specs = materials_mod.select(item["course"], state.download_root,
                                         ledger=state.ledger)
            if any(s["local"] is None for s in specs):
                service = materials_mod.service_for(state.download_root)
                if service is None:
                    specs = [s for s in specs if s["local"]]
                    state.note("Drive not linked; sharing only local material")
            if specs:
                state.note("Sharing {} {} file(s) with this run".format(
                    len(specs), item["course"]))

        state.note("Researching {}".format(title))
        finding = research_mod.research(item, courses=course_codes(state),
                                        backend=state.research_backend,
                                        download_root=state.download_root,
                                        material_specs=specs, drive_service=service)
        state.rnd.set_research(item_id, finding)
        state.rnd.save()
        with state.lock:
            state.research_errors.pop(item_id, None)
        cost = finding.get("cost_usd")
        shared = finding.get("materials") or []
        state.note("Research done: {} via {} ({} source(s){}{})".format(
            title, finding.get("backend", "?"), len(finding["sources"]),
            ", {} material file(s)".format(len(shared)) if shared else "",
            ", ${:.4f}".format(cost) if cost else ""))
    except research_mod.ResearchError as e:
        with state.lock:
            state.research_errors[item_id] = str(e)
        state.note("Research failed for {}: {}".format(title, e))
    except Exception as e:  # noqa: BLE001 - a thread dying silently is worse
        with state.lock:
            state.research_errors[item_id] = str(e)
        state.note("Research failed for {}: {}".format(title, e))
    finally:
        with state.lock:
            state.researching.discard(item_id)


def do_push(state: State):
    """Mirror every locally-held plan entry into Drive, then reclaim the disk."""
    try:
        with state.lock:
            targets = [
                e for e in state.plan
                if e["status"] in PUSHABLE and os.path.isfile(e["path"])
            ]
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
    finally:
        with state.lock:
            state.pushing = False
            state.push_progress["current"] = ""


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
        path = urlparse(self.path).path
        if path == "/":
            body = load_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send(self.state.snapshot())
            return
        self._send({"error": "not found"}, status=404)

    def do_PUT(self):
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
        path = urlparse(self.path).path
        state = self.state

        try:
            payload = self._body()
        except ValueError:
            self._send({"error": "bad json"}, status=400)
            return

        if path == "/api/token":
            try:
                token = auth.resolve(payload.get("token", ""))
            except auth.AuthenticationError as e:
                self._send({"error": str(e)}, status=400)
                return
            with state.lock:
                state.token = token
            threading.Thread(target=state.refresh_identity, daemon=True).start()
            state.note("Session token accepted")
            self._send({"ok": True})
            return

        if path == "/api/settings":
            with state.lock:
                if payload.get("download_root"):
                    state.download_root = payload["download_root"]
                if "prefer_rest" in payload:
                    state.prefer_rest = bool(payload["prefer_rest"])
            self._send({"ok": True})
            return

        if path == "/api/rnd":
            action = payload.get("action")
            try:
                if action == "add":
                    state.rnd.add(payload.get("title", ""),
                                  course=payload.get("course", ""),
                                  notes=payload.get("notes", ""),
                                  link=payload.get("link", ""))
                elif action == "advance":
                    state.rnd.advance(payload.get("id", ""))
                elif action == "update":
                    state.rnd.update(payload.get("id", ""),
                                     **{k: payload.get(k) for k in
                                        ("title", "course", "notes", "link", "status")})
                elif action == "remove":
                    state.rnd.remove(payload.get("id", ""))
                elif action == "research":
                    item_id = payload.get("id", "")
                    if state.rnd.get(item_id) is None:
                        self._send({"error": "no such entry"}, status=404)
                        return
                    if research_mod.resolve_backend(state.research_backend) is None:
                        self._send({"error": "No research backend. Install the Claude "
                                             "CLI (uses your existing login), or set "
                                             "an Anthropic API key."}, status=400)
                        return
                    with state.lock:
                        if item_id in state.researching:
                            self._send({"error": "already researching"}, status=409)
                            return
                        state.researching.add(item_id)
                        state.research_errors.pop(item_id, None)
                    threading.Thread(target=do_research, args=(state, item_id),
                                     daemon=True).start()
                    self._send({"ok": True, "researching": item_id})
                    return
                elif action == "materials":
                    with state.lock:
                        state.research_materials = bool(payload.get("on"))
                    state.note("Course materials sharing {}".format(
                        "on" if state.research_materials else "off"))
                elif action == "clear_research":
                    state.rnd.set_research(payload.get("id", ""), None)
                else:
                    self._send({"error": "unknown action"}, status=400)
                    return
            except ValueError as e:
                self._send({"error": str(e)}, status=400)
                return
            state.rnd.save()
            self._send({"ok": True, "count": len(state.rnd)})
            return

        # Pushing operates purely on files already on disk, so it must work even when
        # the NTULearn session has expired. Handle it before the session gate below.
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

        if not state.token:
            self._send({"error": "no session token"}, status=401)
            return

        if path == "/api/courses":
            try:
                courses = get_courses(
                    state.token, prefer_rest=state.prefer_rest,
                    scope=state.scope,
                )
            except Exception as e:  # noqa: BLE001
                self._send({"error": str(e)}, status=500)
                return
            with state.lock:
                state.courses = [{"name": n, "id": i} for n, i in courses]
            state.note("Found {} {}".format(
                len(courses),
                "course(s) for " + semester_mod.format_semester(
                    semester_mod.current_semester())
                if state.scope == api_mod.SCOPE_SEMESTER else "course(s)"))
            self._send({"ok": True})
            return

        if path == "/api/scan":
            with state.lock:
                if state.scanning:
                    self._send({"error": "already scanning"}, status=409)
                    return
                state.scanning = True
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


def load_page() -> str:
    """Read the HUD page off disk.

    Kept as a separate file rather than a giant string literal so the markup, CSS and
    JS stay editable with normal tooling. Read per request (it is a few KB) so design
    tweaks show up on refresh without restarting the server.
    """
    with open(PAGE_PATH, encoding="utf-8") as f:
        return f.read()


def serve(download_root: str, port: int = DEFAULT_PORT, prefer_rest: bool = True,
          open_browser: bool = True, drive_folder: str = drive.DEFAULT_ROOT_FOLDER,
          move: bool = True, scope: str = api_mod.SCOPE_SEMESTER,
          transcribe_model: str = transcribe_mod.DEFAULT_MODEL,
          cerberus_db: Optional[str] = None):
    Handler.state = State(
        os.path.abspath(download_root), prefer_rest=prefer_rest,
        drive_folder=drive_folder, move=move, scope=scope,
        transcribe_model=transcribe_model,
    )
    Handler.state.cerberus_db = cerberus_db
    # Populate the transcript survey up front so the UI is accurate before any scan.
    Handler.state.refresh_media()
    threading.Thread(target=Handler.state.refresh_identity, daemon=True).start()
    server = ThreadingHTTPServer((DEFAULT_HOST, port), Handler)
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
              "python -m ntu_learn_downloader.drive_push --setup")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="iNTUition - local sync dashboard for NTULearn"
    )
    parser.add_argument(
        "--download_to", default="NTU", help="Local sync folder (default: NTU)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--cerberus_db",
        help="Path to Cerberus's flagged.db, to show flagged NTU mail. Defaults to "
             "a sibling J.A.R.V.I.S checkout; the panel hides itself if absent.")
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
        cerberus_db=args.cerberus_db,
    )


if __name__ == "__main__":
    main()
