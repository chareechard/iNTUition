"""Local file workspace and process execution for the in-browser Lab IDE.

Files live under <download_root>/.intuition/lab/workspace/. Running a file
shells out to a real local python3 / javac+java / gcc(or clang/cc) on PATH -
the same trust boundary as running it from a terminal, not a sandboxed
runtime. The one safety net beyond the user's own Stop button is a hard
wall-clock timeout, so a runaway script can't pin a background thread forever.
"""
import ast
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

from intuition import ai_provider, lab_analysis

STORAGE_DIR = os.path.join(".intuition", "lab")
WORKSPACE_SUBDIR = "workspace"  # pre-repositories flat layout, migrated on first load
REPOS_SUBDIR = "repos"          # <download_root>/.intuition/lab/repos/<repo>/...
DEFAULT_REPO_NAME = "sandbox"
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}")
INPUT_METADATA_FILE = ".input-kinds.json"
ORDER_METADATA_FILE = ".file-order.json"  # per-directory drag order in the tree
RUN_TIMEOUT_SECONDS = 30
OUTPUT_LINE_LIMIT = 4000  # per job - bounds memory for a runaway print loop
RUNTIME_TRACE_PREFIX = ".lab-trace-"

PYTHON_CANDIDATES = ("python3", "python")
C_COMPILER_CANDIDATES = ("gcc", "clang", "cc")
SUPPORTED_LANGUAGES = ("python", "java", "c")

# Which extensions are runnable source (Run button, input scaffolds, FRIDAY
# coach). Every other extension is a plain document: it can be created, edited
# and saved inside any folder, but not Run.
CODE_EXTENSIONS = {".py": "python", ".java": "java", ".c": "c"}

# Windows file-naming rules. The Lab's folder tree mirrors them so a repository
# can be copied straight onto a real Windows disk without hitting an illegal
# name - see learn.microsoft.com/windows/win32/fileio/naming-a-file.
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{}".format(i) for i in range(1, 10)]
    + ["LPT{}".format(i) for i in range(1, 10)]
)
_WINDOWS_ILLEGAL_CHARS = set('<>:"/\\|?*')
MAX_NAME_LENGTH = 255   # one path component (the NTFS per-name limit)
MAX_PATH_LENGTH = 4096  # the whole workspace-relative path


class WorkspaceError(ValueError):
    pass


def validate_name(name: str) -> str:
    """Check one path component (a single file or folder name) against the
    Windows file-naming rules, raising WorkspaceError on the first violation."""
    name = str(name or "")
    if not name or name in (".", ".."):
        raise WorkspaceError("a name is required")
    if name.startswith("."):
        # the Lab hides dot-prefixed entries (its own .file-order.json etc. live
        # there), so a dot-file a user made would silently vanish from the tree.
        raise WorkspaceError("a name cannot start with a period")
    if len(name) > MAX_NAME_LENGTH:
        raise WorkspaceError(
            "a name cannot be longer than {} characters".format(MAX_NAME_LENGTH))
    if any(ch in _WINDOWS_ILLEGAL_CHARS for ch in name):
        raise WorkspaceError('a name cannot contain any of  < > : " / \\ | ? *')
    if any(ord(ch) < 32 for ch in name):
        raise WorkspaceError("a name cannot contain control characters")
    if name[-1] in (" ", "."):
        raise WorkspaceError("a name cannot end with a space or a period")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise WorkspaceError('"{}" is a reserved device name on Windows'.format(
            name.split(".", 1)[0]))
    return name


def validate_rel_path(rel_path: str) -> str:
    """Validate every component of a workspace-relative path and return it
    normalised to forward slashes with no leading/trailing/repeated separator."""
    # Only surrounding slashes are trimmed - never whitespace, so a trailing
    # space or period on the final component is still caught by validate_name.
    parts = [part for part in
             str(rel_path or "").replace("\\", "/").strip("/").split("/") if part]
    if not parts:
        raise WorkspaceError("a path is required")
    if len("/".join(parts)) > MAX_PATH_LENGTH:
        raise WorkspaceError("that path is too long")
    for part in parts:
        validate_name(part)
    return "/".join(parts)


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


