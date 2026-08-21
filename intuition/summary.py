"""Compendium: compile one AI-composed, page-cited LaTeX summary from accessed
course material. See docs/compendium.md for the full design; this module is build
steps 3-4 from its build order - corpus assembly, the prompt, and the compile-and-
repair orchestration that ties ``ai_provider``, ``drive.extract_learning_pages``/
``drive.extract_learning_figures`` and ``latex`` together.

Three structural guards make a compiled summary trustworthy enough to revise from
for months rather than a plausible-sounding fabrication:

* **Page-anchored citation.** The material reaches the prompt with ``[[p.N]]``
  markers between pages; the model is required to cite ``\\srcref{name}{N}`` on
  every non-trivial claim.
* **Verbatim quarantine.** Definitions and stated results go inside
  ``sourcequote``, reproduced word-for-word; paraphrase and worked examples stay
  outside it.
* **A required, honest gaps section.** The document always ends with what the
  user's prompt asked for that the source does not support - stated as empty
  rather than omitted when there is nothing to report.

Never automatic: a run happens only when ``generate()`` is called with an explicit
prompt, the same way ``research.research()`` only ever runs on request.
"""
import os
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from intuition import ai_provider, drive, latex, materials

# Ring 1 (the open material) is bounded at the same 40 000 characters
# drive.extract_learning_text always has been - roughly 10 000 tokens, per
# docs/compendium.md's own budget. Ring 2 and ring 3 borrow from that budget rather
# than adding to it uncapped, so the total prompt stays predictable.
MATERIAL_CHAR_LIMIT = 40000
NOTE_CHAR_LIMIT = 6000
CHAT_TURNS_LIMIT = 12
CHAT_TURN_CHAR_LIMIT = 1000
SIBLING_FILE_CHAR_LIMIT = 4000
SIBLING_TOTAL_CHAR_LIMIT = 20000

# A six-page summary with maths and margin citations is long; the chat surfaces'
# 900-1200 token budgets are nowhere near enough for it. 8000 itself turned out to
# be too tight in practice - a single 24-page lecture deck reproducibly hit this
# ceiling (finish_reason "length"/"max_tokens") a few hundred tokens before the
# closing % END BODY marker, which _extract_body then reports as a confusing
# "model didn't follow the format" error rather than what it actually was: ran out
# of room. Doubled with headroom for documents at least this size; see
# _extract_body's finish-reason check below for what happens if it still isn't enough.
MAX_TOKENS = 16000

SCOPE_MATERIAL = "material"   # ring 1 only
SCOPE_NOTES = "notes"         # ring 1 + ring 2 (your note, your FRIDAY conversation)
SCOPE_TOPIC = "topic"         # ring 1 + ring 2 + ring 3 (sibling course material)
SCOPES = (SCOPE_MATERIAL, SCOPE_NOTES, SCOPE_TOPIC)

MAX_REPAIR_TURNS = 2

_BODY_RE = re.compile(
    r"%\s*BEGIN BODY\s*\n(.*?)\n%\s*END BODY", re.S)
_REPORT_RE = re.compile(
    r"%%\s*BEGIN REPORT\s*\n(.*?)\n%%\s*END REPORT", re.S)


class SummaryError(Exception):
    pass


