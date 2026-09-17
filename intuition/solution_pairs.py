"""Pairs practice material (tutorials, past-year midterms/finals) with the
professor's solution documents that live beside them in Drive, and flags newly
discovered solutions so they can be tagged automatically.

Detection is filename-only and mirrors ``transcribe.classify_drive_media``'s
approach: this never talks to Drive on its own, it only classifies whatever flat
file listing (``drive.DriveMirror.list_files()`` shape - id, name, rel_path,
mime_type, size, modified) the caller already has cached. That keeps it free to
call on every state snapshot and every Drive listing refresh, so a professor
posting a new tutorial-and-solution pair is picked up the next time the
dashboard's own "Index" action already runs - no separate poll loop needed.
"""
import os
import re
from typing import Dict, List, Optional

# "Model answer(s)" is deliberately spelled out rather than bare "model" - a bare
# "model" keyword matched "...ObjectOrientedModelling..." during the manual pass
# that preceded this module and produced a false positive.
#
# `\b` alone is not enough to bound the keyword: these filenames separate words
# with underscores ("Tut01_solutions.pdf"), and "_" counts as a word character,
# so "1_s" has no \b between them. The (?<![A-Za-z]) / (?![A-Za-z]) lookarounds
# bound on *letters* specifically, so an underscore or hyphen separator still
# counts as a break.
# The keyword itself may be followed by a revision qualifier ("Solutions
# Updated.pdf") that has to be stripped along with it - otherwise "Solutions
# Updated" and "Questions" reduce to different bases and a real pair is
# reported as unmatched.
_REVISION_TAIL = r"(?:\s*(?:updated|revised|corrected|final|latest))?"
SOLUTION_RE = re.compile(
    r"[-_\s]*(?<![A-Za-z])(solution\s*key|solutions?|answer\s*key|answers?|"
    r"model\s*answers?|marking\s*scheme|mark\s*scheme)" + _REVISION_TAIL +
    r"(?![A-Za-z])", re.I)
HINT_RE = re.compile(r"[-_\s]*(?<![A-Za-z])hints?(?![A-Za-z])", re.I)
# Deliberately just "question(s)", not "problem(s)": "Problem Set" is part of
# the tutorial's own title in this course (e.g. MH2100's "Tutorial Problem Set
# 1"), not a marker that a document is the question paper - stripping it broke
# an otherwise-exact match against that module's solution key.
QUESTION_RE = re.compile(r"[-_\s]*(?<![A-Za-z])questions?(?![A-Za-z])", re.I)
TUTORIAL_FOLDER_RE = re.compile(r"tutorial", re.I)


def _name_of(f: Dict) -> str:
    return f.get("name") or os.path.basename(f.get("rel_path") or "")


def is_tutorial_material(rel_path: str) -> bool:
    """Whether ``rel_path`` sits under a folder Blackboard itself titled with
    "tutorial" - "Tutorials", "Tutorial Problem Sets/Tutorial Problem Set 1
    (Teaching Week 2)", etc. Deliberately checks the *directory* portion only,
    not the filename: grading is scoped to weekly tutorial practice, not past-
    year midterm/final exam papers, which live in differently-named folders
    even though they also get a solution_pairs match.
    """
    directory = os.path.dirname(rel_path or "")
    return bool(TUTORIAL_FOLDER_RE.search(directory))


def classify_material(name: str) -> str:
    """'solution', 'hint', or 'practice'.

    A hint is a partial nudge, not an answer key, and must never be treated as
    a solution - MH2500's "Tutorial 01_2026-hints.pdf" (a hints file with no
    accompanying solution key anywhere in the module) is exactly the case this
    distinction exists to keep separate.
    """
    stem = os.path.splitext(name or "")[0]
    if SOLUTION_RE.search(stem):
        return "solution"
    if HINT_RE.search(stem):
        return "hint"
    return "practice"


def _base(name: str, marker: "re.Pattern") -> str:
    stem = os.path.splitext(name or "")[0]
    stripped = marker.sub(" ", stem)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _ref(f: Optional[Dict]) -> Optional[Dict]:
    if not f:
        return None
    name = _name_of(f)
    return {"id": f["id"], "name": name, "rel_path": f.get("rel_path") or name}


def pair_practice_with_solutions(files: List[Dict]) -> List[Dict]:
    """One entry per solution document in ``files``, matched against its practice
    (and, if present, hint) sibling in the same Drive folder.

    Matching is base-name equality after stripping the solution/question/hint
    marker - ``"AY 1516 - Solutions.pdf"`` pairs with ``"AY 1516 - Questions.pdf"``
    because both reduce to ``"ay 1516"`` - falling back to a prefix match so a
    numbered variant like ``"Tut01_1.pdf"`` still lines up with
    ``"Tut01_solutions.pdf"`` when no exact-base practice file exists.
    """
    by_dir: Dict[str, List[Dict]] = {}
    for f in files:
        directory = os.path.dirname(f.get("rel_path") or _name_of(f))
        by_dir.setdefault(directory, []).append(f)

    out: List[Dict] = []
    for directory, siblings in by_dir.items():
        solutions = [f for f in siblings if classify_material(_name_of(f)) == "solution"]
        if not solutions:
            continue
        solution_ids = {f["id"] for f in solutions}
        others = [f for f in siblings if f["id"] not in solution_ids]
        for sol in solutions:
            sol_base = _base(_name_of(sol), SOLUTION_RE)
            practice = None
            hint = None
            best_prefix = None
            for cand in others:
                cand_name = _name_of(cand)
                kind = classify_material(cand_name)
                cand_base = _base(cand_name, QUESTION_RE if kind != "hint" else HINT_RE)
                if cand_base == sol_base:
                    if kind == "hint":
                        hint = cand
                    else:
                        practice = cand
                elif (kind == "practice" and best_prefix is None
                      and cand_base.startswith(sol_base) and cand_base != sol_base):
                    best_prefix = cand
            if practice is None:
                practice = best_prefix
            out.append({
                "solution": _ref(sol),
                "practice": _ref(practice),
                "hint": _ref(hint),
                "rel_path": directory,
                "match": "exact" if practice and _base(_name_of(practice), QUESTION_RE) == sol_base
                         else ("prefix" if practice else "none"),
            })
    return sorted(out, key=lambda e: (e["rel_path"].lower(), e["solution"]["name"].lower()))


def new_solutions(files: List[Dict], notebook) -> List[Dict]:
    """Solution documents in ``files`` that have never been noted before - i.e. no
    note row exists for their id yet.

    Safe to call on every listing refresh: a document that already has *any*
    note (the SOL tag from an earlier pass, or the student's own writing) is
    left untouched, so this only ever fills the gap for material that just
    appeared in Drive.
    """
    fresh = []
    for f in files:
        if classify_material(_name_of(f)) != "solution":
            continue
        if notebook.get(f["id"]) is None:
            fresh.append(f)
    return fresh
