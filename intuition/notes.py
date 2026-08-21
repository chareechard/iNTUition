"""Local-first study notes linked to Drive materials."""
import html as html_lib
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional


class NoteConflict(Exception):
    """Raised when a stale editor attempts to overwrite a newer note."""


class _NoteTextExtractor(HTMLParser):
    """Turns a note's stored HTML back into plain text for consumers - the
    Compendium prompt - that want the student's words, not their markup."""
    _BREAKS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "tr", "blockquote", "br"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """Strip a note's stored HTML down to plain text, keeping paragraph and
    list breaks so the structure survives even though the formatting does
    not. Falls back to a plain tag strip if the markup does not parse."""
    raw = str(value or "")
    try:
        parser = _NoteTextExtractor()
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class Notebook:
    def __init__(self, root: str):
        directory = os.path.join(os.path.abspath(root), ".intuition")
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "notes.sqlite3")
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notes (
                document_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                drive_modified TEXT NOT NULL DEFAULT '',
                markdown TEXT NOT NULL DEFAULT '',
                last_page INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            try:
                db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    document_id UNINDEXED, rel_path, markdown
                )""")
            except sqlite3.OperationalError:
                pass
            # One row per Compendium generation, success or failure - a failed run
            # still records what was attempted and why, so a `.tex`/log handed back
            # for a failed compile is discoverable later, not just returned once.
            db.execute("""CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                material_name TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                backend TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                rung TEXT NOT NULL DEFAULT '',
                tex_path TEXT NOT NULL DEFAULT '',
                pdf_path TEXT NOT NULL DEFAULT '',
                page_anchors TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                report TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
            # This repo's first schema migration: CREATE TABLE IF NOT EXISTS above is
            # a no-op against a summaries table that already existed before the
            # self-report column was added, so a pre-existing database needs it
            # added explicitly rather than picking it up "for free".
            existing_columns = {row[1] for row in
                               db.execute("PRAGMA table_info(summaries)")}
            if "report" not in existing_columns:
                db.execute(
                    "ALTER TABLE summaries ADD COLUMN report TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _row(row) -> Optional[Dict]:
        if not row:
            return None
        return dict(zip(("document_id", "rel_path", "mime_type", "drive_modified",
                         "markdown", "last_page", "created_at", "updated_at"), row))

    def get(self, document_id: str) -> Optional[Dict]:
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT document_id, rel_path, mime_type,
                drive_modified, markdown, last_page, created_at, updated_at
                FROM notes WHERE document_id=?""", (document_id[:256],)).fetchone()
        return self._row(row)

    def save(self, document_id: str, markdown: str, rel_path: str = "",
             mime_type: str = "", drive_modified: str = "", last_page: int = 1,
             expected_updated_at: str = "") -> Dict:
        document_id = str(document_id or "").strip()[:256]
        if not document_id:
            raise ValueError("document id is required")
        markdown = str(markdown or "")[:250000]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as db:
            current = self.get(document_id)
            if (current and expected_updated_at
                    and current["updated_at"] != expected_updated_at):
                raise NoteConflict("This note changed in another window")
            created = current["created_at"] if current else now
            db.execute("""INSERT INTO notes(document_id, rel_path, mime_type,
                drive_modified, markdown, last_page, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
                rel_path=excluded.rel_path, mime_type=excluded.mime_type,
                drive_modified=excluded.drive_modified, markdown=excluded.markdown,
                last_page=excluded.last_page, updated_at=excluded.updated_at""",
                (document_id, str(rel_path)[:1000], str(mime_type)[:200],
                 str(drive_modified)[:100], markdown, max(1, int(last_page or 1)),
                 created, now))
            try:
                db.execute("DELETE FROM notes_fts WHERE document_id=?", (document_id,))
                db.execute("INSERT INTO notes_fts(document_id, rel_path, markdown) VALUES(?,?,?)",
                           (document_id, str(rel_path)[:1000], markdown))
            except sqlite3.OperationalError:
                pass
        return self.get(document_id)

    @staticmethod
    def _match_expression(query: str) -> str:
        """Quoted tokens, with the last one prefix-matched.

        This feeds a search-as-you-type box, so the final token is nearly always
        half-written: without the prefix match, "eigen" finds nothing until
        "eigenvalue" is complete and the panel reads as "you have no notes on this"
        through most of every query. Quoting each token is what keeps FTS5
        operators appearing in a note's own text from being parsed as syntax.
        """
        tokens = [token.replace('"', '""') for token in re.findall(r"\w+", query)]
        if not tokens:
            return ""
        return " ".join(['"{}"'.format(t) for t in tokens[:-1]]
                        + ['"{}"*'.format(tokens[-1])])

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        match = self._match_expression(query)
        with self._lock, self._connect() as db:
            rows = None
            if match:
                try:
                    rows = db.execute("""SELECT n.document_id, n.rel_path, n.mime_type,
                        n.drive_modified, n.markdown, n.last_page, n.created_at,
                        n.updated_at
                        FROM notes_fts f JOIN notes n ON n.document_id=f.document_id
                        WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?""",
                        (match, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = None
            if rows is None:
                # No FTS5 in this SQLite, or a query that tokenised to nothing -
                # a lone "\begin{" is not a search, but the box should still answer.
                needle = "%{}%".format(query)
                rows = db.execute("""SELECT document_id, rel_path, mime_type,
                    drive_modified, markdown, last_page, created_at, updated_at
                    FROM notes WHERE rel_path LIKE ? OR markdown LIKE ?
                    ORDER BY updated_at DESC LIMIT ?""", (needle, needle, limit)).fetchall()
        return [self._row(row) for row in rows]

    _SUMMARY_COLUMNS = ("id", "document_id", "material_name", "prompt", "scope",
                       "backend", "model", "rung", "tex_path", "pdf_path",
                       "page_anchors", "ok", "error", "report", "created_at")
    _SUMMARY_SELECT = "SELECT " + ", ".join(_SUMMARY_COLUMNS) + " FROM summaries"

    @classmethod
    def _summary_row(cls, row) -> Optional[Dict]:
        if not row:
            return None
        return dict(zip(cls._SUMMARY_COLUMNS, row))

    def save_summary(self, summary_id: str, document_id: str, material_name: str = "",
                     prompt: str = "", scope: str = "", backend: str = "",
                     model: str = "", rung: str = "", tex_path: str = "",
                     pdf_path: str = "", page_anchors: Optional[List[int]] = None,
                     ok: bool = False, error: str = "", report: str = "") -> Dict:
        """Recording the model, backend and page anchors per summary is what makes
        Compendium's consistency and citation guarantees auditable rather than
        aspirational - every row, success or failure, says exactly which model
        wrote it and which pages of the source it actually cited."""
        summary_id = str(summary_id or "").strip()[:64]
        if not summary_id:
            raise ValueError("summary id is required")
        anchors = ",".join(str(int(n)) for n in (page_anchors or []))
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO summaries(id, document_id, material_name,
                prompt, scope, backend, model, rung, tex_path, pdf_path,
                page_anchors, ok, error, report, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                document_id=excluded.document_id, material_name=excluded.material_name,
                prompt=excluded.prompt, scope=excluded.scope, backend=excluded.backend,
                model=excluded.model, rung=excluded.rung, tex_path=excluded.tex_path,
                pdf_path=excluded.pdf_path, page_anchors=excluded.page_anchors,
                ok=excluded.ok, error=excluded.error, report=excluded.report""",
                (summary_id, str(document_id or "")[:256], str(material_name)[:1000],
                 str(prompt)[:4000], str(scope)[:20], str(backend)[:40],
                 str(model)[:100], str(rung)[:100], str(tex_path)[:1000],
                 str(pdf_path)[:1000], anchors[:2000], 1 if ok else 0,
                 str(error)[:2000], str(report)[:4000], now))
        return self.get_summary(summary_id)

    def get_summary(self, summary_id: str) -> Optional[Dict]:
        with self._lock, self._connect() as db:
            row = db.execute(self._SUMMARY_SELECT + " WHERE id=?",
                             (str(summary_id)[:64],)).fetchone()
        return self._summary_row(row)

    def list_summaries(self, document_id: str, limit: int = 20) -> List[Dict]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                self._SUMMARY_SELECT + " WHERE document_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(document_id)[:256], max(1, min(int(limit), 100)))).fetchall()
        return [self._summary_row(row) for row in rows]

    def delete_summary(self, summary_id: str) -> Optional[Dict]:
        """Delete one summary row, returning it (paths and all) so the caller can
        also remove its .tex/.pdf files - the DB row is this module's job, the files
        on disk are the caller's, the same split ``save_summary``/``generate``
        already keep."""
        summary_id = str(summary_id or "").strip()[:64]
        if not summary_id:
            return None
        with self._lock, self._connect() as db:
            row = db.execute(self._SUMMARY_SELECT + " WHERE id=?",
                             (summary_id,)).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM summaries WHERE id=?", (summary_id,))
        return self._summary_row(row)

    def delete_summaries_older_than(self, cutoff_iso: str) -> List[Dict]:
        """Bulk sweep for the dashboard's startup housekeeping - every deleted row
        is returned (paths and all) so the caller can also clear its .tex/.pdf
        files. Age is judged by ``created_at``, an ISO-8601 UTC timestamp, so the
        caller passes a cutoff of the same form."""
        with self._lock, self._connect() as db:
            rows = db.execute(self._SUMMARY_SELECT + " WHERE created_at < ?",
                             (cutoff_iso,)).fetchall()
            if rows:
                db.execute("DELETE FROM summaries WHERE created_at < ?", (cutoff_iso,))
        return [self._summary_row(row) for row in rows]