COMPENDIUM_SYSTEM = """\
You are Compendium, composing one compiled LaTeX study summary for a Nanyang \
Technological University undergraduate, from material they have actually opened. \
This document will be revised from for months and used the night before an exam, so a \
plausible-sounding fabrication is worse than no summary at all. It is a revision \
document for a student who will not open the original material again - if a fact was \
examinable in the source, it must be examinable here. Two failure directions matter: \
loss (source content absent, weakened, or truncated) is the more common one; invention \
(content the source doesn't support, presented as fact) is the more damaging one.

You are given both the extracted text of every page AND a rendered image of every page, \
in that order. The image is authoritative for anything the text extraction cannot show - \
a diagram's own labels, an image-only slide, a callout box, colour or highlighting used \
as emphasis. Read every page image before writing. Never conclude that content is \
unavailable, or that the source "doesn't cover" something, on the basis of the extracted \
text alone - check the image first.

Output contract: reply with exactly two fenced blocks, in this order, nothing before, \
between, or after them beyond the markers themselves:

% BEGIN BODY
...your LaTeX here - never a preamble, \\documentclass, \\usepackage, or \
\\begin{document}/\\end{document}, just the body...
% END BODY

%% BEGIN REPORT
...your self-report here, plain text - see REPORT below...
%% END REPORT

Only these commands and environments are available in the BODY, because the preamble is \
fixed and not yours to change: standard sectioning (\\section, \\subsection, \
\\subsection*), itemize/enumerate/description (enumitem is loaded), amsmath/amssymb \
(equation, align, align*, gather, cases, pmatrix/bmatrix/vmatrix, \\[ \\] and \\( \\) \
inline maths), booktabs rules (\\toprule, \\midrule, \\bottomrule) inside tabularx (the \
X column type) or longtable for any table - never plain tabular or tabular*, which \
silently clip a long cell in the rendered PDF with no compile error, tikzpicture \
(arrows.meta, positioning, shapes.geometric and calc libraries are \
loaded - node/draw/path for flowcharts, state diagrams, trees and simple geometric \
figures) wrapped in a figure[htbp] of its own with a \\caption and a \\label{fig:<name>} \
you choose, lstlisting or verbatim for code/pseudocode (every character inside is \
typeset literally - never escape anything in one, and never paraphrase code into prose \
bullets instead), \\texttt{} for an inline identifier, call, type, or file path, and \
three commands built for this document specifically: \\srcref{Material \
Name}{page} for a margin citation, the sourcequote environment for a verbatim quotation, \
and \\srcfig{N}{caption} to place a real image extracted from the material - N is the \
figure number the prompt lists (never a page number), only numbers the prompt actually \
lists exist, and it auto-generates its own \\label{fig:N} you can \\ref. Every figure and \
table, real or redrawn, gets \\ref'd by name from the surrounding prose - a float nobody's \
prose points at has lost most of its value. For a document beyond roughly 4 pages or 3 \
sections, open the body with \\tableofcontents, mirroring the material's own outline \
slide's structure when it has one. Never write \\usepackage, \\documentclass, \\input, \
\\include, \\write, \\catcode, \\includegraphics, or plain tabular/tabular* - none would \
compile against the fixed preamble (or, for \\includegraphics, would compile against \
nothing you're allowed to name), and some are refused outright before compilation is \
even attempted.

Structural rules, all required:

1. PAGE-ANCHORED CITATION. The material is given to you with [[p.N]] markers between \
pages. Every non-trivial claim - a definition, a result, a number, a named technique - \
must carry \\srcref{<material name>}{<page>} immediately after it, naming the page the \
[[p.N]] marker put it under. A claim you cannot anchor to a page is a claim you should \
not be making.

2. VERBATIM QUARANTINE. A definition, theorem statement, or stated result must be \
reproduced word-for-word from the source inside a sourcequote environment. Paraphrase, \
intuition, and worked examples are allowed only outside sourcequote. This distinction \
must be structurally visible in the output, not a matter of tone.

3. HONEST GAPS, NEVER A NARRATED PIPELINE. End the document with \\subsection*{Not \
covered by this material} listing anything the user's request asked for that the source \
does not support - phrased as a scope note about the subject ("shared-memory IPC is \
named here as one of two models but its API is deferred to a later lecture"), never a \
reference to extraction, parsing, image availability, or the conversion process itself \
("the extracted text gives only...", "no extractable figure is available, so..."). The \
reader is a student, not the operator of this tool, and must never see it mentioned. If \
there is nothing to report, the section must still appear and say so explicitly (e.g. \
"Nothing requested was left uncovered.") - never omit it. Before writing that the source \
lacks something, verify against the rendered page image, not just the extracted text - a \
false absence claim actively tells the student not to look, which is worse than silence.

4. NO NEW NUMBERS, BUT MARK A LABELLED INFERENCE. A constant, complexity bound, date, or \
figure may appear only if it appears in the source. Never derive, estimate, or invent one \
the material does not state, even if it seems like an obvious calculation - a wrong \
number in a revision note is indistinguishable from a right one at 2am. The one exception: \
if the material poses a question and withholds the answer, answering it is valuable - but \
must be visibly marked as your own inference (e.g. "Inferred:"), never presented as the \
source's own words.

5. ESCAPE LITERAL SPECIAL CHARACTERS. A literal %, &, _, # or $ in prose (not used for \
its LaTeX purpose - a comment, a table column, a subscript, a macro parameter, a math \
delimiter) must be written \\%, \\&, \\_, \\# or \\$. An unescaped % silently discards \
the rest of its line with no compile error, which is the most common way a summary \
quietly loses a sentence.

6. SUCCINCT, NOT PADDED. This is read the night before an exam, under time pressure - a \
sentence that could be a phrase, or a paragraph that could be a bullet list, costs the \
reader time they don't have. Prefer itemize/enumerate/tables over prose paragraphs \
wherever the material is itself a list, a comparison, or a procedure. State a definition \
once, precisely; don't restate it in different words a paragraph later. Cut connective \
throat-clearing ("It is important to note that...", "As we can see..."); start each \
section on the substance. None of this licenses cutting content required by the other \
rules - citations, verbatim quotes, and the gaps section stay - it means every sentence \
that remains earns its place.

7. DIAGRAMS AND FIGURES, REDRAWN RATHER THAN DECLARED LOST. A tikzpicture or \\srcfig is \
a tool for a process, a structure, a state machine, or a spatial relationship that is \
genuinely faster to read as a picture than as a sentence - not decoration, and not one \
per section by default. A box-and-arrow diagram - a state machine, a layered \
architecture, a request flow - is cheap to redraw in TikZ and should be, copying the \
source's own labels exactly (including capitalisation) and preserving arrow direction and \
topology; reserve "not reproduced" for genuinely photographic content, and describe in \
prose what it shows even then. Every tikzpicture must be built only from relationships \
the material text or page image actually shows - never a plausible-looking diagram \
invented to fill space. Every \\srcfig{N}{...} caption must describe only what that page \
actually supports; never invent what a figure supposedly shows.

8. COMPOUND CONDITIONS AND COUNTS SURVIVE WHOLE. When the source states something is \
triggered by, caused by, or consists of "A or B" / "A and B", both members must appear in \
the output - dropping one narrows a true claim into a false one. When the source counts \
something ("two ways", "three models", "four regions"), the output must contain exactly \
that many items, the same items.

9. EMPHASIS IS SIGNAL, NOT DECORATION. Bold, colour, boxes, and callouts on a slide mark \
what the author considered important - carry that forward (bold, a highlighted note) \
rather than flattening it into ordinary prose, and never let emphasised content be what \
gets cut for space. Text repeated across slides is deliberate; keep the repetition or \
note it rather than silently deduplicating it away.

10. SYMMETRY ACROSS PARALLEL ITEMS. When the source treats several parallel items - \
models, algorithms, approaches, protocols - each with some combination of definition, \
advantage, disadvantage, and example spread across different slides, the output must \
cover the same cells for each item, or explicitly note which cells the source itself \
leaves empty. The common failure is capturing advantages for every item but disadvantages \
for only some, because they lived on different slides - build the comparison mentally \
before writing, then render it as one consolidated table. Consolidating a comparison the \
source left scattered across slides into one table is the single highest-value thing you \
can do to make this document better to revise from than the source itself.

11. RECONCILE AND FLAG DISCREPANCIES. If two parts of the source disagree - a figure \
omits an item an adjacent list includes, two slides give different counts, terminology \
drifts - do not silently pick one version. Present the fuller version and add a one-line \
footnote naming the discrepancy; this is exactly what a student would otherwise waste \
revision time discovering alone. Once you settle on a term for a concept, use the \
source's own term for it consistently throughout, even where the source itself uses \
synonyms interchangeably.

12. PRESERVE EXTERNAL REFERENCES COMPLETE. URLs, citations, further-reading lists, and \
named tools or libraries carry forward in full - dropping one item from a list because it \
was long or awkwardly formatted is a silent loss.

REPORT: after the body, a short plain-text self-report for a human reviewer - never for \
the student, and never referencing extraction/parsing register the way rule 3 forbids \
inside the body. Cover, tersely: pages processed vs. total pages in the material; figures \
reproduced, redrawn in TikZ, or omitted (with a one-line reason per omission); \
discrepancies found in the source and how each was handled; where any inferred answer \
appears. If a category is empty, say so in one line rather than omitting it.

If asked to repair a body that failed linting or compilation: fix only what is named as \
broken. Every claim, citation, and section from the previous attempt must still be \
present - deleting content to make a document compile is the worst possible outcome and \
the hardest for the reader to notice, so it is never an acceptable fix. A repair turn \
only needs the corrected % BEGIN BODY / % END BODY block, not a new REPORT block.\
"""