def _c_solve_parts(content: str) -> Optional[Dict[str, object]]:
    """Locate a `... solve(...) { ... }` definition and split it out.

    Brace/comment/string scanning is the same as Java's - `_java_matching_brace`
    already understands `//`, `/* */`, `"` and `'`, which is all C needs here.
    """
    match = re.search(
        r"(?m)^[ \t]*(?:static\s+)?[A-Za-z_][A-Za-z0-9_ \t\*]*?\bsolve\s*"
        r"\([^;{)]*\)\s*\{",
        content,
    )
    if not match:
        return None
    opening = content.find("{", match.start(), match.end())
    closing = _java_matching_brace(content, opening)
    if closing < 0:
        return None
    # The C scaffolds use fixed, sensible parameter names (values / value /
    # matrix / edges) and a kind switch always rewrites the whole signature +
    # stdin adapter, so there is no user-chosen name to carry across - unlike
    # Python/Java, migration here only needs to preserve the solve body.
    return {
        "start": match.start(),
        "opening": opening,
        "closing": closing,
        "signature": content[match.start():opening + 1],
        "body": content[opening + 1:closing],
        "parameter": "",
    }


def _migrate_python_scaffold(rel_path: str, input_kind: str, current: str) -> Optional[str]:
    old = _python_solve_parts(current)
    if not old:
        return None
    parameter = old.get("parameter")
    new = scaffold_content(rel_path, input_kind,
                           parameter if isinstance(parameter, str) else None)
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
    parameter = old.get("parameter")
    new = scaffold_content(rel_path, input_kind,
                           parameter if isinstance(parameter, str) else None)
    generated = _java_solve_parts(new)
    if not generated:
        return None
    start = int(str(generated["start"]))
    opening = int(str(generated["opening"]))
    closing = int(str(generated["closing"]))
    method = (new[start:opening + 1] + str(old["body"]) +
              new[closing:closing + 1])
    return new[:start] + method + new[closing + 1:]


