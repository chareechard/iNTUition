import pytest

from intuition.notes import Notebook, NoteConflict


def test_note_round_trip_and_metadata(tmp_path):
    book = Notebook(str(tmp_path))
    saved = book.save("drive-1", "# Week 2\n\nTransforms", rel_path="SC2001/L2.pdf",
                      mime_type="application/pdf", drive_modified="2026-08-12",
                      last_page=7)
    loaded = book.get("drive-1")
    assert loaded == saved
    assert loaded["markdown"].startswith("# Week 2")
    assert loaded["last_page"] == 7


def test_note_updates_use_optimistic_concurrency(tmp_path):
    book = Notebook(str(tmp_path))
    first = book.save("drive-1", "first")
    second = book.save("drive-1", "second", expected_updated_at=first["updated_at"])
    assert second["markdown"] == "second"
    with pytest.raises(NoteConflict):
        book.save("drive-1", "stale", expected_updated_at=first["updated_at"])


def test_notes_are_searchable_by_content_and_path(tmp_path):
    book = Notebook(str(tmp_path))
    book.save("one", "Laplace transform worked example", rel_path="SC2001/Lecture 4.pdf")
    book.save("two", "Graph traversal", rel_path="SC1007/Tutorial.pdf")
    assert [item["document_id"] for item in book.search("Laplace transform")] == ["one"]
    assert [item["document_id"] for item in book.search("SC1007")] == ["two"]


def test_empty_search_returns_no_notes(tmp_path):
    book = Notebook(str(tmp_path))
    book.save("one", "content")
    assert book.search("  ") == []


def test_a_half_typed_word_still_finds_the_note(tmp_path):
    """The search box runs on every keystroke, so prefixes have to match."""
    book = Notebook(str(tmp_path))
    book.save("one", "The eigenvalue of a symmetric matrix", rel_path="MH2500/L3.pdf")
    assert [item["document_id"] for item in book.search("eigen")] == ["one"]
    assert [item["document_id"] for item in book.search("symmetric eig")] == ["one"]


def test_search_survives_text_that_looks_like_fts_syntax(tmp_path):
    book = Notebook(str(tmp_path))
    book.save("one", "complexity is O(n) NEAR the bound", rel_path="SC2001/L1.pdf")
    assert [item["document_id"] for item in book.search("NEAR the")] == ["one"]
    assert book.search("\\begin{") == []


def test_summary_round_trip(tmp_path):
    book = Notebook(str(tmp_path))
    saved = book.save_summary(
        "sum-1", document_id="drive-1", material_name="Lecture 4.pdf",
        prompt="summarise deadlock", scope="notes", backend="omniroute",
        model="claude-opus-5", rung="claude/claude-opus-5",
        tex_path="/x/sum-1.tex", pdf_path="/x/sum-1.pdf", ok=True)
    loaded = book.get_summary("sum-1")
    assert loaded == saved
    assert loaded["ok"] == 1
    assert loaded["model"] == "claude-opus-5"


def test_summary_report_round_trips(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("sum-1", document_id="drive-1", ok=True,
                      report="Pages: 3/3. No figures reproduced.")
    loaded = book.get_summary("sum-1")
    assert loaded["report"] == "Pages: 3/3. No figures reproduced."


def test_summary_report_defaults_to_empty_string(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("sum-1", document_id="drive-1", ok=False)
    assert book.get_summary("sum-1")["report"] == ""


def test_a_pre_migration_database_picks_up_the_report_column(tmp_path):
    """The summaries table shipped without a report column before self-report
    support existed; a student's existing .intuition/notes.sqlite3 has to gain
    it on next open rather than erroring or silently losing the feature."""
    import sqlite3
    directory = tmp_path / ".intuition"
    directory.mkdir()
    db_path = directory / "notes.sqlite3"
    with sqlite3.connect(str(db_path)) as db:
        db.execute("""CREATE TABLE summaries (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
            material_name TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '', backend TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '', rung TEXT NOT NULL DEFAULT '',
            tex_path TEXT NOT NULL DEFAULT '', pdf_path TEXT NOT NULL DEFAULT '',
            page_anchors TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )""")
        db.execute("""INSERT INTO summaries(id, document_id, ok, created_at)
            VALUES('old-sum', 'drive-1', 1, '2026-01-01T00:00:00+00:00')""")

    book = Notebook(str(tmp_path))
    pre_existing = book.get_summary("old-sum")
    assert pre_existing["report"] == ""
    book.save_summary("new-sum", document_id="drive-1", ok=True, report="fresh report")
    assert book.get_summary("new-sum")["report"] == "fresh report"


def test_failed_summary_is_still_recorded_with_its_error(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("sum-1", document_id="drive-1", ok=False,
                      error="compile failed: undefined control sequence")
    loaded = book.get_summary("sum-1")
    assert loaded["ok"] == 0
    assert "undefined control sequence" in loaded["error"]


def test_list_summaries_is_scoped_to_one_document_newest_first(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("a", document_id="drive-1", ok=True)
    book.save_summary("b", document_id="drive-1", ok=True)
    book.save_summary("c", document_id="drive-2", ok=True)
    ids = [row["id"] for row in book.list_summaries("drive-1")]
    assert set(ids) == {"a", "b"}
    assert "c" not in ids


def test_summary_id_is_required(tmp_path):
    book = Notebook(str(tmp_path))
    with pytest.raises(ValueError):
        book.save_summary("", document_id="drive-1")


def test_delete_summary_returns_the_row_and_removes_it(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("sum-1", document_id="drive-1", tex_path="/x/sum-1.tex",
                      pdf_path="/x/sum-1.pdf", ok=True)
    deleted = book.delete_summary("sum-1")
    assert deleted["tex_path"] == "/x/sum-1.tex"
    assert deleted["pdf_path"] == "/x/sum-1.pdf"
    assert book.get_summary("sum-1") is None


def test_delete_summary_is_a_noop_for_an_unknown_id(tmp_path):
    book = Notebook(str(tmp_path))
    assert book.delete_summary("does-not-exist") is None
    assert book.delete_summary("") is None


def test_delete_summaries_older_than_only_removes_the_stale_ones(tmp_path):
    book = Notebook(str(tmp_path))
    book.save_summary("old", document_id="drive-1", ok=True)
    with book._connect() as db:
        db.execute("UPDATE summaries SET created_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+00:00", "old"))
    book.save_summary("new", document_id="drive-1", ok=True)
    removed = book.delete_summaries_older_than("2025-01-01T00:00:00+00:00")
    assert [row["id"] for row in removed] == ["old"]
    assert book.get_summary("old") is None
    assert book.get_summary("new") is not None