class Corpus(NamedTuple):
    material_name: str
    pages: List[Tuple[int, str]]              # ring 1
    note_text: str                              # ring 2a, "" if none/excluded
    chat_turns: List[Dict]                      # ring 2b, [] if none/excluded
    siblings: List[Tuple[str, List[Tuple[int, str]]]]  # ring 3, [(name, pages)]
    figures: List[Tuple[int, bytes, str]] = ()  # ring 1, [(page, data, ext)], real
                                                 # images pulled from the open material -
                                                 # () not [], a shared mutable default is
                                                 # a bug waiting to happen
    page_images: List[Tuple[int, bytes]] = ()   # ring 1, [(page, png_bytes)], a full
                                                 # render of every page - what the model
                                                 # actually sees, text extraction is blind
                                                 # to a diagram's own labels


def _page_marked(pages: List[Tuple[int, str]]) -> str:
    return "\n\n".join("[[p.{}]]\n{}".format(num, text) for num, text in pages)


def assemble_corpus(material_path: str, material_name: str, scope: str,
                    note_text: str = "", chat_turns: Optional[List[Dict]] = None,
                    course: str = "", download_root: str = ".",
                    ledger=None, drive_service=None) -> Corpus:
    """Ring 1 always; ring 2 under ``notes``/``topic`` scope; ring 3 under ``topic``.

    Ring 3 fetches (staging into, then clearing, a throwaway sandbox) up to
    ``materials.MAX_FILES`` sibling files for ``course`` and extracts each one's own
    page-marked text, same as the open material. This is a single pass over the
    sibling set rather than the two-stage map-reduce docs/compendium.md describes for
    a whole-topic summary - a documented simplification, not the full design: a
    single pass over many files risks a summary "of nothing in particular" once the
    sibling set is large, which per-file claim extraction exists to avoid.
    """
    if scope not in SCOPES:
        raise ValueError("Unknown scope {!r}, expected one of {}".format(scope, SCOPES))

    pages = drive.extract_learning_pages(material_path, limit=MATERIAL_CHAR_LIMIT)
    # Best-effort and PDF-only (see extract_learning_figures/rasterize_pages) - a
    # material with no extractable images, or one that isn't a PDF, just gets an
    # empty list here, same as ring 2/3 being empty when there is nothing to include.
    figures = drive.extract_learning_figures(material_path)
    # The model's other eye onto the open material - see rasterize_pages's docstring
    # for why text extraction alone misses a diagram's own labels. Ring 1 (the open
    # material) only: ring 3's sibling files are supplementary comparison context,
    # not the document under strict fidelity, so they stay text-only rather than
    # multiplying the vision cost across up to a dozen files per run.
    page_images = drive.rasterize_pages(material_path)

    if scope == SCOPE_MATERIAL:
        return Corpus(material_name, pages, "", [], [], figures, page_images)

    kept_notes = (note_text or "").strip()[:NOTE_CHAR_LIMIT]
    kept_turns = [
        {"role": t.get("role", "user"), "content": (t.get("content") or "")[:CHAT_TURN_CHAR_LIMIT]}
        for t in (chat_turns or [])[-CHAT_TURNS_LIMIT:]
    ]

    if scope == SCOPE_NOTES:
        return Corpus(material_name, pages, kept_notes, kept_turns, [], figures, page_images)

    siblings: List[Tuple[str, List[Tuple[int, str]]]] = []
    if course:
        specs = materials.select(course, download_root, ledger=ledger)
        sandbox = os.path.join(download_root, ".intuition", "compendium")
        staged = materials.stage(specs, sandbox, service=drive_service)
        try:
            used = 0
            for item in staged:
                if used >= SIBLING_TOTAL_CHAR_LIMIT:
                    break
                path = os.path.join(materials.stage_dir(sandbox), item["name"])
                try:
                    sib_pages = drive.extract_learning_pages(
                        path, limit=min(SIBLING_FILE_CHAR_LIMIT,
                                        SIBLING_TOTAL_CHAR_LIMIT - used))
                except drive.DriveError:
                    continue
                siblings.append((item["rel"], sib_pages))
                used += sum(len(text) for _n, text in sib_pages)
        finally:
            materials.clear(sandbox)

    return Corpus(material_name, pages, kept_notes, kept_turns, siblings, figures,
                 page_images)


