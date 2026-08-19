"""Local file workspace and process execution for the in-browser Lab IDE.

Files live under <download_root>/.intuition/lab/workspace/. Running a file
shells out to a real local python3/javac/java on PATH - the same trust
boundary as running it from a terminal, not a sandboxed runtime. The one
safety net beyond the user's own Stop button is a hard wall-clock timeout,
so a runaway script can't pin a background thread forever.
"""
import os
import shutil
import subprocess
import sys
import threading
import uuid
from shutil import which
from typing import Dict, List, Optional

STORAGE_DIR = os.path.join(".intuition", "lab")
WORKSPACE_SUBDIR = "workspace"
RUN_TIMEOUT_SECONDS = 30
OUTPUT_LINE_LIMIT = 4000  # per job - bounds memory for a runaway print loop

PYTHON_CANDIDATES = ("python3", "python")
SUPPORTED_LANGUAGES = ("python", "java")


class WorkspaceError(ValueError):
    pass


def _popen_kwargs() -> Dict:
    # No console window flashing up behind the dashboard on Windows - the
    # same flag claude_bridge.py already uses for its own subprocess calls.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class Workspace:
    """CRUD over one directory tree, confined to it - never escapes the root."""

    def __init__(self, download_root: str):
        self.root = os.path.join(os.path.abspath(download_root), STORAGE_DIR, WORKSPACE_SUBDIR)
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, rel_path: str) -> str:
        rel_path = (rel_path or "").strip().lstrip("/\\")
        if not rel_path:
            raise WorkspaceError("path is required")
        candidate = os.path.realpath(os.path.join(self.root, rel_path.replace("/", os.sep)))
        root = os.path.realpath(self.root)
        try:
            if os.path.commonpath((root, candidate)) != root:
                raise WorkspaceError("path escapes the workspace")
        except ValueError:
            raise WorkspaceError("path escapes the workspace")
        return candidate

    def abs_path(self, rel_path: str) -> str:
        return self._resolve(rel_path)

    def tree(self) -> List[Dict]:
        def walk(dir_path: str, rel: str) -> List[Dict]:
            try:
                names = sorted(os.listdir(dir_path))
            except OSError:
                return []
            entries = []
            for name in names:
                if name.startswith("."):
                    continue
                full = os.path.join(dir_path, name)
                rel_child = "{}/{}".format(rel, name) if rel else name
                if os.path.isdir(full):
                    entries.append({"path": rel_child, "name": name, "type": "dir",
                                     "children": walk(full, rel_child)})
                else:
                    entries.append({"path": rel_child, "name": name, "type": "file"})
            return entries
        return walk(self.root, "")

    def read(self, rel_path: str) -> str:
        path = self._resolve(rel_path)
        if not os.path.isfile(path):
            raise WorkspaceError("no such file")
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def write(self, rel_path: str, content: str) -> None:
        path = self._resolve(rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content or "")

    def create(self, rel_path: str, kind: str) -> None:
        path = self._resolve(rel_path)
        if os.path.exists(path):
            raise WorkspaceError("already exists")
        if kind == "dir":
            os.makedirs(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "a", encoding="utf-8").close()

    def delete(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)
        else:
            raise WorkspaceError("no such path")

    def rename(self, rel_path: str, new_rel_path: str) -> None:
        src = self._resolve(rel_path)
        dst = self._resolve(new_rel_path)
        if not os.path.exists(src):
            raise WorkspaceError("no such path")
        if os.path.exists(dst):
            raise WorkspaceError("destination already exists")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)


class Job:
    """One run's live state: buffered output lines plus a monotonic seq
    so the frontend can poll "give me everything after N" cheaply."""

    def __init__(self, job_id: str, language: str):
        self.id = job_id
        self.language = language
        self.status = "running"  # running | exited | killed | error
        self.exit_code: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self._lines: List[Dict] = []
        self._next_seq = 0
        self._lock = threading.Lock()

    def append(self, stream: str, text: str) -> None:
        with self._lock:
            self._lines.append({"seq": self._next_seq, "stream": stream, "text": text})
            self._next_seq += 1
            if len(self._lines) > OUTPUT_LINE_LIMIT:
                self._lines = self._lines[-OUTPUT_LINE_LIMIT:]

    def since(self, seq: int) -> Dict:
        with self._lock:
            lines = [line for line in self._lines if line["seq"] >= seq]
            next_seq = self._next_seq
        return {"lines": lines, "seq": next_seq, "status": self.status,
                "exitCode": self.exit_code}


