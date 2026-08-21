# Compendium - compiled LaTeX summary notes from accessed material

Status: design. Nothing here is built yet.

## What it is

A fourth tab in the study workspace, beside Notes, Recall and Ask FRIDAY. You have a
material open in the drawer. You type what you want summarised - "the whole scheduling
lecture", "just the parts about deadlock", "everything I got wrong in the tutorial" -
and Compendium returns a typeset PDF: a structured summary of that material, with real
mathematics, cited back to the page it came from.

It is not a chat reply that happens to contain LaTeX. The output is a document: written
once, compiled locally, saved next to the material, and revisable.

## Why it belongs here rather than somewhere else

Most of the parts already exist and are load-bearing for something else:

* `drive.extract_learning_text` already pulls study text out of PDF, DOCX, PPTX and
  plain text, bounded at 40 000 characters.
* `materials.select` already knows how to pick a capped, readable, course-scoped set of
  files from disk or Drive, transcripts first.
* `ai_provider.complete` already fronts three backends with one signature.
* `ChatMemory` already holds what you asked FRIDAY about this material, and `Notebook`
  already holds what you wrote about it. Both are corpus.
* `notes.extract_cards` already turns `{{cloze}}` into scheduled recall cards, so a
  summary can seed the review queue instead of being a dead end.
* KaTeX is already vendored under `static/vendor/katex`, so the body can be previewed in
  the browser before anything is compiled.

What is genuinely new is small: a LaTeX preamble we own, a compile-and-repair loop, page
provenance, and one honest prompt.

## Research findings

The notes below describe an implementation investigation; local tool availability and timings vary by machine.

**A real LaTeX toolchain may be available.** The implementation should discover `pdflatex`, `xelatex`, `lualatex` or `latexmk` at runtime; `tectonic` and `pandoc` are optional. The feature should produce a PDF when a supported TeX distribution is available and degrade to `.tex` only when it is not.

**A representative document compiles headlessly in 3.5 seconds.** `article` with
`amsmath, amssymb, amsthm, geometry, hyperref, enumitem, booktabs`, inline and display
maths, an `align` environment and a `booktabs` table: exit 0, 108 KB PDF, no prompting.
That is the whole preamble the feature needs and it is cheap.

**Missing packages auto-install without prompting.** `tikz-cd` and `siunitx`, neither
previously installed, were fetched by the local TeX distribution mid-compile and the run still exited 0. Good
for robustness, but it means the *first* compile after a preamble change can be slow, and
it means the timeout has to be generous rather than tight.

**Malformed LaTeX fails cleanly and machine-readably.** A body with a mismatched
`\begin{align}...\end{aligned}` under `-interaction=nonstopmode -halt-on-error` exits 1,
produces no PDF, and writes errors on lines beginning `! `:

```
! LaTeX Error: \begin{align} on input line 4 ended by \end{aligned}.
!  ==> Fatal error occurred, no output PDF file produced!
```

Those lines are exactly what a repair turn needs. No hang, no interactive prompt.

**But pdflatex can still hang, so a timeout is mandatory.** Observed directly: a run
against a mangled input file sat until a 120-second timeout killed it. `nonstopmode`
covers errors *inside* a document it can read; it does not cover being unable to read one.
The compile must be run with a hard timeout and the process killed, never waited on.

**Unescaped `%` is the one failure the compiler will not catch.** `100% margin of error`
compiles happily and silently discards the rest of the line, because `%` opens a comment.
The same class of bug applies to `&`, `_`, `#` and `$` in prose. This is the most likely
way a summary quietly loses a sentence, and no exit code will report it. It needs a
pre-flight lint, not a compile check.

**Page provenance is currently destroyed.** `drive.extract_learning_text` joins
`page.extract_text()` across pages with `\n` (drive.py:268) and then collapses whitespace.
By the time the model sees the material there is no way to say which page a claim came
from. Citation is the core of the rigour requirement, so this has to be fixed first.

**The study surfaces do not currently agree on a model.** This is the finding that bears
directly on the consistency requirement, and it is worth stating precisely:

| Surface | Call site | Model actually used |
|---|---|---|
| Ask FRIDAY | `dashboard.py:1775` | none passed - CLI falls back to `haiku`, OmniRoute to `auto` |
| Research | `research.MODEL` | `claude-opus-5` |
| To-do neural query | `dashboard.py:803` | whatever the panel's dropdown says |