def build_prompt(user_prompt: str, corpus: Corpus, scope: str) -> str:
    """The entire user turn, assembled in one readable, testable place."""
    parts = [
        "Material: {}".format(corpus.material_name),
        "",
        "--- MATERIAL TEXT (page-marked) ---",
        _page_marked(corpus.pages),
        "--- END MATERIAL TEXT ---",
    ]

    if corpus.page_images:
        parts += ["", "Attached below the text: a rendered image of every page in "
                      "this material, in page order. The image is authoritative for "
                      "anything the extracted text above can't show - a diagram's own "
                      "labels, an image-only slide, a callout box, colour or "
                      "highlighting used as emphasis. Read every page image before "
                      "writing; do not conclude content is unavailable on the basis "
                      "of the extracted text alone."]

    if corpus.figures:
        parts += ["", "Real images extracted from this material, available via "
                      "\\srcfig{{N}}{{caption}} (N is the number below, not the "
                      "page) - include one only where it actually clarifies a "
                      "point already anchored in the text above; skip any that "
                      "don't add real understanding rather than including all of "
                      "them:"]
        parts += ["Figure {} - page {}".format(i, page)
                  for i, (page, _data, _ext) in enumerate(corpus.figures, start=1)]
    else:
        parts += ["", "(No usable images were extracted from this material - draw "
                      "a tikzpicture diagram instead if one would clarify a "
                      "process, structure or relationship already in the text; "
                      "never fabricate a diagram of content the source doesn't "
                      "support.)"]

    if scope != SCOPE_MATERIAL:
        if corpus.note_text:
            parts += ["", "The student's own note on this material:", corpus.note_text]
        if corpus.chat_turns:
            parts += ["", "Their recent FRIDAY conversation about this material:"]
            parts += ["{}: {}".format(t["role"].title(), t["content"])
                      for t in corpus.chat_turns]
        if not corpus.note_text and not corpus.chat_turns:
            parts += ["", "(No note or FRIDAY conversation recorded for this material yet.)"]

    if scope == SCOPE_TOPIC:
        if corpus.siblings:
            parts += ["", "Related material from the same course (use only to support "
                          "claims already anchored in the main material above, or to "
                          "note what those files additionally cover if the request asks "
                          "for topic-wide coverage):"]
            for name, pages in corpus.siblings:
                parts += ["", "--- {} (page-marked) ---".format(name), _page_marked(pages)]
        else:
            parts += ["", "(No related course material was available to include.)"]

    parts += ["", "The student's request: {}".format((user_prompt or "").strip())]
    return "\n".join(parts)