class JobManager:
    """One active job at a time per workspace - matches the spec's single
    Run/Stop/Clear lifecycle rather than a multi-job queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._active_id: Optional[str] = None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, workspace: Workspace, rel_path: str, language: str) -> Job:
        if language not in SUPPORTED_LANGUAGES:
            raise WorkspaceError("unsupported language")
        with self._lock:
            active = self._jobs.get(self._active_id) if self._active_id else None
            if active and active.status == "running":
                raise WorkspaceError("a job is already running")
        abs_path = workspace.abs_path(rel_path)
        if not os.path.isfile(abs_path):
            raise WorkspaceError("no such file")
        job = Job(uuid.uuid4().hex[:12], language)
        with self._lock:
            self._jobs[job.id] = job
            self._active_id = job.id
        threading.Thread(target=self._run, args=(job, abs_path), daemon=True).start()
        return job

    def kill(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running":
            return False
        if job.process is not None:
            try:
                job.process.kill()
            except OSError:
                pass
        job.status = "killed"
        return True

    def output_since(self, job_id: str, seq: int) -> Optional[Dict]:
        job = self.get(job_id)
        return job.since(seq) if job else None

    # ── execution ──────────────────────────────────────────────────────
    def _run(self, job: Job, abs_path: str) -> None:
        try:
            if job.language == "python":
                self._run_python(job, abs_path)
            else:
                self._run_java(job, abs_path)
        except Exception as exc:  # noqa: BLE001 - report, never crash the worker thread
            job.append("stderr", str(exc))
            job.status = "error"

    def _stream_to_completion(self, job: Job, process: subprocess.Popen) -> None:
        # Set before starting the pumps so a Stop click landing in the first
        # instant after Popen() still has a live process handle to kill.
        job.process = process

        def pump(stream, name):
            for line in iter(stream.readline, ""):
                job.append(name, line.rstrip("\n"))
            stream.close()
        t_out = threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()
        try:
            exit_code = process.wait(timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait()
            job.append("stderr", "[Killed: exceeded {}s time limit]".format(RUN_TIMEOUT_SECONDS))
            job.status = "killed"
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        job.exit_code = exit_code
        if job.status == "running":
            job.status = "exited"

    def _run_python(self, job: Job, abs_path: str) -> None:
        python = next((c for c in PYTHON_CANDIDATES if which(c)), None)
        if not python:
            job.append("stderr", "No local Python interpreter (python3/python) found on PATH.")
            job.status = "error"
            return
        process = subprocess.Popen(
            [python, "-u", os.path.basename(abs_path)],
            cwd=os.path.dirname(abs_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, **_popen_kwargs())
        self._stream_to_completion(job, process)

    def _run_java(self, job: Job, abs_path: str) -> None:
        if not which("javac") or not which("java"):
            job.append("stderr", "No local JDK (javac/java) found on PATH.")
            job.status = "error"
            return
        directory = os.path.dirname(abs_path)
        filename = os.path.basename(abs_path)
        class_name = os.path.splitext(filename)[0]
        try:
            compiled = subprocess.run(
                ["javac", filename], cwd=directory, capture_output=True, text=True,
                timeout=RUN_TIMEOUT_SECONDS, **_popen_kwargs())
        except subprocess.TimeoutExpired:
            job.append("stderr", "[Killed: compile exceeded {}s time limit]".format(RUN_TIMEOUT_SECONDS))
            job.status = "killed"
            return
        for line in (compiled.stdout or "").splitlines():
            job.append("stdout", line)
        if compiled.returncode != 0:
            for line in (compiled.stderr or "").splitlines():
                job.append("stderr", line)
            job.status = "error"
            job.exit_code = compiled.returncode
            return
        process = subprocess.Popen(
            ["java", "-cp", directory, class_name],
            cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, **_popen_kwargs())
        self._stream_to_completion(job, process)
