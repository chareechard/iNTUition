"""Local file workspace and process execution for the in-browser Lab IDE.

Files live under <download_root>/.intuition/lab/workspace/. Running a file
shells out to a real local python3/javac/java on PATH - the same trust
boundary as running it from a terminal, not a sandboxed runtime. The one
safety net beyond the user's own Stop button is a hard wall-clock timeout,
so a runaway script can't pin a background thread forever.
"""
import json
import os
import random
import re
import string
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import hashlib
from shutil import which
from typing import Dict, List, Optional

STORAGE_DIR = os.path.join(".intuition", "lab")
WORKSPACE_SUBDIR = "workspace"
INPUT_METADATA_FILE = ".input-kinds.json"
RUN_TIMEOUT_SECONDS = 30
OUTPUT_LINE_LIMIT = 4000  # per job - bounds memory for a runaway print loop
RUNTIME_TRACE_PREFIX = ".lab-trace-"

PYTHON_CANDIDATES = ("python3", "python")
SUPPORTED_LANGUAGES = ("python", "java")


class WorkspaceError(ValueError):
    pass


INPUT_KIND_LABELS = {
    "none": "No generated input",
    "array": "Array of integers",
    "string": "String",
    "matrix": "Matrix of integers",
    "graph": "Weighted graph",
    "tree": "Binary tree values",
}


def normalize_input_kind(input_kind: str) -> str:
    kind = str(input_kind or "none").strip().lower()
    if kind not in INPUT_KIND_LABELS:
        raise WorkspaceError("unsupported input kind")
    return kind


def _java_class_name(rel_path: str) -> str:
    """Return a Java class name that matches a valid source file name."""
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    if (stem and (stem[0].isalpha() or stem[0] in "_$") and
            all(ch.isalnum() or ch in "_$" for ch in stem)):
        return stem
    return "Main"


def _solution_parameter_name(input_kind: str, parameter_name: Optional[str] = None) -> str:
    defaults = {
        "array": "values",
        "string": "value",
        "matrix": "matrix",
        "graph": "graph",
        "tree": "values",
    }
    if parameter_name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parameter_name):
        return parameter_name
    return defaults[input_kind]


def _python_solve_parts(content: str) -> Optional[Dict[str, str]]:
    match = re.search(r"(?m)^[ \t]*def solve\((?P<params>[^)]*)\):[ \t]*$", content)
    if not match:
        return None
    line_end = content.find("\n", match.end())
    body_start = len(content) if line_end < 0 else line_end + 1
    next_top_level = re.search(r"(?m)^(?![ \t])\S", content[body_start:])
    body_end = (body_start + next_top_level.start()
                if next_top_level else len(content))
    params = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group("params"))
    return {
        "prefix": content[:match.start()],
        "signature": match.group(0),
        "body": content[body_start:body_end].rstrip(),
        "runner": content[body_end:],
        "parameter": params[-1] if params else "",
    }