# What the OpenAI-style (OmniRoute) and Anthropic-native completion paths each call
# the "the response was cut off by max_tokens, not because it was finished" signal.
_TRUNCATED_FINISH_REASONS = ("length", "max_tokens")


def _extract_body(text: str, finish_reason: Optional[str] = None) -> str:
    match = _BODY_RE.search(text or "")
    if not match:
        if finish_reason in _TRUNCATED_FINISH_REASONS:
            raise SummaryError(
                "The summary hit its {}-token length limit before finishing - try "
                "a narrower prompt or a smaller scope.".format(MAX_TOKENS))
        raise SummaryError(
            "The model's reply did not contain a % BEGIN BODY / % END BODY block.")
    return match.group(1).strip()


def _extract_report(text: str) -> str:
    """The self-report block (see COMPENDIUM_SYSTEM's REPORT section) - never fatal
    if absent, unlike _extract_body. A missing report just means the reviewer gets
    nothing extra; it must never block a summary that otherwise succeeded, and a
    repair turn is never asked to produce one at all (see _build_repair_prompt)."""
    match = _REPORT_RE.search(text or "")
    return match.group(1).strip() if match else ""


_WHITESPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([.,;:])")
_ELLIPSIS_RE = re.compile(r"\.\.\.")
_DOUBLE_SPACE_RE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")