`ai_provider.complete` defaults `model` to `"haiku"` on the CLI backend (ai_provider.py:45)
and to `omniroute_provider.MODEL`, which is `"auto"`, on OmniRoute. Ask FRIDAY passes no
model, so today the assistant that reads your course material is answering on Haiku or on
a stochastic route, while Research is pinned to Opus. That inconsistency is the reason a
summary built on the current plumbing would not match the chat it sits beside.

## The consistency mechanism

**Superseded.** This section originally proposed a single house model shared by every AI
surface. Ask FRIDAY has since moved to free OmniRoute routes, which makes one shared model
impossible and, on inspection, undesirable - see
[ai-infrastructure.md](ai-infrastructure.md) for the measurements and the replacement.

The short version: surfaces are grouped into tiers, and Compendium sits in `SCHOLAR`,
which is pinned to `claude-opus-5` and **does not ladder**. If that model is unavailable,
generation fails loudly rather than quietly producing a revision note from whatever route
happened to be up.

```python
result = ai_provider.complete_tier("scholar", prompt, COMPENDIUM_SYSTEM, ...)
```

Consistency is therefore preserved exactly where it was actually needed - across summaries,
over the whole semester - rather than across surfaces that never needed to agree. A chat
turn answered by Haiku and a summary composed by Opus is the correct arrangement; what was
wrong before was that nobody had chosen it.

## Rigour: how a summary earns being trusted

These are study notes for exams. A plausible-sounding fabrication in a compiled PDF is
worse than no summary at all, because the typesetting lends it authority and because it
will be revised from for months. Four structural guards, in order of how much they buy:

**1. Page-anchored citation.** Add a page-aware extractor and keep the flat one on top of
it:

```python
def extract_learning_pages(path, limit=40000):
    """[(page_number, text)] for paginated formats; [(1, text)] for flat ones."""

def extract_learning_text(path, limit=40000):
    return "\n".join(text for _page, text in extract_learning_pages(path, limit))
```

PDFs paginate naturally. PPTX slides are already separate XML parts under `ppt/slides/`
and sort into slide order, so they paginate too. DOCX and plain text do not; they report
page 1 and the summary cites the file rather than a location, which is honest.

The material then reaches the prompt with `[[p.12]]` markers between pages, and every
non-trivial claim in the body carries `\srcref{Lecture 4}{12}`, rendered as a margin note.
A claim the model cannot anchor is a claim it should not be making.

**2. Verbatim quarantine.** Definitions, theorem statements and stated results must be
reproduced word-for-word inside a `sourcequote` environment. Paraphrase, intuition and
worked examples are allowed only outside it. The existing FRIDAY prompt already asks the
model to "distinguish what the material states from your own explanation"; this makes that
distinction structural and visible in the output rather than a matter of tone.

**3. A required gaps section.** The document ends with **Not covered by this material** -
everything the user's prompt asked for that the source does not support. An empty section
must be stated as empty, not omitted. This converts the most dangerous failure, quietly
filling a hole, into the most visible part of the document.

**4. No new numbers.** Constants, complexities, dates and figures must appear in the
source. The model may not derive a bound the material does not state, because a wrong
bound in a revision note is indistinguishable from a right one at 2am.

## The prompt contract

The model returns a body only, never a whole document:

```
% BEGIN BODY
...
% END BODY
```

The preamble is ours. That is a correctness decision before it is a security one - a
fixed preamble means the repair loop has a small surface, compiles are reproducible, and
a `\usepackage` the model invented cannot break a document that worked yesterday.

It is also a security decision. LaTeX is a programming language with filesystem reach, and
this body is untrusted text assembled from a Drive document. So:

* compile in a `TemporaryDirectory`, never in the download root;
* `-no-shell-escape`, and `openin_any=p` / `openout_any=p` in the environment;
* lint the body and reject `\input`, `\include`, `\write`, `\openout`, `\catcode`,
  `\def\endinput`, and anything matching `\\write18`;
* the same lint pass flags unescaped `%`, `&`, `_`, `#`, `$` outside maths mode, which is
  the silent-truncation bug above.

Rejection is not failure. A rejected body goes back to the model once with the offending
lines named, the same way a compile error does.

## The compile and repair loop

```
assemble body -> lint -> compile (timeout 120s)
   exit 0            -> PDF
   lint reject       -> one repair turn, offending lines named
   exit != 0         -> extract "! " lines plus two lines of context
                        -> one repair turn against the failing region only
                        -> recompile
   still failing     -> hand back the .tex and the log, do not pretend
```

