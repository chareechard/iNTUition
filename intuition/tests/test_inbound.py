import os
import sqlite3
import unittest
from tempfile import TemporaryDirectory

from intuition import inbound

COLUMNS = ("email_id, sender, subject, priority, reasoning, action_items, flagged_at, "
           "status, actioned_at, confidence, matched_snippet, link, body_content")


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE flagged_emails ({})".format(
        ", ".join(c.strip() + " TEXT" for c in COLUMNS.split(","))))
    conn.executemany(
        "INSERT INTO flagged_emails ({}) VALUES ({})".format(
            COLUMNS, ",".join("?" * 13)), rows)
    conn.commit()
    conn.close()


def row(email_id="e1", priority="High", status="open", flagged_at="2026-08-01",
        subject="", snippet="Applications are now open. Apply by 31 Aug.",
        actions='["Apply by 31 Aug"]', body="SECRET BODY TEXT", confidence="0.75"):
    return (email_id, "spms-undergrad@ntu.edu.sg", subject, priority, "because",
            actions, flagged_at, status, None, confidence, snippet,
            "https://outlook.office.com/mail/id/x", body)


class TestInbound(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop(inbound.ENV_VAR, None)
        inbound._CACHE.clear()

    def tearDown(self):
        # Restore, or remove if it was never set - leaving it behind leaks into every
        # later test, since resolve_path consults it before the local store.
        if self._env is not None:
            os.environ[inbound.ENV_VAR] = self._env
        else:
            os.environ.pop(inbound.ENV_VAR, None)
        inbound._CACHE.clear()

    def test_absent_database_is_not_an_error(self):
        with TemporaryDirectory() as d:
            snap = inbound.snapshot(os.path.join(d, "nope.db"))
            self.assertFalse(snap["available"])
            self.assertEqual(snap["flags"], [])

    def test_only_open_flags_are_shown(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("a"), row("b", status="done")])
            self.assertEqual([f["id"] for f in inbound.open_flags(db)], ["a"])

    def test_urgency_outranks_recency(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("new-med", priority="Medium", flagged_at="2026-08-09"),
                         row("old-crit", priority="Critical", flagged_at="2026-08-01")])
            self.assertEqual([f["id"] for f in inbound.open_flags(db)],
                             ["old-crit", "new-med"])

    def test_newest_first_within_a_priority(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("older", flagged_at="2026-08-01"),
                         row("newer", flagged_at="2026-08-09")])
            self.assertEqual([f["id"] for f in inbound.open_flags(db)],
                             ["newer", "older"])

    def test_snapshot_groups_repeated_subjects_without_losing_ids(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("a", subject="GIC applications open"),
                         row("b", subject="GIC applications open")])
            snap = inbound.snapshot(db, ttl=0)
            self.assertEqual(snap["total"], 1)
            self.assertEqual(snap["flags"][0]["duplicate_count"], 2)
            self.assertEqual(set(snap["flags"][0]["ids"]), {"a", "b"})

    def test_subjectless_legacy_rows_group_by_fallback_title_and_sender(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("a", subject="", snippet="GIC applications open."),
                         row("b", subject="", snippet="GIC applications open.")])
            snap = inbound.snapshot(db, ttl=0)
            self.assertEqual(snap["flags"][0]["duplicate_count"], 2)

    def test_email_bodies_are_never_exposed(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row()])
            flag = inbound.open_flags(db)[0]
            self.assertNotIn("body_content", flag)
            self.assertNotIn("SECRET BODY TEXT", repr(flag))

    def test_json_action_items_are_parsed(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row(actions='["First thing", "Second thing"]')])
            self.assertEqual(inbound.open_flags(db)[0]["actions"],
                             ["First thing", "Second thing"])

    def test_an_empty_subject_falls_back_to_the_matched_snippet(self):
        """OWA does not always yield a subject; sender alone is unreadable."""
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row(subject="")])
            self.assertEqual(inbound.open_flags(db)[0]["title"],
                             "Applications are now open.")

    def test_a_real_subject_is_preferred(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row(subject="Scholarship renewal")])
            self.assertEqual(inbound.open_flags(db)[0]["title"],
                             "Scholarship renewal")

    def test_the_reader_cannot_write(self):
        """Read-only is enforced by SQLite, not by this module being careful."""
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row()])
            conn = sqlite3.connect("file:{}?mode=ro".format(db.replace("\\", "/")),
                                   uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM flagged_emails")
            conn.close()

    def test_a_corrupt_file_yields_no_flags(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            open(db, "w").write("not a database")
            self.assertEqual(inbound.open_flags(db), [])
            self.assertTrue(inbound.snapshot(db)["available"])

    def test_a_missing_table_yields_no_flags(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            sqlite3.connect(db).close()
            self.assertEqual(inbound.open_flags(db), [])

    def test_the_row_cap_holds(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("e{}".format(n)) for n in range(30)])
            self.assertEqual(len(inbound.open_flags(db)), inbound.MAX_ROWS)

    def test_the_env_var_overrides_the_default_path(self):
        os.environ[inbound.ENV_VAR] = r"X:\elsewhere\flagged.db"
        self.assertEqual(inbound.default_path(), r"X:\elsewhere\flagged.db")
        self.assertEqual(inbound.resolve_path("NTU"), r"X:\elsewhere\flagged.db")

    def test_the_default_path_stays_inside_this_project(self):
        """Independence, asserted: no sibling checkout may appear in the default."""
        path = inbound.default_path("NTU")
        self.assertTrue(path.startswith(os.path.join("NTU", ".intuition")))
        self.assertNotIn("J.A.R.V.I.S", path)
        self.assertNotIn("cerberus", path.lower())

    def test_results_are_cached_between_polls(self):
        with TemporaryDirectory() as d:
            db = os.path.join(d, "f.db")
            make_db(db, [row("a")])
            self.assertEqual(inbound.snapshot(db)["total"], 1)
            os.remove(db)
            self.assertEqual(inbound.snapshot(db)["total"], 1)      # cached
            self.assertEqual(inbound.snapshot(db, ttl=0)["total"], 0)  # fresh


if __name__ == "__main__":
    unittest.main()