def _clean_text(body: str) -> str:
    """Deterministic prose cleanup - the classic residue of stripped citation
    markers and pasted-together slide text (whitespace before punctuation, a
    literal "..." instead of \\ldots, doubled spaces). Cheaper and more reliable
    as a mechanical pass than as one more instruction competing for the model's
    attention, and unlike a lint violation it never costs a repair turn.

    Skips verbatim/lstlisting spans entirely (reusing latex's own raw-environment
    boundaries) since whitespace is semantically load-bearing in code - collapsing
    it there would corrupt indentation, not just tidy prose.
    """
    out: List[str] = []
    i, n = 0, len(body)
    while i < n:
        match = latex._RAW_BEGIN_RE.search(body, i)
        prose_end = match.start() if match else n
        prose = body[i:prose_end]
        prose = _WHITESPACE_BEFORE_PUNCT_RE.sub(r"\1", prose)
        prose = _ELLIPSIS_RE.sub(r"\\ldots{}", prose)
        prose = _DOUBLE_SPACE_RE.sub(" ", prose)
        out.append(prose)
        if not match:
            break
        end_marker = "\\end{{{}}}".format(match.group(1))
        end_idx = body.find(end_marker, match.end())
        span_end = end_idx + len(end_marker) if end_idx != -1 else n
        out.append(body[match.start():span_end])
        i = span_end
    return "".join(out)


def _build_repair_prompt(body: str, stage: str, errors: List[str]) -> str:
    kind = "failed the pre-compile lint" if stage == "lint" else "failed to compile"
    return "\n".join([
        "Your previous body {} and needs a fix, not a rewrite.".format(kind),
        "",
        "--- YOUR PREVIOUS BODY ---",
        body,
        "--- END YOUR PREVIOUS BODY ---",
        "",
        "What's wrong:",
        "\n".join("- {}".format(e) for e in errors),
        "",
        "Return the corrected body in the same % BEGIN BODY / % END BODY format. "
        "Fix only what's named above; every claim, citation and section that was "
        "correct must still be present.",
    ])


_SRCREF_RE = re.compile(r"\\srcref\{[^}]*\}\{(\d+)\}")


def _pages_cited(body: str) -> List[int]:
    """Page numbers actually cited via \\srcref in a finished body, sorted and
    deduplicated - what makes the rigour guarantee auditable rather than just
    asserted: a caller can check this against the pages the material actually had."""
    return sorted({int(n) for n in _SRCREF_RE.findall(body or "")})


class GenerateResult(NamedTuple):
    ok: bool
    tex: Optional[str]
    pdf: Optional[bytes]
    body: Optional[str]
    stage: str              # "" on success, else "generate" | "lint" | "compile"
    errors: List[str]
    backend: Optional[str]
    model: Optional[str]
    rung: Optional[str]
    pages_cited: List[int] = []
    report: str = ""         # self-report for a human reviewer, "" on failure or absence


