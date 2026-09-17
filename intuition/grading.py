"""Grades a student's worked tutorial against the professor's own solution.

Sits directly on top of two things this app already has: solution_pairs (to find
which Drive solution document actually belongs beside the tutorial the student
had open) and ai_provider's ``scholar`` tier (Claude Opus, pinned, no ladder - see
docs/ai-infrastructure.md) so a grade always carries the same academic rigour
Compendium already demands of that tier for compiled summaries.

Text extraction reuses drive.extract_learning_text - the same PDF/DOCX/PPTX
reader every other study feature already relies on. A scanned photo of
handwritten work is sent to the model as an image instead, riding the same
vision path Ask FRIDAY's Lasso snapshot already proves out. A scanned *PDF*
with no text layer is the one gap: this module has no PDF-to-image renderer, so
that case fails with a clear, actionable error rather than silently grading
nothing.
"""
import os
from typing import NamedTuple, Optional, Tuple

from intuition import ai_provider, drive

GRADING_SYSTEM = (
    "You are FRIDAY's grading mode, an experienced, rigorous university grader. "
    "You are given the professor's own solution to a tutorial or exam question and "
    "a student's submitted working. Compare the student's working against the "
    "solution method by method: check whether the final answers match and whether "
    "each step is logically or mathematically valid. A different method from the "
    "professor's that is still valid deserves full credit - do not penalise a "
    "correct alternative approach for not matching the solution's path. Where the "
    "student went wrong, name the specific step or concept rather than saying "
    "'incorrect'. For each question or part give a clear verdict (Correct / "
    "Partially correct / Incorrect) with a one-line reason, then a short overall "
    "assessment. Be specific and honest, not generically encouraging - a right "
    "answer reached by invalid reasoning is not full credit - but do note genuine "
    "strengths. Never invent a numeric score out of an unstated rubric total; "
    "describe correctness qualitatively per question unless the material itself "
    "states point values. Output focused Markdown: a per-question breakdown, then "
    "an overall summary. Write inline mathematics as \\( ... \\) and display "
    "mathematics as \\[ ... \\]; never use dollar-sign delimiters."
)

MIN_TEXT_CHARS = 30
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


class GradingError(RuntimeError):
    pass


class GradeResult(NamedTuple):
    ok: bool
    feedback: str
    backend: str
    model: str
    rung: str
    error: str


def extract_work_content(work_path: str) -> Tuple[Optional[str], Optional[bytes]]:
    """``(text, None)`` for a document with a real text layer, ``(None, bytes)``
    for an image sent to the grader as vision input. Exactly one is populated.
    """
    extension = os.path.splitext(work_path)[1].lower()
    if extension in IMAGE_EXTENSIONS:
        with open(work_path, "rb") as f:
            return None, f.read()
    try:
        text = drive.extract_learning_text(work_path)
    except drive.DriveError:
        # A PDF with no text layer at all (the common scanned-tutorial case)
        # fails inside extract_learning_text before this function's own
        # MIN_TEXT_CHARS check ever runs - re-raise with the actionable
        # PNG/JPEG alternative rather than drive.py's generic message.
        text = ""
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise GradingError(
            "Could not find readable text in the uploaded file. If this is a "
            "scanned PDF with no text layer, upload a clear photo of each page "
            "instead (PNG or JPEG) - a text-based PDF/DOCX export also works.")
    return text, None


def build_grading_prompt(material_name: str, practice_text: Optional[str],
                         solution_text: str, solution_name: str,
                         work_text: Optional[str]) -> str:
    sections = ["Material: {}".format(material_name)]
    if practice_text:
        sections.append("--- Tutorial questions ---\n{}".format(practice_text[:20000]))
    sections.append(
        "--- Professor's solution, {} (ground truth for grading; do not simply "
        "paste it back to the student) ---\n{}".format(solution_name, solution_text[:20000]))
    if work_text is not None:
        sections.append("--- Student's submitted working ---\n{}".format(work_text[:20000]))
    else:
        sections.append(
            "--- Student's submitted working ---\n(attached as an image - read it directly)")
    sections.append("Grade the student's submitted working against the professor's solution.")
    return "\n\n".join(sections)


def grade(work_path: str, solution_text: str, solution_name: str,
         practice_text: Optional[str], material_name: str,
         preferred_backend: Optional[str] = None, download_root: str = ".") -> GradeResult:
    try:
        work_text, work_image = extract_work_content(work_path)
    except GradingError as exc:
        return GradeResult(ok=False, feedback="", backend="", model="", rung="",
                           error=str(exc))
    prompt = build_grading_prompt(
        material_name, practice_text, solution_text, solution_name, work_text)
    try:
        result = ai_provider.complete_tier(
            "scholar", prompt, GRADING_SYSTEM, preferred=preferred_backend,
            max_tokens=1800, download_root=download_root,
            images=[work_image] if work_image is not None else None)
    except ai_provider.ProviderError as exc:
        return GradeResult(ok=False, feedback="", backend="", model="", rung="",
                           error=str(exc))
    return GradeResult(ok=True, feedback=result.get("text") or "",
                       backend=result.get("backend") or "",
                       model=result.get("model") or "", rung=result.get("rung") or "",
                       error="")
