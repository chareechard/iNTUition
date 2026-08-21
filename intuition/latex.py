"""Compile, lint and repair one LaTeX document body for Compendium.

The preamble is ours, never the model's - see the module docstring in ``summary.py``
for why. This module only ever sees a *body*: the fragment that goes between
``\\begin{document}`` and ``\\end{document}``. It has three jobs, each independently
testable with no model in the loop:

* ``lint`` - reject a body containing filesystem/shell-escape commands, or prose that
  looks like it has an unescaped LaTeX special character, before it ever reaches a
  compiler.
* ``assemble`` - wrap a linted body in the fixed preamble.
* ``compile`` - run pdflatex against the assembled document in a throwaway temp
  directory, under a hard timeout, with shell-escape and arbitrary file I/O disabled.

None of this calls an AI model. The retry-with-the-model-told-what-broke loop lives in
``summary.py``, which is the only caller that has a prompt to retry with.
"""
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, NamedTuple, Optional

COMPILE_TIMEOUT = 120

# A representative local toolchain test: article + these packages compiles a document with
# inline/display maths, an align environment, a tabularx table, a TikZ diagram and
# an embedded figure in under 5s, no prompting.
# \srcref is the margin citation every non-trivial claim carries; sourcequote is the
# verbatim-quarantine environment for definitions and stated results; \srcfig is the
# only way a body can place a real embedded image (see compile()'s figures= param) -
# \includegraphics itself is lint-forbidden so a document can never reach outside the
# fixed, pre-staged fig1/fig2/... set this module puts in the working directory.
# tabularx/longtable replace plain tabular: a fixed-width tabular column silently clips
# a long cell in the rendered PDF with no compile error, so lint() below forbids it
# outright rather than merely discouraging it (see _FORBIDDEN_TABLE_ENVIRONMENTS).
# float is loaded for [H] - a body's own escape hatch when a figure/table truly must
# not drift from its reference point.
PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath, amssymb, amsthm}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{tabularx}
\usepackage{longtable}
\usepackage{booktabs}
\usepackage{float}
\usepackage{marginnote}
\usepackage{graphicx}
\usepackage{tikz}
\usetikzlibrary{arrows.meta, positioning, shapes.geometric, calc}
\usepackage{listings}
\lstset{basicstyle=\ttfamily\footnotesize, breaklines=true, columns=fullflexible,
       frame=single}

\graphicspath{{./}}
\DeclareGraphicsExtensions{.png,.jpg,.jpeg}