At most two repair turns. The rule that matters: **never drop content to make it
compile.** A summary that compiles because the model deleted the theorem it could not
typeset is the worst possible outcome and the hardest to notice. If it will not compile,
the user gets the `.tex`, the log, and a plain statement of what failed.

## Corpus - what "accessed material" means

Three rings, narrowest first:

1. **The open material.** Always included, in full, page-marked. This is the default and
   for most requests it is the whole corpus.
2. **Your own traces on it.** The note you wrote in the Notes tab, and the FRIDAY
   conversation for this material from `ChatMemory`. These record what you personally
   found hard, which is what makes the summary yours rather than generic. Included by
   default; a checkbox turns it off.
3. **Sibling course material.** `materials.select(course, download_root, ledger)` under
   its existing caps - 12 files, 12 MB. Off by default, enabled by a "whole topic" scope,
   and it changes the shape of the job (see below).

Ring 3 needs map-reduce: summarise each file to a bounded set of anchored claims, then
compose the document from the claims. One pass over 12 files does not fit, and more
importantly a single pass over a whole course produces a summary of nothing in particular.

Token budget for ring 1 and 2: material caps at 40 000 characters, roughly 10 000 tokens.
Output needs `max_tokens` around 8 000 - a six-page summary with maths is long, and the
current chat value of 900 (dashboard.py:1777) is nowhere near it.

## Surfaces

**Interface.** A fourth tab, `Compendium`, in the existing `studyTabs` group. A prompt
box, a scope control (this material / this material and my notes / whole topic), a
Generate button, a live status line, and a result: KaTeX preview of the body plus a
`Download PDF` and `Open .tex`. Regenerating keeps the previous version rather than
overwriting it - revision notes get corrected, and losing a good one to a worse re-roll
is a real cost.

**API.** Generation is a 60-120 second job, unlike chat, so it cannot be a blocking POST.
It follows the pattern `do_unified_sync` already uses - worker thread plus polled state:

```
POST /api/study/summary          {id, prompt, scope, include_notes} -> {job}
GET  /api/study/summary/<job>    -> {state, stage, log, error, tex, ...}
GET  /api/study/summary/<job>/pdf -> application/pdf
```

`stage` should be honest and specific, because two minutes of "Working..." is worse than
two minutes of "Compiling (attempt 2 of 3)".

**Storage.** `<download_root>/<course>/summaries/<material>-<timestamp>.tex` and `.pdf`,
with a row in a new `summaries` table in the existing notes database: material id, prompt,
scope, model, backend, page anchors used, created_at. Recording the model and backend per
summary is what makes the consistency guarantee auditable rather than aspirational.

**Into Recall.** The generation prompt asks for key terms wrapped in `{{...}}` in a
plain-Markdown companion of the summary. Saving that companion through the existing
`Notebook.save` runs `extract_cards` unchanged, and the summary lands in the review queue
the same day. This is the highest-value integration in the whole design and it costs
almost nothing, because both ends already exist.

## Build order

Genuinely sequential - each step is testable before the next exists.

1. The tier ladder in `ai_provider` - see [ai-infrastructure.md](ai-infrastructure.md)
   steps 1-4. No UI, fixes a live inconsistency on its own, and everything else assumes it.
2. `extract_learning_pages()` in `drive.py`, with `extract_learning_text` rewritten as a
   join over it. Pure function, easy to test against synthetic inputs.
3. `latex.py` - preamble, lint, compile, repair loop. Fully testable with no model in the
   loop: feed it known-good and known-bad bodies.
4. `summary.py` - corpus assembly and the prompt. Also model-free to test, since
   `build_prompt` is a pure function in the style `research.build_prompt` already
   establishes.
5. The endpoint and the worker thread.
6. The Compendium tab.
7. Cloze seeding into Recall.

Steps 1-5 with a bare Generate button and no scope control is the smallest version that
is genuinely useful. Steps 6-7 are what make it get used.

## Defaults chosen, worth disagreeing with

* **Scope defaults to the open material plus your notes**, not the whole course. A
  narrow summary that is right beats a broad one that is thin.
* **The `.tex` stays local, the PDF is eligible for Drive push.** The source is working
  material; the PDF is the artefact.
* **Compendium is a tab, not a button inside Notes.** It produces a separate document
  with its own history, and burying that in the note editor would imply it edits the note.
* **No `latexmk`, single `pdflatex` pass.** Verified sufficient for this preamble. A
  second pass is only needed for cross-references and a table of contents, neither of
  which a six-page summary should have.