def generate(material_path: str, material_name: str, user_prompt: str,
            download_root: str, scope: str = SCOPE_NOTES, note_text: str = "",
            chat_turns: Optional[List[Dict]] = None, course: str = "",
            ledger=None, drive_service=None, preferred_backend: Optional[str] = None,
            on_stage: Optional[Callable[[str], None]] = None) -> GenerateResult:
    """Corpus -> prompt -> SCHOLAR-tier completion -> lint -> compile, with up to
    ``MAX_REPAIR_TURNS`` repair turns spent across the lint and compile stages
    combined. ``on_stage`` is an optional callback fed a short human-readable stage
    name, for a caller that polls progress (the dashboard job endpoint).
    """
    def stage(name: str):
        if on_stage:
            on_stage(name)

    stage("Reading material")
    try:
        corpus = assemble_corpus(
            material_path, material_name, scope, note_text=note_text,
            chat_turns=chat_turns, course=course, download_root=download_root,
            ledger=ledger, drive_service=drive_service)
    except drive.DriveError as exc:
        return GenerateResult(False, None, None, None, "generate", [str(exc)],
                              None, None, None)

    # Position here is the figure number \srcfig{N}{...} in the model's output
    # resolves against - fixed once at corpus assembly, reused unchanged across
    # every repair/recompile below since the corpus itself never changes mid-run.
    figure_bytes = [data for _page, data, _ext in corpus.figures]

    prompt = build_prompt(user_prompt, corpus, scope)
    # Vision only on this first pass - a repair turn resends only the previous body
    # and what broke (see _build_repair_prompt), never the full corpus, so the added
    # image cost is paid once per generation, not once per repair.
    page_images = [data for _page, data in corpus.page_images]

    stage("Composing (Opus, scholar tier)")
    try:
        result = ai_provider.complete_tier(
            "scholar", prompt, COMPENDIUM_SYSTEM, preferred=preferred_backend,
            max_tokens=MAX_TOKENS, download_root=download_root, images=page_images)
    except ai_provider.ProviderError as exc:
        return GenerateResult(False, None, None, None, "generate", [str(exc)],
                              None, None, None)
    backend, model, rung = result.get("backend"), result.get("model"), result.get("rung")
    # From the composing call only - a repair turn is only ever asked for a
    # corrected body (see COMPENDIUM_SYSTEM's closing line and _build_repair_prompt),
    # so this is the one and only report a run will ever produce.
    report = _extract_report(result.get("text") or "")

    try:
        body = _clean_text(_extract_body(result["text"], result.get("finish_reason")))
    except SummaryError as exc:
        return GenerateResult(False, None, None, None, "generate", [str(exc)],
                              backend, model, rung)

    repairs_used = 0

    stage("Checking for unsafe or malformed LaTeX")
    violations = latex.lint(body)
    if violations:
        if repairs_used >= MAX_REPAIR_TURNS:
            return GenerateResult(False, latex.assemble(body), None, body, "lint",
                                  violations, backend, model, rung)
        stage("Repairing (lint)")
        repair_prompt = _build_repair_prompt(body, "lint", violations)
        try:
            repaired = ai_provider.complete_tier(
                "scholar", repair_prompt, COMPENDIUM_SYSTEM,
                preferred=preferred_backend, max_tokens=MAX_TOKENS,
                download_root=download_root)
        except ai_provider.ProviderError as exc:
            return GenerateResult(False, latex.assemble(body), None, body, "lint",
                                  violations + [str(exc)], backend, model, rung)
        repairs_used += 1
        backend, model, rung = (repaired.get("backend"), repaired.get("model"),
                                repaired.get("rung"))
        try:
            body = _clean_text(_extract_body(repaired["text"], repaired.get("finish_reason")))
        except SummaryError as exc:
            return GenerateResult(False, None, None, None, "lint",
                                  violations + [str(exc)], backend, model, rung)
        violations = latex.lint(body)
        if violations:
            return GenerateResult(False, latex.assemble(body), None, body, "lint",
                                  violations, backend, model, rung)

    stage("Compiling")
    document = latex.assemble(body)
    compiled = latex.compile(document, figures=figure_bytes)
    if not compiled.ok:
        if repairs_used >= MAX_REPAIR_TURNS:
            return GenerateResult(False, document, None, body, "compile",
                                  compiled.errors, backend, model, rung)
        stage("Repairing (compile)")
        repair_prompt = _build_repair_prompt(body, "compile", compiled.errors)
        try:
            repaired = ai_provider.complete_tier(
                "scholar", repair_prompt, COMPENDIUM_SYSTEM,
                preferred=preferred_backend, max_tokens=MAX_TOKENS,
                download_root=download_root)
        except ai_provider.ProviderError as exc:
            return GenerateResult(False, document, None, body, "compile",
                                  compiled.errors + [str(exc)], backend, model, rung)
        repairs_used += 1
        backend, model, rung = (repaired.get("backend"), repaired.get("model"),
                                repaired.get("rung"))
        try:
            body = _clean_text(_extract_body(repaired["text"], repaired.get("finish_reason")))
        except SummaryError as exc:
            return GenerateResult(False, document, None, body, "compile",
                                  compiled.errors + [str(exc)], backend, model, rung)
        violations = latex.lint(body)
        if violations:
            return GenerateResult(False, latex.assemble(body), None, body, "lint",
                                  violations, backend, model, rung)
        stage("Recompiling")
        document = latex.assemble(body)
        compiled = latex.compile(document, figures=figure_bytes)
        if not compiled.ok:
            return GenerateResult(False, document, None, body, "compile",
                                  compiled.errors, backend, model, rung)

    return GenerateResult(True, document, compiled.pdf, body, "", [],
                          backend, model, rung, _pages_cited(body), report)