\newcommand{\srcref}[2]{\marginnote{\footnotesize #1, p.#2}}
\newenvironment{sourcequote}
  {\begin{quote}\itshape}
  {\end{quote}}
% [htbp], not [h]: a float pinned to exactly "here" is the one placement pdflatex is
% most likely to have to violate, silently pushing the figure and its caption apart
% from wherever the body's own \ref points. \label{fig:N} is generated here, once,
% rather than asked of the model, so a body can \ref{fig:N} without ever managing the
% label string itself.
\newcommand{\srcfig}[2]{\begin{figure}[htbp]\centering
  \includegraphics[width=0.8\linewidth]{fig#1}
  \caption{\footnotesize #2}
  \label{fig:#1}
  \end{figure}}

\setlength{\parindent}{0pt}
\setlength{\parskip}{6pt}
"""

# Plain concatenation, not %-style templating: PREAMBLE is LaTeX source, and LaTeX's
# own comment character is %, so a %-format string here is one \newcommand{...}{%
# line-continuation idiom away from breaking on its own preamble.
_DOCUMENT_HEAD = PREAMBLE + "\n\\begin{document}\n"
_DOCUMENT_TAIL = "\n\\end{document}\n"

# ── Security lint: filesystem/shell reach ───────────────────────────────────────────
# A generated body is untrusted text assembled from a Drive document. LaTeX is a
# programming language with filesystem reach, so these are rejected outright rather
# than merely discouraged - the fixed preamble already supplies everything a summary
# legitimately needs.
_FORBIDDEN_COMMANDS = (
    r"\input", r"\include", r"\write", r"\openout", r"\openin", r"\catcode",
    r"\def\endinput", r"\csname", r"\immediate",
    # \includegraphics reaches an arbitrary filename; \srcfig{N}{caption} is the only
    # way a body may place an image, and it only ever resolves to one of this
    # module's own pre-staged fig1/fig2/... files (see compile()'s figures= param).
    r"\includegraphics",
)
_SHELL_ESCAPE_RE = re.compile(r"\\write\s*18\b")

# ── Prose lint: the silent-truncation class of bug ──────────────────────────────────
# '%' opens a LaTeX comment and silently discards the rest of the line - the compiler
# reports no error, so this is the one failure a compile check cannot catch. '&', '_'
# and '#' have the same class of problem when they slip into prose unescaped: valid
# inside their own construct (a tabular column separator, a subscript, a macro
# parameter), a LaTeX error anywhere else. An unescaped one is either flagged here or,
# for '&'/'_'/'#', usually caught by the compiler anyway - but a lint that names the
# line beats a compile failure that names only the line pdflatex gave up on.
_MATH_ENV_NAMES = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*",
    "math", "displaymath",
}
_TABULAR_ENV_NAMES = {
    "tabular", "tabular*", "tabularx", "longtable", "array", "matrix", "pmatrix",
    "bmatrix", "vmatrix", "cases", "split", "aligned",
}
_ENV_RE = re.compile(r"\\(begin|end)\{([a-zA-Z*]+)\}")

# tabular/tabular* silently clip a long cell in the rendered PDF with no compile
# error - forbidden outright (see PREAMBLE's comment) rather than merely discouraged,
# the same posture as _FORBIDDEN_COMMANDS above. tabularx/longtable are the model's
# only way to build a table, and are deliberately absent from this set.
_FORBIDDEN_TABLE_ENV_RE = re.compile(r"\\begin\{(tabular\*?)\}")

# verbatim/lstlisting are LaTeX's literal-text environments: every character inside,
# including %, &, _, #, $ and \ itself, is typeset as-is rather than interpreted - the
# whole reason code/pseudocode belongs in one (see COMPENDIUM_SYSTEM). The scanner
# below otherwise treats \ as always introducing a command, so a raw span has to be
# skipped wholesale rather than character-by-character or every real escape sequence
# a code sample contains would misfire as a lint violation or a mangled environment
# count.
_RAW_BEGIN_RE = re.compile(r"\\begin\{(verbatim|lstlisting)\}(\[[^\]]*\])?")


def lint(body: str) -> List[str]:
    """Violations found in ``body``, as human-readable ``"line N: ..."`` strings.

    An empty list means the body is safe to assemble and compile. Every message
    names a 1-indexed line number, because the caller's repair turn quotes it back to
    the model verbatim.

    A single forward pass over the whole body, not a per-line scan: inline math
    (``$...$``, ``\\(...\\)``) very often opens and closes on the same line, so
    "am I in math mode" has to be tracked character-by-character rather than reset
    at each newline.
    """
    violations: List[str] = []
    if _SHELL_ESCAPE_RE.search(body):
        violations.append("shell-escape (\\write18) is never permitted")
    for command in _FORBIDDEN_COMMANDS:
        idx = body.find(command)
        if idx != -1:
            line_no = body.count("\n", 0, idx) + 1
            violations.append(
                "line {}: {} is not permitted (filesystem/shell reach)".format(
                    line_no, command))
    for match in _FORBIDDEN_TABLE_ENV_RE.finditer(body):
        line_no = body.count("\n", 0, match.start()) + 1
        violations.append(
            "line {}: \\begin{{{}}} silently clips long cells in the rendered PDF - "
            "use tabularx (X columns) or longtable instead".format(
                line_no, match.group(1)))

    inline_math = False   # toggled by $ and \( \)
    env_math_depth = 0    # nesting of display-math environments and \[ \]
    tabular_depth = 0     # nesting of tabular-like environments
    dollar_count = 0
    last_dollar_line = 0
    line_no = 1
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\n":
            line_no += 1
            i += 1
            continue
        raw_match = _RAW_BEGIN_RE.match(body, i)
        if raw_match:
            end_marker = "\\end{{{}}}".format(raw_match.group(1))
            end_idx = body.find(end_marker, raw_match.end())
            span_end = end_idx + len(end_marker) if end_idx != -1 else n
            line_no += body.count("\n", raw_match.start(), span_end)
            i = span_end
            continue
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt in "([":
                env_math_depth += 1
                i += 2
                continue
            if nxt in ")]":
                env_math_depth = max(0, env_math_depth - 1)
                i += 2
                continue
            if nxt in "%&_#$":
                # An escaped special character (\%, \&, ...) is exactly the fix
                # this lint asks for elsewhere, so it is never itself a violation.
                i += 2
                continue
            env_match = _ENV_RE.match(body, i)
            if env_match:
                kind, name = env_match.group(1), env_match.group(2)
                if name in _MATH_ENV_NAMES:
                    env_math_depth += 1 if kind == "begin" else -1
                    env_math_depth = max(0, env_math_depth)
                if name in _TABULAR_ENV_NAMES:
                    tabular_depth += 1 if kind == "begin" else -1
                    tabular_depth = max(0, tabular_depth)
                i = env_match.end()
                continue
            i += 2  # any other command: skip the backslash and its next char
            continue

        in_math = inline_math or env_math_depth > 0
        if ch == "$":
            inline_math = not inline_math
            dollar_count += 1
            last_dollar_line = line_no
        elif ch == "%":
            violations.append(
                "line {}: unescaped % opens a comment and silently discards the "
                "rest of the line - use \\% for a literal percent".format(line_no))
        elif ch == "&" and tabular_depth == 0 and not in_math:
            violations.append(
                "line {}: unescaped & outside a tabular/matrix environment - "
                "use \\& for a literal ampersand".format(line_no))
        elif ch in "_#" and not in_math:
            name = "underscore" if ch == "_" else "hash"
            violations.append(
                "line {}: unescaped {} outside math mode - use \\{} for a "
                "literal {}".format(line_no, name, ch, name))
        i += 1

    if dollar_count % 2 == 1:
        violations.append(
            "line {}: an odd number of unescaped $ - inline math looks unclosed, "
            "or a literal dollar sign needs \\$".format(last_dollar_line))

    return violations


def assemble(body: str) -> str:
    """Wrap an already-linted body in the fixed preamble."""
    return _DOCUMENT_HEAD + body + _DOCUMENT_TAIL


def tooling_status() -> Dict:
    path = shutil.which("pdflatex")
    return {"pdflatex": bool(path), "ready": bool(path)}


class CompileResult(NamedTuple):
    ok: bool
    pdf: Optional[bytes]
    tex: str
    log: str
    errors: List[str]


def extract_errors(log: str, context_lines: int = 2) -> List[str]:
    """``"! "``-prefixed error lines from a pdflatex run, each with a couple of lines
    of context - exactly what a repair turn needs to fix the failing region without
    resending the whole log."""
    lines = log.split("\n")
    out: List[str] = []
    for i, line in enumerate(lines):
        if line.startswith("! "):
            block = lines[i:i + 1 + context_lines]
            out.append("\n".join(block).strip())
    return out


def compile(document: str, figures: Optional[List[bytes]] = None,
           timeout: float = COMPILE_TIMEOUT) -> CompileResult:
    """Compile a full ``.tex`` document (preamble and all) to PDF.

    Runs in a throwaway ``TemporaryDirectory``, never the download root. Shell-escape
    is disabled at the binary flag (``-no-shell-escape``) and again at the environment
    level (``openin_any``/``openout_any`` restricted to the working directory), so a
    command the lint pass missed still cannot read or write outside the sandbox.
    Never waits past ``timeout``: a malformed input pdflatex cannot even parse hangs
    rather than erroring, so the process is killed rather than joined.

    ``figures`` are raw image bytes (PNG or JPEG), written into the same sandbox as
    ``fig1.png``/``fig1.jpg``, ``fig2...`` before pdflatex runs - the fixed, numbered
    set the body's own ``\\srcfig{N}{caption}`` calls resolve against. Position in the
    list is the figure number; an unrecognised format is skipped rather than failing
    the whole compile, since a caller that over-requested a figure just gets a missing
    image on that page, not a lost summary.
    """
    status = tooling_status()
    if not status["ready"]:
        return CompileResult(
            ok=False, pdf=None, tex=document, log="",
            errors=["pdflatex not found on PATH. Install a LaTeX distribution "
                    "(e.g. MiKTeX or TeX Live) to compile summaries to PDF."])

    with tempfile.TemporaryDirectory(prefix="intuition_latex_") as tmp:
        source = os.path.join(tmp, "summary.tex")
        with open(source, "w", encoding="utf-8") as f:
            f.write(document)

        for index, data in enumerate(figures or [], start=1):
            ext = ("png" if data.startswith(b"\x89PNG\r\n\x1a\n")
                   else "jpg" if data.startswith(b"\xff\xd8\xff") else None)
            if not ext:
                continue
            with open(os.path.join(tmp, "fig{}.{}".format(index, ext)), "wb") as f:
                f.write(data)

        env = dict(os.environ)
        env["openin_any"] = "p"
        env["openout_any"] = "p"
        cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
              "-no-shell-escape", "-output-directory", tmp, source]

        # Two passes, unconditionally: \tableofcontents and \ref/\label (both now
        # part of the fixed preamble/macros - see PREAMBLE) only resolve on a second
        # pass, standard LaTeX behaviour. A first-pass failure is reported without
        # spending a second attempt on a document that will not compile anyway; a
        # single ~5s compile (see this module's own comment above) doubling to ~10s
        # is a cheap price for a TOC/reference that isn't "??" on delivery.
        try:
            proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or "") + (exc.stderr or "")
            return CompileResult(
                ok=False, pdf=None, tex=document, log=log,
                errors=["Compile timed out after {}s.".format(timeout)])

        log = (proc.stdout or "") + (proc.stderr or "")
        pdf_path = os.path.join(tmp, "summary.pdf")
        if proc.returncode == 0 and os.path.isfile(pdf_path):
            try:
                proc2 = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                                       text=True, timeout=timeout)
                log += (proc2.stdout or "") + (proc2.stderr or "")
                # The second pass is authoritative once it runs to completion - even
                # a failure, since that's a real regression on the resolving pass,
                # not something to paper over with the first pass's stale success.
                proc = proc2
            except subprocess.TimeoutExpired as exc:
                # The first pass already produced a usable PDF - references may read
                # "??" but a document that compiled once is still better delivered
                # than dropped over a second pass timing out.
                log += (exc.stdout or "") + (exc.stderr or "")

        if proc.returncode == 0 and os.path.isfile(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            return CompileResult(ok=True, pdf=pdf_bytes, tex=document, log=log, errors=[])

        errors = extract_errors(log) or ["pdflatex exited {} with no '! ' error line "
                                         "found in its output.".format(proc.returncode)]
        return CompileResult(ok=False, pdf=None, tex=document, log=log, errors=errors)