def _migrate_c_scaffold(rel_path: str, input_kind: str, current: str) -> Optional[str]:
    old = _c_solve_parts(current)
    if not old:
        return None
    parameter = old.get("parameter")
    new = scaffold_content(rel_path, input_kind,
                           parameter if isinstance(parameter, str) else None)
    generated = _c_solve_parts(new)
    if not generated:
        return None
    start = int(str(generated["start"]))
    opening = int(str(generated["opening"]))
    closing = int(str(generated["closing"]))
    # Keep the fresh scaffold's includes + stdin adapter (the whole point of a
    # kind switch); splice the student's solve body into its new signature.
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
    elif lower.endswith(".c"):
        migrated = _migrate_c_scaffold(rel_path, kind, current)
    else:
        raise WorkspaceError("input scaffolds are available for Python, Java and C files")
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
    if lower.endswith(".c"):
        if kind == "none":
            return ("#include <stdio.h>\n"
                    "\n"
                    "void solve(void) {\n"
                    "    // Write your solution here.\n"
                    "}\n"
                    "\n"
                    "int main(void) {\n"
                    "    solve();\n"
                    "    return 0;\n"
                    "}\n")
        if kind == "array":
            return ("#include <stdio.h>\n"
                    "#include <stdlib.h>\n"
                    "\n"
                    "void solve(int *" + parameter + ", int n) {\n"
                    "    // Write your solution here.\n"
                    "}\n"
                    "\n"
                    "int main(void) {\n"
                    "    int capacity = 16, n = 0, *" + parameter + " = malloc(capacity * sizeof(int));\n"
                    "    while (" + parameter + " && scanf(\"%d\", &" + parameter + "[n]) == 1) {\n"
                    "        if (++n == capacity) " + parameter + " = realloc(" + parameter + ", (capacity *= 2) * sizeof(int));\n"
                    "    }\n"
                    "    solve(" + parameter + ", n);\n"
                    "    free(" + parameter + ");\n"
                    "    return 0;\n"
                    "}\n")
        if kind == "string":
            return ("#include <stdio.h>\n"
                    "#include <string.h>\n"
                    "\n"
                    "void solve(const char *" + parameter + ") {\n"
                    "    // Write your solution here.\n"
                    "}\n"
                    "\n"
                    "int main(void) {\n"
                    "    char " + parameter + "[4096];\n"
                    "    if (!fgets(" + parameter + ", sizeof(" + parameter + "), stdin)) " + parameter + "[0] = '\\0';\n"
                    "    " + parameter + "[strcspn(" + parameter + ", \"\\r\\n\")] = '\\0';\n"
                    "    solve(" + parameter + ");\n"
                    "    return 0;\n"
                    "}\n")
        if kind == "matrix":
            return ("#include <stdio.h>\n"
                    "#include <stdlib.h>\n"
                    "\n"
                    "void solve(int **" + parameter + ", int rows, int cols) {\n"
                    "    // Write your solution here.\n"
                    "}\n"
                    "\n"
                    "int main(void) {\n"
                    "    int rows = 0, cols = 0;\n"
                    "    if (scanf(\"%d %d\", &rows, &cols) != 2) return 0;\n"
                    "    int **" + parameter + " = malloc(rows * sizeof(int *));\n"
                    "    for (int r = 0; r < rows; r++) {\n"
                    "        " + parameter + "[r] = malloc(cols * sizeof(int));\n"
                    "        for (int c = 0; c < cols; c++) scanf(\"%d\", &" + parameter + "[r][c]);\n"
                    "    }\n"
                    "    solve(" + parameter + ", rows, cols);\n"
                    "    for (int r = 0; r < rows; r++) free(" + parameter + "[r]);\n"
                    "    free(" + parameter + ");\n"
                    "    return 0;\n"
                    "}\n")
        if kind == "graph":
            return ("#include <stdio.h>\n"
                    "#include <stdlib.h>\n"
                    "\n"
                    "typedef struct { int source, target, weight; } Edge;\n"
                    "\n"
                    "void solve(int node_count, Edge *edges, int edge_count) {\n"
                    "    // edges is an array of (source, target, weight) triples.\n"
                    "    // Write your solution here.\n"
                    "}\n"
                    "\n"
                    "int main(void) {\n"
                    "    int node_count = 0, edge_count = 0;\n"
                    "    if (scanf(\"%d %d\", &node_count, &edge_count) != 2) return 0;\n"
                    "    Edge *edges = malloc(edge_count * sizeof(Edge));\n"
                    "    for (int i = 0; i < edge_count; i++)\n"
                    "        scanf(\"%d %d %d\", &edges[i].source, &edges[i].target, &edges[i].weight);\n"
                    "    solve(node_count, edges, edge_count);\n"
                    "    free(edges);\n"
                    "    return 0;\n"
                    "}\n")
        return ("#include <stdio.h>\n"
                "#include <stdlib.h>\n"
                "\n"
                "void solve(int *" + parameter + ", int n) {\n"
                "    // " + parameter + " is a level-order array; -1 marks an empty node.\n"
                "    // Write your solution here.\n"
                "}\n"
                "\n"
                "int main(void) {\n"
                "    int n = 0;\n"
                "    if (scanf(\"%d\", &n) != 1) return 0;\n"
                "    int *" + parameter + " = malloc(n * sizeof(int));\n"
                "    for (int i = 0; i < n; i++) scanf(\"%d\", &" + parameter + "[i]);\n"
                "    solve(" + parameter + ", n);\n"
                "    free(" + parameter + ");\n"
                "    return 0;\n"
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

# ── Missing-entry-point autofill ────────────────────────────────────────────
# The single most common reason a student's file "does nothing" when run: it
# defines the algorithm but never calls it (no driver, no `if __name__ ==
# "__main__":`). runpy executes the module top-to-bottom either way, so this
# is invisible as an error - exit code 0, empty output, a trivial runtime
# trace with nothing to simulate. Detected via ast rather than a Run-time
# failure, and fixed by asking the model for a small, sandboxed driver.

LAB_ENTRYPOINT_SYSTEM = """You are FRIDAY, completing one Python source file open in a student's local IDE. The file defines one or more functions but never calls any of them, so running it does nothing. Write ONLY the missing driver: a `if __name__ == "__main__":` block that builds small, concrete sample input matching what the function(s) actually operate on (an adjacency structure for a graph function, a short list of numbers for a sorting/searching one, a string, and so on), calls the primary function(s) defined in the file with it, and prints the result so the run produces visible output. Respond with strict JSON only - no prose, no markdown fences. Schema: {"driver": "<the exact Python source of the if __name__ == \\"__main__\\": block and nothing else, correctly indented, as it will be appended verbatim to the end of the file>"}. Only call names already defined in the file below, plus ordinary builtins (print, len, range, sorted, and the like) - never invent a name, never add an import, never read stdin, never write or open a file, never touch the network or a subprocess. If the file already has a working entry point, or you cannot tell what a safe call would look like, respond with {"driver": ""}."""


def _module_top_level(tree: ast.Module) -> List[ast.stmt]:
    return tree.body


def _stmts_do_work(stmts: List[ast.stmt]) -> bool:
    """True if any of these statements plausibly runs the module's own code
    at import time - a direct call, an assignment fed by one, a loop/with/try
    doing real work - rather than just declaring functions/classes/constants."""
    for node in stmts:
        if isinstance(node, ast.If):
            if _stmts_do_work(node.body) or _stmts_do_work(node.orelse):
                return True
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(getattr(node, "value", None), ast.Call):
                return True
        elif isinstance(node, (ast.For, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            return True
    return False


def needs_entry_point(source: str) -> bool:
    """True when the file defines a function worth simulating but never
    actually runs one - the common "wrote the algorithm, forgot the driver"
    gap that leaves Run producing nothing to trace."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False  # broken code is a syntax problem, not a missing-driver one
    top = _module_top_level(tree)
    defines_function = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           for n in top)
    return defines_function and not _stmts_do_work(top)


_DRIVER_DENYLIST = (
    "subprocess", "os.system", "socket", "urllib", "requests", "eval(",
    "exec(", "__import__", "shutil", "open(", "input(", "import ",
)
_DRIVER_SAFE_CALLS = frozenset({
    "print", "len", "range", "str", "repr", "list", "dict", "set", "tuple",
    "sorted", "enumerate", "min", "max", "sum", "abs", "round", "zip", "map",
    "filter", "int", "float", "bool", "reversed", "frozenset", "type",
})


def _driver_is_safe(driver: str, source_tree: ast.Module) -> bool:
    """A second, independent check on top of ``parse_entrypoint_response``'s
    shape validation, before code the model wrote is appended to and then
    executed from a real file: no dangerous tokens by substring, and every
    call it makes resolves to either a builtin or a name the file itself
    already defines - so a hallucinated call fails closed instead of running."""
    if any(token in driver for token in _DRIVER_DENYLIST):
        return False
    try:
        driver_tree = ast.parse(driver)
    except SyntaxError:
        return False
    defined = {n.name for n in source_tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for node in ast.walk(driver_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in defined and node.func.id not in _DRIVER_SAFE_CALLS:
                return False
    return True


def autofill_entry_point(workspace: "Workspace", rel_path: str,
                          preferred_backend: Optional[str], download_root: str) -> Optional[str]:
    """When ``rel_path`` defines a function but never calls one, ask the model
    for a small driver and append it to the file. Returns the new file content
    when it made a change, else None - including every failure case (no AI
    backend, a bad or unsafe response, a race with an edit): autofill is a
    convenience on top of Run, never a reason to block it.
    """
    try:
        content = workspace.read(rel_path)
    except WorkspaceError:
        return None
    if not needs_entry_point(content):
        return None
    lines = content.splitlines()
    numbered = "\n".join("{}: {}".format(i + 1, line) for i, line in enumerate(lines))
    prompt = "Source file ({}):\n{}\n---\nRespond with the JSON driver now.".format(
        rel_path, numbered)
    try:
        result = ai_provider.complete_tier(
            "chat", prompt, LAB_ENTRYPOINT_SYSTEM, preferred=preferred_backend,
            max_tokens=400, download_root=download_root)
    except ai_provider.ProviderError:
        return None
    driver = lab_analysis.parse_entrypoint_response(result.get("text") or "")
    if not driver:
        return None
    try:
        source_tree = ast.parse(content)
    except SyntaxError:
        return None
    if not _driver_is_safe(driver, source_tree):
        return None
    separator = "\n\n" if content and not content.endswith("\n\n") else ""
    if content and not content.endswith("\n"):
        separator = "\n" + separator
    new_content = content + separator + (
        "# --- Entry point added by FRIDAY so this file has something to run - "
        "edit or delete freely ---\n" + driver + "\n")
    try:
        current = workspace.read(rel_path)
    except WorkspaceError:
        return None
    if current != content:
        return None  # the student changed the file while the model was thinking
    workspace.write(rel_path, new_content)
    return new_content


def _popen_kwargs() -> Dict:
    # No console window flashing up behind the dashboard on Windows - the
    # same flag claude_bridge.py already uses for its own subprocess calls.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class Workspace:
    """CRUD over one directory tree, confined to it - never escapes the root.

    A bare ``Workspace(download_root)`` is the legacy flat lab folder; a single
    lab repository is a ``Workspace.at(repo_dir)`` handed out by ``LabRepos``.
    """

    def __init__(self, download_root: str):
        self.root = os.path.join(os.path.abspath(download_root), STORAGE_DIR, WORKSPACE_SUBDIR)
        os.makedirs(self.root, exist_ok=True)

    @classmethod
    def at(cls, directory: str) -> "Workspace":
        """A workspace confined to an exact directory (one lab repository)."""
        obj = cls.__new__(cls)
        obj.root = os.path.abspath(directory)
        os.makedirs(obj.root, exist_ok=True)
        return obj

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
        if os.path.splitext(path)[1].lower() not in CODE_EXTENSIONS:
            return  # plain documents have no randomized-input slot
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

    # ── explicit tree ordering (drag a file up/down in the Lab tree) ────
    def _order_path(self) -> str:
        return os.path.join(self.root, ORDER_METADATA_FILE)

    def _read_order(self) -> Dict[str, List[str]]:
        """{parent rel path -> [child name, ...]}; the repo root is the key ""."""
        try:
            with open(self._order_path(), encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        clean: Dict[str, List[str]] = {}
        for parent, names in raw.items():
            if isinstance(parent, str) and isinstance(names, list):
                clean[parent] = [n for n in names if isinstance(n, str)]
        return clean

    def _write_order(self, values: Dict[str, List[str]]) -> None:
        values = {parent: names for parent, names in values.items() if names}
        if not values:
            try:
                os.remove(self._order_path())
            except OSError:
                pass
            return
        with open(self._order_path(), "w", encoding="utf-8", newline="\n") as f:
            json.dump(values, f, ensure_ascii=True, sort_keys=True, indent=2)
            f.write("\n")

    def _ordered_names(self, parent_rel: str, names: List[str]) -> List[str]:
        """Apply the saved drag order for one directory; names it does not
        mention keep their incoming (alphabetical) order, after the placed ones."""
        saved = self._read_order().get(parent_rel, [])
        rank = {name: index for index, name in enumerate(saved)}
        return sorted(names, key=lambda name: (rank.get(name, len(saved)), name))

    def _parent_dir(self, parent_rel: str) -> str:
        parent_rel = (parent_rel or "").strip().strip("/")
        return self._resolve(parent_rel) if parent_rel else self.root

    def reorder(self, parent_rel: str, ordered_names: List[str]) -> None:
        """Persist a new sibling order for one directory (repo root is "")."""
        parent_rel = (parent_rel or "").strip().strip("/")
        parent_dir = self._parent_dir(parent_rel)
        if not os.path.isdir(parent_dir):
            raise WorkspaceError("no such folder")
        present = [name for name in os.listdir(parent_dir)
                   if not name.startswith(".")]
        present_set = set(present)
        placed = [name for name in ordered_names if name in present_set]
        seen = set(placed)
        placed.extend(sorted(name for name in present if name not in seen))
        order = self._read_order()
        order[parent_rel] = placed
        self._write_order(order)

    def _order_append(self, rel_path: str) -> None:
        canonical = self._canonical_rel_path(self._resolve(rel_path))
        parent, _, name = canonical.rpartition("/")
        order = self._read_order()
        siblings = order.get(parent)
        if siblings is None:
            siblings = sorted(n for n in os.listdir(self._parent_dir(parent))
                              if not n.startswith(".") and n != name)
        if name not in siblings:
            order[parent] = siblings + [name]
            self._write_order(order)

    def _order_forget(self, rel_path: str) -> None:
        canonical = self._canonical_rel_path(self._resolve(rel_path))
        parent, _, name = canonical.rpartition("/")
        order = self._read_order()
        changed = False
        for key in list(order):
            if key == canonical or key.startswith(canonical + "/"):
                del order[key]
                changed = True
        siblings = order.get(parent)
        if siblings and name in siblings:
            order[parent] = [n for n in siblings if n != name]
            changed = True
        if changed:
            self._write_order(order)

    def _order_rename(self, src: str, dst: str) -> None:
        src_c = self._canonical_rel_path(self._resolve(src))
        dst_c = self._canonical_rel_path(self._resolve(dst))
        remapped: Dict[str, List[str]] = {}
        for key, names in self._read_order().items():
            if key == src_c:
                remapped[dst_c] = names
            elif key.startswith(src_c + "/"):
                remapped[dst_c + key[len(src_c):]] = names
            else:
                remapped[key] = names
        src_parent, _, src_name = src_c.rpartition("/")
        dst_parent, _, dst_name = dst_c.rpartition("/")
        siblings = remapped.get(src_parent)
        if siblings and src_name in siblings:
            if src_parent == dst_parent:
                remapped[src_parent] = [dst_name if n == src_name else n
                                        for n in siblings]
            else:
                remapped[src_parent] = [n for n in siblings if n != src_name]
                tail = remapped.get(dst_parent, [])
                if dst_name not in tail:
                    remapped[dst_parent] = tail + [dst_name]
        self._write_order(remapped)

    def _assert_available(self, abs_path: str, allow: Optional[str] = None) -> None:
        """Refuse a name that collides with an existing sibling. Matching is
        case-insensitive, like Windows/NTFS - "Main.py" and "main.py" cannot
        share a folder. ``allow`` is an existing path that may keep its slot
        (so a file can be renamed to a different casing of its own name)."""
        parent = os.path.dirname(abs_path)
        if not os.path.isdir(parent):
            return
        wanted = os.path.basename(abs_path).lower()
        allow_real = os.path.realpath(allow) if allow else None
        for existing in os.listdir(parent):
            if existing.lower() != wanted:
                continue
            if allow_real and os.path.realpath(os.path.join(parent, existing)) == allow_real:
                continue
            raise WorkspaceError(
                '"{}" already exists here (names are case-insensitive, '
                'like Windows)'.format(existing))

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
            names = self._ordered_names(rel, [n for n in names if not n.startswith(".")])
            entries = []
            for name in names:
                full = os.path.join(dir_path, name)
                rel_child = "{}/{}".format(rel, name) if rel else name
                if os.path.isdir(full):
                    entries.append({"path": rel_child, "name": name, "type": "dir",
                                     "children": walk(full, rel_child)})
                else:
                    entries.append({"path": rel_child, "name": name, "type": "file",
                                     "ext": os.path.splitext(name)[1].lower().lstrip(".")})
            return entries
        return walk(self.root, "")

    def read(self, rel_path: str) -> str:
        path = self._resolve(rel_path)
        if not os.path.isfile(path):
            raise WorkspaceError("no such file")
        with open(path, "rb") as f:
            sample = f.read(8192)
        if b"\x00" in sample:
            raise WorkspaceError(
                "this looks like a binary file - it can't be opened in the editor")
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def write(self, rel_path: str, content: str) -> None:
        path = self._resolve(validate_rel_path(rel_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content or "")

    def create(self, rel_path: str, kind: str, input_kind: str = "none") -> None:
        rel_path = validate_rel_path(rel_path)
        path = self._resolve(rel_path)
        if os.path.exists(path):
            raise WorkspaceError("already exists")
        self._assert_available(path)
        if kind == "dir":
            os.makedirs(path)  # nested parents come along, like Explorer's New folder
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.splitext(path)[1].lower() in CODE_EXTENSIONS:
                input_kind = normalize_input_kind(input_kind)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(scaffold_content(rel_path, input_kind))
                self.set_input_kind(rel_path, input_kind)
            else:
                # a plain document (.md, .txt, .json, ...): no runnable scaffold
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write("")
        self._order_append(rel_path)

    def delete(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)
        else:
            raise WorkspaceError("no such path")
        self._remove_input_kinds(rel_path)
        self._order_forget(rel_path)

    def rename(self, rel_path: str, new_rel_path: str) -> None:
        new_rel_path = validate_rel_path(new_rel_path)
        src = self._resolve(rel_path)
        dst = self._resolve(new_rel_path)
        if not os.path.exists(src):
            raise WorkspaceError("no such path")
        if os.path.exists(dst) and os.path.realpath(dst) != os.path.realpath(src):
            raise WorkspaceError("destination already exists")
        self._assert_available(dst, allow=src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        self._rename_input_kinds(rel_path, new_rel_path)
        self._order_rename(rel_path, new_rel_path)


class LabRepos:
    """The Lab's top level: a set of named repositories, each an isolated
    ``Workspace`` tree under ``<download_root>/.intuition/lab/repos/``. Grouping
    is physical - a file belongs to a repository by living under it - and a
    single Run/compile only ever sees one repository.
    """

    def __init__(self, download_root: str):
        lab_dir = os.path.join(os.path.abspath(download_root), STORAGE_DIR)
        self.root = os.path.join(lab_dir, REPOS_SUBDIR)
        os.makedirs(self.root, exist_ok=True)
        self._adopt_legacy_workspace(os.path.join(lab_dir, WORKSPACE_SUBDIR))
        if not self.list():
            self.create(DEFAULT_REPO_NAME)

    def _adopt_legacy_workspace(self, legacy_dir: str) -> None:
        """Move a pre-repositories flat workspace into its own repository, once."""
        target = os.path.join(self.root, DEFAULT_REPO_NAME)
        if not os.path.isdir(legacy_dir) or os.path.exists(target):
            return
        if any(not name.startswith(".") for name in os.listdir(legacy_dir)):
            shutil.move(legacy_dir, target)

    def _repo_dir(self, name: str) -> str:
        name = (name or "").strip()
        if not _REPO_NAME_RE.fullmatch(name):
            raise WorkspaceError(
                "repository names use letters, numbers, spaces, hyphen and underscore")
        candidate = os.path.realpath(os.path.join(self.root, name))
        if os.path.dirname(candidate) != os.path.realpath(self.root):
            raise WorkspaceError("invalid repository name")
        return candidate

    @staticmethod
    def _file_count(repo_path: str) -> int:
        total = 0
        for _dir, _subdirs, files in os.walk(repo_path):
            total += sum(1 for name in files if not name.startswith("."))
        return total

    def list(self) -> List[Dict]:
        names = sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []
        return [{"name": name,
                 "fileCount": self._file_count(os.path.join(self.root, name))}
                for name in names
                if not name.startswith(".")
                and os.path.isdir(os.path.join(self.root, name))]

    def names(self) -> List[str]:
        return [repo["name"] for repo in self.list()]

    def default_name(self) -> str:
        names = self.names()
        if DEFAULT_REPO_NAME in names:
            return DEFAULT_REPO_NAME
        return names[0] if names else DEFAULT_REPO_NAME

    def exists(self, name: str) -> bool:
        try:
            return os.path.isdir(self._repo_dir(name))
        except WorkspaceError:
            return False

    def create(self, name: str) -> "Workspace":
        path = self._repo_dir(name)
        if os.path.exists(path):
            raise WorkspaceError("a repository with that name already exists")
        os.makedirs(path)
        return Workspace.at(path)

    def delete(self, name: str) -> None:
        path = self._repo_dir(name)
        if not os.path.isdir(path):
            raise WorkspaceError("no such repository")
        shutil.rmtree(path)
        if not self.list():  # a lab always has at least one repository
            self.create(DEFAULT_REPO_NAME)

    def rename(self, name: str, new_name: str) -> None:
        src = self._repo_dir(name)
        dst = self._repo_dir(new_name)
        if not os.path.isdir(src):
            raise WorkspaceError("no such repository")
        if os.path.exists(dst):
            raise WorkspaceError("a repository with that name already exists")
        os.rename(src, dst)

    def workspace(self, name: str) -> "Workspace":
        path = self._repo_dir(name)
        if not os.path.isdir(path):
            raise WorkspaceError("no such repository")
        return Workspace.at(path)

    def move_file(self, source: str, rel_path: str, dest: str) -> str:
        """Move one file or folder from one repository to another repository's
        top level. Its randomized-input metadata and drag-order slot travel
        with it; a name collision in the destination is refused, not clobbered.
        """
        src_ws = self.workspace(source)
        dst_ws = self.workspace(dest)
        if os.path.realpath(src_ws.root) == os.path.realpath(dst_ws.root):
            raise WorkspaceError("source and destination are the same repository")
        src_abs = src_ws.abs_path(rel_path)
        if not os.path.exists(src_abs):
            raise WorkspaceError("no such path")
        name = os.path.basename(src_abs)
        dst_abs = dst_ws.abs_path(name)
        if os.path.exists(dst_abs):
            raise WorkspaceError(
                '"{}" already exists in repository "{}"'.format(name, dest))
        canonical = src_ws._canonical_rel_path(src_abs)
        src_kinds = src_ws._read_input_kinds()
        carried = {key: value for key, value in src_kinds.items()
                   if key == canonical or key.startswith(canonical + "/")}
        src_ws._order_forget(rel_path)
        shutil.move(src_abs, dst_abs)
        if carried:
            src_ws._write_input_kinds({key: value for key, value in src_kinds.items()
                                       if key not in carried})
            dst_kinds = dst_ws._read_input_kinds()
            for key, value in carried.items():
                dst_kinds[name + key[len(canonical):]] = value
            dst_ws._write_input_kinds(dst_kinds)
        dst_ws._order_append(name)
        return name

    def resolve(self, name: str):
        """Like ``workspace`` but forgiving: an unknown/blank name falls back to
        the default repository instead of raising - the browser can hold a stale
        selection across a delete or a fresh session. Always returns a usable
        ``(Workspace, name)``, recreating the default repository if it is gone.
        """
        if self.exists(name):
            return self.workspace(name), name
        fallback = self.default_name()
        if not self.exists(fallback):
            self.create(fallback)
        return self.workspace(fallback), fallback


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
            elif job.language == "c":
                self._run_c(job, abs_path, input_text)
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
                trace = {"version": 3, "inputKind": job.input_kind,
                         "model": {"type": "none", "initialState": None,
                                   "finalState": None}, "frames": [],
                         "eventCount": 0, "truncated": False}
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

    def _run_c(self, job: Job, abs_path: str, input_text: str) -> None:
        compiler = next((c for c in C_COMPILER_CANDIDATES if which(c)), None)
        if not compiler:
            job.append("stderr", "No local C compiler (gcc/clang/cc) found on PATH.")
            job.status = "error"
            return
        directory = os.path.dirname(abs_path)
        filename = os.path.basename(abs_path)
        # Build into a hidden temp dir inside the workspace folder: the compiled
        # binary never litters the file tree (tree() skips dot-names), and the
        # program still runs with cwd = the workspace so its own file I/O lands
        # where the student expects.
        build_dir = tempfile.mkdtemp(prefix=".lab-cc-", dir=directory)
        exe = os.path.join(build_dir, "program.exe" if sys.platform == "win32" else "program")
        try:
            try:
                compiled = subprocess.run(
                    [compiler, "-std=c11", "-O0", "-g", "-Wall", filename, "-o", exe, "-lm"],
                    cwd=directory, capture_output=True, text=True,
                    timeout=RUN_TIMEOUT_SECONDS, **_popen_kwargs())
            except subprocess.TimeoutExpired:
                job.append("stderr", "[Killed: compile exceeded {}s time limit]".format(RUN_TIMEOUT_SECONDS))
                job.status = "killed"
                return
            for line in (compiled.stdout or "").splitlines():
                job.append("stdout", line)
            # gcc/clang report both errors and warnings on stderr; show them
            # either way so a learner sees an implicit declaration or a format
            # mismatch, not just a silent miscompile.
            for line in (compiled.stderr or "").splitlines():
                job.append("stderr", line)
            if compiled.returncode != 0:
                job.status = "error"
                job.exit_code = compiled.returncode
                return
            process = subprocess.Popen(
                [exe], cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, **_popen_kwargs())
            self._stream_to_completion(job, process, input_text)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