def _java_matching_brace(content: str, opening: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char in ('"', "'"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _java_solve_parts(content: str) -> Optional[Dict[str, object]]:
    match = re.search(
        r"(?m)^[ \t]*(?:(?:public|private|protected)\s+)?static\s+void\s+solve\s*"
        r"\([^)]*\)\s*\{",
        content,
    )
    if not match:
        return None
    opening = content.find("{", match.start(), match.end())
    closing = _java_matching_brace(content, opening)
    if closing < 0:
        return None
    signature = content[match.start():opening + 1]
    params = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", signature.split("(", 1)[1])
    return {
        "start": match.start(),
        "opening": opening,
        "closing": closing,
        "signature": signature,
        "body": content[opening + 1:closing],
        "parameter": params[-1] if params else "",
    }


def _migrate_python_scaffold(rel_path: str, input_kind: str, current: str) -> Optional[str]:
    old = _python_solve_parts(current)
    if not old:
        return None
    new = scaffold_content(rel_path, input_kind, old["parameter"] or None)
    generated = _python_solve_parts(new)
    if not generated:
        return None
    body = old["body"] or "    pass"
    new_runner = str(generated["runner"]).lstrip("\n")
    return (generated["prefix"] + old["prefix"] +
            generated["signature"] + "\n" + body + "\n\n" + new_runner)


def _migrate_java_scaffold(rel_path: str, input_kind: str, current: str) -> Optional[str]:
    old = _java_solve_parts(current)
    if not old:
        return None
    new = scaffold_content(rel_path, input_kind, old["parameter"] or None)
    generated = _java_solve_parts(new)
    if not generated:
        return None
    start = int(generated["start"])
    opening = int(generated["opening"])
    closing = int(generated["closing"])
    method = (new[start:opening + 1] + str(old["body"]) +
              new[closing:closing + 1])
    return new[:start] + method + new[closing + 1:]


def migrate_scaffold(rel_path: str, input_kind: str, current: str) -> str:
    """Change only the generated input contract, preserving solve implementation."""
    kind = normalize_input_kind(input_kind)
    if not current.strip():
        return scaffold_content(rel_path, kind)
    lower = (rel_path or "").lower()
    if lower.endswith(".py"):
        migrated = _migrate_python_scaffold(rel_path, kind, current)
    elif lower.endswith(".java"):
        migrated = _migrate_java_scaffold(rel_path, kind, current)
    else:
        raise WorkspaceError("input scaffolds are available for Python and Java files")
    if migrated is None:
        raise WorkspaceError("file does not contain a solve scaffold")
    return migrated

def scaffold_content(rel_path: str, input_kind: str = "none", parameter_name: Optional[str] = None) -> str:
    """Return a runnable, input-aware scaffold for a newly created source file."""
    kind = normalize_input_kind(input_kind)
    lower = (rel_path or "").lower()
    parameter = _solution_parameter_name(kind, parameter_name) if kind != "none" else ""
    if lower.endswith(".py"):
        if kind == "none":
            return ("def solve():\n"
                    "    # Write your solution here.\n"
                    "    pass\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    solve()\n")
        if kind == "array":
            return ("import sys\n"
                    "\n"
                    "\n"
                    "def solve(" + parameter + "):\n"
                    "    # Write your solution here.\n"
                    "    return " + parameter + "\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    values = [int(token) for token in sys.stdin.read().split()]\n"
                    "    result = solve(values)\n"
                    "    if result is not None:\n"
                    "        print(result)\n")
        if kind == "string":
            return ("import sys\n"
                    "\n"
                    "\n"
                    "def solve(" + parameter + "):\n"
                    "    # Write your solution here.\n"
                    "    return " + parameter + "\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    value = sys.stdin.readline().rstrip(\"\\n\")\n"
                    "    result = solve(value)\n"
                    "    if result is not None:\n"
                    "        print(result)\n")
        if kind == "matrix":
            return ("import sys\n"
                    "\n"
                    "\n"
                    "def solve(" + parameter + "):\n"
                    "    # Write your solution here.\n"
                    "    return " + parameter + "\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    tokens = [int(token) for token in sys.stdin.read().split()]\n"
                    "    rows, cols = tokens[:2] if len(tokens) >= 2 else (0, 0)\n"
                    "    values = tokens[2:]\n"
                    "    matrix = [values[i * cols:(i + 1) * cols] for i in range(rows)]\n"
                    "    result = solve(matrix)\n"
                    "    if result is not None:\n"
                    "        print(result)\n")
        if kind == "graph":
            return ("import sys\n"
                    "\n"
                    "\n"
                    "def solve(" + parameter + "):\n"
                    "    # graph has integer nodes and (source, target, weight) edges.\n"
                    "    # Write your solution here.\n"
                    "    return " + parameter + "\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    tokens = [int(token) for token in sys.stdin.read().split()]\n"
                    "    node_count, edge_count = tokens[:2] if len(tokens) >= 2 else (0, 0)\n"
                    "    raw_edges = tokens[2:]\n"
                    "    edges = [tuple(raw_edges[i:i + 3]) for i in range(0, edge_count * 3, 3)]\n"
                    "    graph = {\"nodes\": list(range(node_count)), \"edges\": edges}\n"
                    "    result = solve(graph)\n"
                    "    if result is not None:\n"
                    "        print(result)\n")
        return ("import sys\n"
                "\n"
                "\n"
                "def solve(" + parameter + "):\n"
                "    # values is a level-order list; -1 represents an empty node.\n"
                "    # Write your solution here.\n"
                "    return " + parameter + "\n"
                "\n"
                "\n"
                "if __name__ == \"__main__\":\n"
                "    tokens = [int(token) for token in sys.stdin.read().split()]\n"
                "    count = tokens[0] if tokens else 0\n"
                "    values = tokens[1:count + 1]\n"
                "    result = solve(values)\n"
                "    if result is not None:\n"
                "        print(result)\n")
    if lower.endswith(".java"):
        class_name = _java_class_name(rel_path)
        if kind == "none":
            return ("public class " + class_name + " {\n"
                    "    public static void main(String[] args) {\n"
                    "        solve();\n"
                    "    }\n"
                    "\n"
                    "    static void solve() {\n"
                    "        // Write your solution here.\n"
                    "    }\n"
                    "}\n")
        if kind == "array":
            return ("import java.io.IOException;\n"
                    "import java.nio.charset.StandardCharsets;\n"
                    "import java.util.StringTokenizer;\n"
                    "\n"
                    "public class " + class_name + " {\n"
                    "    public static void main(String[] args) throws Exception {\n"
                    "        solve(parseArray(readAll()));\n"
                    "    }\n"
                    "\n"
                    "    static void solve(int[] " + parameter + ") {\n"
                    "        // Write your solution here.\n"
                    "    }\n"
                    "\n"
                    "    static int[] parseArray(String raw) {\n"
                    "        StringTokenizer tokens = new StringTokenizer(raw);\n"
                    "        int[] values = new int[tokens.countTokens()];\n"
                    "        for (int i = 0; i < values.length; i++) values[i] = Integer.parseInt(tokens.nextToken());\n"
                    "        return values;\n"
                    "    }\n"
                    "\n"
                    "    static String readAll() throws IOException {\n"
                    "        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);\n"
                    "    }\n"
                    "}\n")
        if kind == "string":
            return ("import java.nio.charset.StandardCharsets;\n"
                    "\n"
                    "public class " + class_name + " {\n"
                    "    public static void main(String[] args) throws Exception {\n"
                    "        solve(new String(System.in.readAllBytes(), StandardCharsets.UTF_8).trim());\n"
                    "    }\n"
                    "\n"
                    "    static void solve(String " + parameter + ") {\n"
                    "        // Write your solution here.\n"
                    "    }\n"
                    "}\n")
        if kind == "matrix":
            return ("import java.io.IOException;\n"
                    "import java.nio.charset.StandardCharsets;\n"
                    "import java.util.StringTokenizer;\n"
                    "\n"
                    "public class " + class_name + " {\n"
                    "    public static void main(String[] args) throws Exception {\n"
                    "        solve(parseMatrix(readAll()));\n"
                    "    }\n"
                    "\n"
                    "    static void solve(int[][] " + parameter + ") {\n"
                    "        // Write your solution here.\n"
                    "    }\n"
                    "\n"
                    "    static int[][] parseMatrix(String raw) {\n"
                    "        StringTokenizer tokens = new StringTokenizer(raw);\n"
                    "        if (!tokens.hasMoreTokens()) return new int[0][0];\n"
                    "        int rows = Integer.parseInt(tokens.nextToken());\n"
                    "        int cols = Integer.parseInt(tokens.nextToken());\n"
                    "        int[][] matrix = new int[rows][cols];\n"
                    "        for (int r = 0; r < rows; r++) for (int c = 0; c < cols; c++) matrix[r][c] = Integer.parseInt(tokens.nextToken());\n"
                    "        return matrix;\n"
                    "    }\n"
                    "\n"
                    "    static String readAll() throws IOException {\n"
                    "        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);\n"
                    "    }\n"
                    "}\n")
        comment = ("// Input format: first the node count and edge count, then weighted edges.\n"
                   if kind == "graph" else
                   "// Input format: first the number of level-order values, then values; -1 is empty.\n")
        return ("import java.nio.charset.StandardCharsets;\n"
                "\n"
                "public class " + class_name + " {\n"
                "    public static void main(String[] args) throws Exception {\n"
                "        solve(new String(System.in.readAllBytes(), StandardCharsets.UTF_8).trim());\n"
                "    }\n"
                "\n"
                "    static void solve(String " + parameter + ") {\n"
                "        " + comment +
                "        // Write your solution here.\n"
                "    }\n"
                "}\n")
    return ""


def generate_input(input_kind: str = "none", rng=None) -> Dict:
    """Create one bounded random stdin sample and a human-readable preview."""
    kind = normalize_input_kind(input_kind)
    rng = rng or random.Random()
    if kind == "none":
        return {"kind": kind, "text": "", "preview": "(none)"}
    if kind == "array":
        values = [rng.randint(-20, 20) for _ in range(rng.randint(6, 10))]
        return {"kind": kind, "text": " ".join(map(str, values)) + "\n",
                "preview": " ".join(map(str, values))}
    if kind == "string":
        value = "".join(rng.choice(string.ascii_lowercase) for _ in range(12))
        return {"kind": kind, "text": value + "\n", "preview": value}
    if kind == "matrix":
        rows, cols = rng.randint(2, 4), rng.randint(2, 4)
        matrix = [[rng.randint(-9, 9) for _ in range(cols)] for _ in range(rows)]
        lines = ["{} {}".format(rows, cols)] + [" ".join(map(str, row)) for row in matrix]
        return {"kind": kind, "text": "\n".join(lines) + "\n", "preview": "\\n".join(lines)}
    if kind == "graph":
        node_count = 6
        pairs = {(i, i + 1) for i in range(node_count - 1)}
        while len(pairs) < 9:
            source = rng.randrange(node_count)
            target = rng.randrange(node_count)
            if source != target:
                pairs.add((min(source, target), max(source, target)))
        edges = [(source, target, rng.randint(1, 9)) for source, target in sorted(pairs)]
        lines = ["{} {}".format(node_count, len(edges))]
        lines.extend("{} {} {}".format(*edge) for edge in edges)
        return {"kind": kind, "text": "\n".join(lines) + "\n", "preview": "\\n".join(lines)}
    values = [rng.randint(1, 99) for _ in range(7)]
    line = "{}\n{}".format(len(values), " ".join(map(str, values)))
    return {"kind": kind, "text": line + "\n", "preview": line.replace("\n", "\\n")}

def _popen_kwargs() -> Dict:
    # No console window flashing up behind the dashboard on Windows - the
    # same flag claude_bridge.py already uses for its own subprocess calls.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class Workspace:
    """CRUD over one directory tree, confined to it - never escapes the root."""

    def __init__(self, download_root: str):
        self.root = os.path.join(os.path.abspath(download_root), STORAGE_DIR, WORKSPACE_SUBDIR)
        os.makedirs(self.root, exist_ok=True)

    def _metadata_path(self) -> str:
        return os.path.join(self.root, INPUT_METADATA_FILE)

    def _read_input_kinds(self) -> Dict[str, str]:
        try:
            with open(self._metadata_path(), encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(path): kind for path, kind in raw.items()
                if isinstance(path, str) and isinstance(kind, str)
                and kind in INPUT_KIND_LABELS}

    def _write_input_kinds(self, values: Dict[str, str]) -> None:
        with open(self._metadata_path(), "w", encoding="utf-8", newline="\n") as f:
            json.dump(values, f, ensure_ascii=True, sort_keys=True, indent=2)
            f.write("\n")

    def _canonical_rel_path(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.root).replace(os.sep, "/")

    def input_kinds(self) -> Dict[str, str]:
        """Return persisted randomized-input modes keyed by visible file path."""
        return self._read_input_kinds()

    def set_input_kind(self, rel_path: str, input_kind: str) -> None:
        path = self._resolve(rel_path)
        if not os.path.isfile(path):
            raise WorkspaceError("no such file")
        values = self._read_input_kinds()
        values[self._canonical_rel_path(path)] = normalize_input_kind(input_kind)
        self._write_input_kinds(values)

    def apply_input_kind(self, rel_path: str, input_kind: str) -> str:
        """Update the input adapter while keeping the user's solve body."""
        path = self._resolve(rel_path)
        if not os.path.isfile(path):
            raise WorkspaceError("no such file")
        kind = normalize_input_kind(input_kind)
        current = self.read(rel_path)
        canonical = self._canonical_rel_path(path)
        if self._read_input_kinds().get(canonical) == kind:
            return current
        updated = migrate_scaffold(rel_path, kind, current)
        self.write(rel_path, updated)
        self.set_input_kind(rel_path, kind)
        return updated
    def _remove_input_kinds(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        canonical = self._canonical_rel_path(path)
        values = self._read_input_kinds()
        values = {key: value for key, value in values.items()
                  if key != canonical and not key.startswith(canonical + "/")}
        self._write_input_kinds(values)

    def _rename_input_kinds(self, src: str, dst: str) -> None:
        source = self._canonical_rel_path(self._resolve(src))
        target = self._canonical_rel_path(self._resolve(dst))
        values = self._read_input_kinds()
        remapped = {}
        for key, value in values.items():
            if key == source or key.startswith(source + "/"):
                key = target + key[len(source):]
            remapped[key] = value
        self._write_input_kinds(remapped)

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

    def create(self, rel_path: str, kind: str, input_kind: str = "none") -> None:
        path = self._resolve(rel_path)
        if os.path.exists(path):
            raise WorkspaceError("already exists")
        if kind == "dir":
            os.makedirs(path)
        else:
            input_kind = normalize_input_kind(input_kind)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(scaffold_content(rel_path, input_kind))
            self.set_input_kind(rel_path, input_kind)

    def delete(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)
        else:
            raise WorkspaceError("no such path")
        self._remove_input_kinds(rel_path)

    def rename(self, rel_path: str, new_rel_path: str) -> None:
        src = self._resolve(rel_path)
        dst = self._resolve(new_rel_path)
        if not os.path.exists(src):
            raise WorkspaceError("no such path")
        if os.path.exists(dst):
            raise WorkspaceError("destination already exists")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        self._rename_input_kinds(rel_path, new_rel_path)


class Job:
    """One run's live state: buffered output lines plus a monotonic seq
    so the frontend can poll "give me everything after N" cheaply."""

    def __init__(self, job_id: str, language: str, input_kind: str,
                 input_preview: str, input_text: str, source_hash: str):
        self.id = job_id
        self.language = language
        self.input_kind = input_kind
        self.input_preview = input_preview
        self.input_text = input_text
        self.source_hash = source_hash
        self.status = "running"  # running | exited | killed | error
        self.exit_code: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self.runtime_trace: Optional[Dict] = None
        self._kill_requested = False
        self._lines: List[Dict] = []
        self._next_seq = 0
        self._lock = threading.Lock()

    def append(self, stream: str, text: str) -> None:
        with self._lock:
            self._lines.append({"seq": self._next_seq, "stream": stream, "text": text})
            self._next_seq += 1
            if len(self._lines) > OUTPUT_LINE_LIMIT:
                self._lines = self._lines[-OUTPUT_LINE_LIMIT:]

    def set_runtime_trace(self, trace: Optional[Dict]) -> None:
        with self._lock:
            self.runtime_trace = trace

    def since(self, seq: int) -> Dict:
        with self._lock:
            lines = [line for line in self._lines if line["seq"] >= seq]
            next_seq = self._next_seq
            status = self.status
            exit_code = self.exit_code
            runtime_trace = self.runtime_trace
        return {"lines": lines, "seq": next_seq, "status": status,
                "exitCode": exit_code, "runtimeTrace": runtime_trace,
                "sourceHash": self.source_hash}

    def request_kill(self) -> bool:
        # Keep the job in "running" until the worker has drained stdout/stderr
        # and waited for the child. Otherwise the browser stops polling early
        # and the final lines disappear from the displayed run.
        with self._lock:
            if self.status != "running" or self._kill_requested:
                return False
            self._kill_requested = True
            process = self.process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        return True

    def kill_requested(self) -> bool:
        with self._lock:
            return self._kill_requested

    def finish(self, status: str, exit_code: Optional[int]) -> None:
        with self._lock:
            self.exit_code = exit_code
            self.status = status


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

    def start(self, workspace: Workspace, rel_path: str, language: str, input_kind: str = "none") -> Job:
        if language not in SUPPORTED_LANGUAGES:
            raise WorkspaceError("unsupported language")
        generated = generate_input(input_kind)
        with self._lock:
            active = self._jobs.get(self._active_id) if self._active_id else None
            if active and active.status == "running":
                raise WorkspaceError("a job is already running")
        abs_path = workspace.abs_path(rel_path)
        if not os.path.isfile(abs_path):
            raise WorkspaceError("no such file")
        try:
            with open(abs_path, "rb") as source_file:
                source_hash = hashlib.sha256(source_file.read()).hexdigest()
        except OSError as exc:
            raise WorkspaceError("could not read source file: {}".format(exc))
        job = Job(uuid.uuid4().hex[:12], language, generated["kind"],
                  generated["preview"], generated["text"], source_hash)
        with self._lock:
            self._jobs[job.id] = job
            self._active_id = job.id
        threading.Thread(target=self._run, args=(job, abs_path, generated["text"]), daemon=True).start()
        return job

    def kill(self, job_id: str) -> bool:
        job = self.get(job_id)
        return job.request_kill() if job else False

    def output_since(self, job_id: str, seq: int) -> Optional[Dict]:
        job = self.get(job_id)
        return job.since(seq) if job else None

    # ── execution ──────────────────────────────────────────────────────
    def _run(self, job: Job, abs_path: str, input_text: str) -> None:
        try:
            if job.language == "python":
                self._run_python(job, abs_path, input_text)
            else:
                self._run_java(job, abs_path, input_text)
        except Exception as exc:  # noqa: BLE001 - report, never crash the worker thread
            job.append("stderr", str(exc))
            job.status = "error"

    def _stream_to_completion(self, job: Job, process: subprocess.Popen,
                              input_text: str, trace_path: Optional[str] = None) -> None:
        # Set before starting the pumps so a Stop click landing in the first
        # instant after Popen() still has a live process handle to kill.
        job.process = process
        if job.kill_requested():
            try:
                process.kill()
            except OSError:
                pass

        try:
            if process.stdin is not None:
                if input_text:
                    process.stdin.write(input_text)
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        def pump(stream, name):
            for line in iter(stream.readline, ""):
                job.append(name, line.rstrip("\n"))
            stream.close()
        t_out = threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = process.wait()
            job.append("stderr", "[Killed: exceeded {}s time limit]".format(RUN_TIMEOUT_SECONDS))
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        if trace_path:
            trace = None
            try:
                with open(trace_path, encoding="utf-8") as trace_file:
                    trace = json.load(trace_file)
            except (OSError, ValueError):
                trace = {"version": 1, "inputKind": job.input_kind,
                         "model": {"type": "none", "initialState": None,
                                   "finalState": None}, "frames": [],
                         "truncated": False}
            finally:
                try:
                    os.remove(trace_path)
                except OSError:
                    pass
            trace["input"] = input_text
            trace["inputPreview"] = job.input_preview
            trace["sourceHash"] = job.source_hash
            trace["stdout"] = "\n".join(line["text"] for line in job._lines
                                          if line["stream"] == "stdout")
            trace["stderr"] = "\n".join(line["text"] for line in job._lines
                                          if line["stream"] == "stderr")
            trace["exitCode"] = exit_code
            trace["verified"] = exit_code == 0 and not job.kill_requested()
            job.set_runtime_trace(trace)
        job.finish("killed" if job.kill_requested() or timed_out else "exited", exit_code)

    def _run_python(self, job: Job, abs_path: str, input_text: str) -> None:
        python = next((c for c in PYTHON_CANDIDATES if which(c)), None)
        if not python:
            job.append("stderr", "No local Python interpreter (python3/python) found on PATH.")
            job.status = "error"
            return
        trace_fd, trace_path = tempfile.mkstemp(prefix=RUNTIME_TRACE_PREFIX,
                                                suffix=".json",
                                                dir=os.path.dirname(abs_path))
        os.close(trace_fd)
        runner = os.path.abspath(os.path.join(os.path.dirname(__file__), "lab_runtime.py"))
        process = subprocess.Popen(
            [python, "-u", runner, abs_path, trace_path, job.input_kind],
            cwd=os.path.dirname(abs_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, **_popen_kwargs())
        self._stream_to_completion(job, process, input_text, trace_path)

    def _run_java(self, job: Job, abs_path: str, input_text: str) -> None:
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
            cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, **_popen_kwargs())
        self._stream_to_completion(job, process, input_text)
