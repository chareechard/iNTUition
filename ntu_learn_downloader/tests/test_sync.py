import os
import time
import unittest
from tempfile import TemporaryDirectory

from ntu_learn_downloader import sync
from ntu_learn_downloader.ledger import Ledger


def tree(children):
    return {"type": "folder", "name": "CE2003", "children": children}


def file_node(name, filename=None, modified=None):
    return {
        "type": "file",
        "name": name,
        "filename": filename,
        "modified": modified,
        "predownload_link": "https://ntulearn.ntu.edu.sg/x",
    }


class TestSync(unittest.TestCase):
    def test_new_file(self):
        with TemporaryDirectory() as root:
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-08-06T12:49:13.403Z")]),
                root,
            )
            self.assertEqual([e["status"] for e in plan], [sync.NEW])
            self.assertEqual(plan[0]["rel_path"], os.path.join("CE2003", "Tut1.pdf"))

    def test_current_when_local_is_newer(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "CE2003")
            os.makedirs(path)
            open(os.path.join(path, "Tut1.pdf"), "w").close()
            # Remote change predates the local file.
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2020-01-01T00:00:00.000Z")]),
                root,
            )
            self.assertEqual(plan[0]["status"], sync.CURRENT)

    def test_updated_when_remote_is_newer(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "CE2003")
            os.makedirs(path)
            target = os.path.join(path, "Tut1.pdf")
            open(target, "w").close()
            old = time.time() - 86400
            os.utime(target, (old, old))
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2099-01-01T00:00:00.000Z")]),
                root,
            )
            self.assertEqual(plan[0]["status"], sync.UPDATED)

    def test_existing_file_without_timestamp_is_current(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "CE2003")
            os.makedirs(path)
            open(os.path.join(path, "Tut1.pdf"), "w").close()
            plan = sync.build_plan(tree([file_node("Tut1.pdf", "Tut1.pdf")]), root)
            self.assertEqual(plan[0]["status"], sync.CURRENT)

    def test_missing_filename_is_unknown(self):
        with TemporaryDirectory() as root:
            plan = sync.build_plan(tree([file_node("Tut1 solutions")]), root)
            self.assertEqual(plan[0]["status"], sync.UNKNOWN)

    def test_nested_folders_and_paths(self):
        with TemporaryDirectory() as root:
            nested = tree(
                [
                    {
                        "type": "folder",
                        "name": "Tutorials",
                        "children": [file_node("a.pdf", "a.pdf")],
                    }
                ]
            )
            plan = sync.build_plan(nested, root)
            self.assertEqual(
                plan[0]["rel_path"], os.path.join("CE2003", "Tutorials", "a.pdf")
            )
            self.assertEqual(plan[0]["folder"], os.path.join("CE2003", "Tutorials"))

    def test_ignore_files(self):
        with TemporaryDirectory() as root:
            plan = sync.build_plan(
                tree([file_node("a.pdf", "a.pdf")]), root, ignore_files=True
            )
            self.assertEqual(plan, [])

    def test_recorded_lecture_dummy_marker_is_ignored_status(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "CE2003")
            os.makedirs(path)
            open(os.path.join(path, ".Lecture 1.mp4"), "w").close()
            node = {
                "type": "recorded_lecture",
                "name": "Lecture 1",
                "predownload_link": "/x",
            }
            plan = sync.build_plan(
                tree([node]), root, ignore_recorded_lectures=False
            )
            self.assertEqual(plan[0]["status"], sync.IGNORED)

    def test_recorded_lectures_skipped_by_default(self):
        with TemporaryDirectory() as root:
            node = {"type": "recorded_lecture", "name": "L1", "predownload_link": "/x"}
            self.assertEqual(sync.build_plan(tree([node]), root), [])

    def test_summarize(self):
        plan = [{"status": sync.NEW}, {"status": sync.NEW}, {"status": sync.CURRENT}]
        counts = sync.summarize(plan)
        self.assertEqual(counts[sync.NEW], 2)
        self.assertEqual(counts[sync.CURRENT], 1)
        self.assertEqual(counts[sync.UPDATED], 0)

    # ── ledger-aware statuses ────────────────────────────────────────────────
    # These cover the move pipeline: once a file is in Drive and the local copy is
    # gone, the ledger must stop it being re-downloaded on every scan.

    def test_archived_when_gone_locally_but_in_ledger(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record(
                os.path.join("CE2003", "Tut1.pdf"), "d1", "2026-08-06T12:00:00Z", 10
            )
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-08-06T12:00:00Z")]),
                root, ledger=led,
            )
            self.assertEqual(plan[0]["status"], sync.ARCHIVED)
            self.assertEqual(plan[0]["drive_id"], "d1")

    def test_archived_file_updated_upstream_becomes_updated(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record(
                os.path.join("CE2003", "Tut1.pdf"), "d1", "2026-08-06T12:00:00Z", 10
            )
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-09-01T00:00:00Z")]),
                root, ledger=led,
            )
            self.assertEqual(plan[0]["status"], sync.UPDATED)

    def test_without_ledger_archived_file_looks_new(self):
        """Guards the regression the ledger exists to prevent."""
        with TemporaryDirectory() as root:
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-08-06T12:00:00Z")]), root
            )
            self.assertEqual(plan[0]["status"], sync.NEW)

    def test_archived_without_recorded_timestamp_stays_archived(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record(os.path.join("CE2003", "Tut1.pdf"), "d1", None, 10)
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-09-01T00:00:00Z")]),
                root, ledger=led,
            )
            self.assertEqual(plan[0]["status"], sync.ARCHIVED)

    def test_local_copy_wins_over_ledger(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "CE2003")
            os.makedirs(path)
            open(os.path.join(path, "Tut1.pdf"), "w").close()
            led = Ledger(root)
            led.record(os.path.join("CE2003", "Tut1.pdf"), "d1", None, 10)
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf")]), root, ledger=led
            )
            self.assertEqual(plan[0]["status"], sync.CURRENT)

    def test_archived_is_not_downloadable_but_current_is_pushable(self):
        self.assertNotIn(sync.ARCHIVED, sync.DOWNLOADABLE)
        self.assertIn(sync.CURRENT, sync.PUSHABLE)
        self.assertIn(sync.UPDATED, sync.PUSHABLE)
        self.assertNotIn(sync.ARCHIVED, sync.PUSHABLE)

    def test_summarize_counts_archived(self):
        counts = sync.summarize([{"status": sync.ARCHIVED}, {"status": sync.NEW}])
        self.assertEqual(counts[sync.ARCHIVED], 1)

    def test_parse_iso8601(self):
        self.assertIsNotNone(sync.parse_iso8601("2026-08-06T12:49:13.403Z"))
        self.assertIsNone(sync.parse_iso8601(None))
        self.assertIsNone(sync.parse_iso8601("not a date"))

    def test_sanitised_names_used_for_paths(self):
        with TemporaryDirectory() as root:
            plan = sync.build_plan(
                tree([file_node("x", "Week 1 / Notes: draft.pdf")]), root
            )
            self.assertNotIn("/", os.path.basename(plan[0]["path"]))
            self.assertNotIn(":", os.path.basename(plan[0]["path"]))


if __name__ == "__main__":
    unittest.main()
