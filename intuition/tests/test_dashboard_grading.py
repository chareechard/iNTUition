import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard, drive, grading
from intuition.notes import Notebook


def _state(tmp_path, drive_files):
    return SimpleNamespace(
        notebook=Notebook(str(tmp_path)),
        lock=threading.Lock(),
        note=lambda _message: None,
        drive_files=drive_files,
        grading=True,
        grading_job={"id": "job-1", "document_id": "prac-1", "material_name": "Tut01.pdf",
                    "work_filename": "my_answers.txt", "stage": "Queued",
                    "ok": None, "done": False},
        research_backend=None,
        download_root=str(tmp_path),
    )


def _work_file(tmp_path, name="my_answers.txt", content=b"my worked answer text"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(content)
    return path


PAIR_FILES = [
    {"id": "prac-1", "name": "Tut01.pdf", "rel_path": "MH1300/Tutorials/Tut01.pdf",
     "mime_type": "application/pdf", "modified": ""},
    {"id": "sol-1", "name": "Tut01_solutions.pdf",
     "rel_path": "MH1300/Tutorials/Tut01_solutions.pdf",
     "mime_type": "application/pdf", "modified": ""},
]

TEXT_BY_ID = {"prac-1": "Q1: differentiate x^2", "sol-1": "d/dx x^2 = 2x"}


def _fake_pull_file(_service, item, _download_root, **_kw):
    return item["id"]  # extract_learning_text below looks this "path" up by id


def _fake_extract_learning_text(path):
    return TEXT_BY_ID[path]


def test_grade_tutorial_success_path(tmp_path):
    work_path = _work_file(tmp_path)
    state = _state(tmp_path, list(PAIR_FILES))

    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "pull_file", side_effect=_fake_pull_file), \
         patch.object(drive, "extract_learning_text", side_effect=_fake_extract_learning_text), \
         patch.object(grading, "grade") as mock_grade:
        mock_grade.return_value = grading.GradeResult(
            ok=True, feedback="Q1: Correct.", backend="omniroute",
            model="claude-opus-5", rung="claude/claude-opus-5", error="")
        dashboard.do_grade_tutorial(state, "job-1", "prac-1", work_path, "my_answers.txt")

    # The practice question text and the solution text both reached grading.grade().
    args, kwargs = mock_grade.call_args
    assert args[0] == work_path
    assert args[1] == "d/dx x^2 = 2x"          # solution_text
    assert args[2] == "Tut01_solutions.pdf"    # solution_name
    assert args[3] == "Q1: differentiate x^2"  # practice_text
    assert args[4] == "MH1300/Tutorials/Tut01.pdf"  # material_name

    assert state.grading is False
    assert state.grading_job["ok"] is True
    assert state.grading_job["done"] is True
    assert state.grading_job["feedback"] == "Q1: Correct."
    assert state.grading_job["solution_name"] == "Tut01_solutions.pdf"

    saved = state.notebook.get_grading("job-1")
    assert saved["ok"] == 1
    assert saved["solution_document_id"] == "sol-1"
    assert saved["feedback"] == "Q1: Correct."

    assert not os.path.exists(work_path)  # staged upload is cleaned up


def test_grade_tutorial_with_no_matching_solution_fails_clearly(tmp_path):
    work_path = _work_file(tmp_path)
    orphan_file = {"id": "prac-1", "name": "Tut01.pdf", "rel_path": "MH1300/Tut01.pdf",
                   "mime_type": "application/pdf", "modified": ""}
    state = _state(tmp_path, [orphan_file])

    dashboard.do_grade_tutorial(state, "job-1", "prac-1", work_path, "my_answers.txt")

    assert state.grading is False
    assert state.grading_job["ok"] is False
    assert state.grading_job["done"] is True
    assert "No professor solution" in state.grading_job["error"]
    saved = state.notebook.get_grading("job-1")
    assert saved["ok"] == 0
    assert not os.path.exists(work_path)


def test_grade_tutorial_works_when_opened_material_is_the_solution_itself(tmp_path):
    work_path = _work_file(tmp_path)
    state = _state(tmp_path, list(PAIR_FILES))
    state.grading_job["document_id"] = "sol-1"

    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "pull_file", side_effect=_fake_pull_file), \
         patch.object(drive, "extract_learning_text", side_effect=_fake_extract_learning_text), \
         patch.object(grading, "grade") as mock_grade:
        mock_grade.return_value = grading.GradeResult(
            ok=True, feedback="Correct.", backend="omniroute", model="claude-opus-5",
            rung="", error="")
        dashboard.do_grade_tutorial(state, "job-1", "sol-1", work_path, "my_answers.txt")

    assert state.grading_job["ok"] is True
    saved = state.notebook.get_grading("job-1")
    assert saved["solution_document_id"] == "sol-1"


def test_grade_tutorial_uploaded_file_is_removed_even_on_unexpected_error(tmp_path):
    work_path = _work_file(tmp_path)
    state = _state(tmp_path, list(PAIR_FILES))

    with patch.object(drive, "build_service", side_effect=RuntimeError("boom")):
        dashboard.do_grade_tutorial(state, "job-1", "prac-1", work_path, "my_answers.txt")

    assert state.grading is False
    assert state.grading_job["ok"] is False
    assert not os.path.exists(work_path)
