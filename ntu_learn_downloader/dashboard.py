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
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlparse

from ntu_learn_downloader import auth
from ntu_learn_downloader.api import (
    get_courses,
    is_excluded,
    get_download_dir,
    get_file_download_link,
    get_recorded_lecture_download_link,
)
from ntu_learn_downloader import drive
from ntu_learn_downloader import transcribe as transcribe_mod
from ntu_learn_downloader.contentcache import ContentCache
from ntu_learn_downloader.ledger import Ledger
from ntu_learn_downloader.sync import DOWNLOADABLE, PUSHABLE, build_plan, summarize
from ntu_learn_downloader.utils import download, get_filename_from_url, sanitise_filename

DEFAULT_PORT = 8384
DEFAULT_HOST = "127.0.0.1"


class State:
    """Everything the UI needs, guarded by a lock since downloads run in a thread."""

    def __init__(self, download_root: str, prefer_rest: bool = True,
                 drive_folder: str = drive.DEFAULT_ROOT_FOLDER, move: bool = True,
                 favorites_only: bool = True, exclude=None,
                 transcribe_model: str = transcribe_mod.DEFAULT_MODEL):
        self.lock = threading.Lock()
        self.download_root = download_root
        self.prefer_rest = prefer_rest
        self.favorites_only = favorites_only
        self.exclude = list(exclude or [])
        self.transcribe_model = transcribe_model
        self.transcribing = False
        self.transcribe_progress = {"done": 0, "total": 0, "current": "", "pct": 0}
        self._transcriber = None
        self.media_survey: List[Dict] = []
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
                "favorites_only": self.favorites_only,
                "exclude": self.exclude,
                "has_token": bool(self.token),
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


def do_push(state: State):
    """Mirror every locally-held plan entry into Drive, then reclaim the disk."""
    try:
        with state.lock:
            targets = [
                e for e in state.plan
                if e["status"] in PUSHABLE and os.path.isfile(e["path"])
                and not is_excluded(e.get("course", ""), state.exclude)
                and not is_excluded(e.get("rel_path", ""), state.exclude)
            ]
            blocked = [
                e for e in state.plan
                if e["status"] in PUSHABLE and os.path.isfile(e["path"])
                and (is_excluded(e.get("course", ""), state.exclude)
                     or is_excluded(e.get("rel_path", ""), state.exclude))
            ]
            state.push_progress = {
                "done": 0, "total": len(targets), "current": "", "pct": 0
            }

        if blocked:
            state.note("Excluded from Drive ({}): {} file(s) held back".format(
                ", ".join(state.exclude), len(blocked)))
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

        # Pushing operates purely on files already on disk, so it must work even when
        # the NTULearn session has expired. Handle it before the session gate below.
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
                    favorites_only=state.favorites_only,
                    exclude=state.exclude,
                )
            except Exception as e:  # noqa: BLE001
                self._send({"error": str(e)}, status=500)
                return
            with state.lock:
                state.courses = [{"name": n, "id": i} for n, i in courses]
            state.note("Found {} {}".format(
                len(courses),
                "favourite course(s)" if state.favorites_only else "course(s)"))
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
          move: bool = True, favorites_only: bool = True, exclude=None,
          transcribe_model: str = transcribe_mod.DEFAULT_MODEL):
    Handler.state = State(
        os.path.abspath(download_root), prefer_rest=prefer_rest,
        drive_folder=drive_folder, move=move, favorites_only=favorites_only,
        exclude=exclude, transcribe_model=transcribe_model,
    )
    # Populate the transcript survey up front so the UI is accurate before any scan.
    Handler.state.refresh_media()
    server = ThreadingHTTPServer((DEFAULT_HOST, port), Handler)
    url = "http://{}:{}/".format(DEFAULT_HOST, port)
    print("iNTUition: {}".format(url))
    print("Staging to:  {}".format(Handler.state.download_root))
    print("Drive root:  {}/  ({})".format(
        drive_folder, "move" if move else "copy"))
    print("Scope:       {}".format(
        "Favourites only" if favorites_only else "all enrolments"))
    if exclude:
        print("Excluded:    {}".format(", ".join(exclude)))
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
        "--all_courses",
        action="store_true",
        help="Use every enrolment. By default only Favourites are read.",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="Comma separated course-name substrings to keep out of the pipeline "
             "entirely, e.g. --exclude ML0004",
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
        favorites_only=not args.all_courses,
        exclude=[x for x in args.exclude.split(',') if x.strip()],
        transcribe_model=args.transcribe_model,
    )


if __name__ == "__main__":
    main()
