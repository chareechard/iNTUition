import os
import threading
from types import SimpleNamespace

from intuition import dashboard
from intuition.notes import Notebook


def _state(tmp_path):
    return SimpleNamespace(
        notebook=Notebook(str(tmp_path)),
        lock=threading.Lock(),
        note=lambda _message: None,
        summary_job=None,
    )


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def test_delete_summary_files_removes_both_when_present(tmp_path):
    tex = str(tmp_path / "a.tex")
    pdf = str(tmp_path / "a.pdf")
    _write(tex)
    _write(pdf)
    dashboard.delete_summary_files({"tex_path": tex, "pdf_path": pdf})
    assert not os.path.exists(tex)
    assert not os.path.exists(pdf)


def test_delete_summary_files_tolerates_missing_paths_and_files():
    # No exception for an empty row, or for paths that were never written
    # (a failed generation never gets this far).
    dashboard.delete_summary_files({})
    dashboard.delete_summary_files({"tex_path": "", "pdf_path": ""})
    dashboard.delete_summary_files(
        {"tex_path": r"C:\nowhere\ghost.tex", "pdf_path": r"C:\nowhere\ghost.pdf"})


def test_sweep_removes_only_summaries_older_than_the_cutoff(tmp_path):
    state = _state(tmp_path)
    old_tex, old_pdf = str(tmp_path / "old.tex"), str(tmp_path / "old.pdf")
    new_tex, new_pdf = str(tmp_path / "new.tex"), str(tmp_path / "new.pdf")
    for p in (old_tex, old_pdf, new_tex, new_pdf):
        _write(p)

    state.notebook.save_summary("old", document_id="drive-1", tex_path=old_tex,
                                pdf_path=old_pdf, ok=True)
    with state.notebook._connect() as db:
        db.execute("UPDATE summaries SET created_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+00:00", "old"))
    state.notebook.save_summary("new", document_id="drive-1", tex_path=new_tex,
                                pdf_path=new_pdf, ok=True)

    dashboard.sweep_old_summaries(state, max_age_days=30)

    assert state.notebook.get_summary("old") is None
    assert state.notebook.get_summary("new") is not None
    assert not os.path.exists(old_tex)
    assert not os.path.exists(old_pdf)
    assert os.path.exists(new_tex)
    assert os.path.exists(new_pdf)


def test_sweep_disabled_by_zero_or_negative_retention(tmp_path):
    state = _state(tmp_path)
    state.notebook.save_summary("old", document_id="drive-1", ok=True)
    with state.notebook._connect() as db:
        db.execute("UPDATE summaries SET created_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+00:00", "old"))
    dashboard.sweep_old_summaries(state, max_age_days=0)
    assert state.notebook.get_summary("old") is not None


def test_sweep_failure_is_logged_not_raised():
    """A housekeeping sweep must never take the dashboard down at startup."""
    notes = []

    class ExplodingNotebook:
        def delete_summaries_older_than(self, _cutoff):
            raise RuntimeError("disk is on fire")

    state = SimpleNamespace(notebook=ExplodingNotebook(), note=notes.append)
    dashboard.sweep_old_summaries(state, max_age_days=30)
    assert any("Compendium sweep failed" in n for n in notes)
