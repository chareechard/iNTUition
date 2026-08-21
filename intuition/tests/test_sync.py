import os
import time
import unittest
from tempfile import TemporaryDirectory

from intuition import sync
from intuition.ledger import Ledger


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
            # rel_path is the logical identity, always "/"-joined so the ledger key
            # is identical on Windows and POSIX.
            self.assertEqual(plan[0]["rel_path"], "CE2003/Tut1.pdf")
            self.assertEqual(
                plan[0]["path"], os.path.join(root, "CE2003", "Tut1.pdf")
            )

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
            self.assertEqual(plan[0]["rel_path"], "CE2003/Tutorials/a.pdf")
            self.assertEqual(plan[0]["folder"], "CE2003/Tutorials")
            self.assertEqual(
                plan[0]["path"], os.path.join(root, "CE2003", "Tutorials", "a.pdf")
            )

    def test_excessively_deep_ultra_tree_is_collapsed_not_dropped(self):
        node = file_node("lecture.pdf", "lecture.pdf")
        for n in range(8):
            node = {"type": "folder", "name": ("Long repeated title " * 4) + str(n),
                    "children": [node]}
        with TemporaryDirectory() as root:
            plan = sync.build_plan(tree([node]), root)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["name"], "lecture.pdf")
        self.assertLessEqual(len(os.path.abspath(plan[0]["path"])), 240)

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

    # ── path identity ───────────────────────────────────────────────────────
    # The ledger is keyed on rel_path, so rel_path must depend only on the course
    # tree. It used to depend on the absolute length of the download root, which
    # silently re-keyed every deep file when the root moved and re-downloaded
    # material that was already archived in Drive.

    def _deep_tree(self):
        node = file_node("lecture.pdf", "lecture.pdf", "2026-08-06T12:00:00Z")
        for n in range(6):
            node = {"type": "folder", "name": ("Past Midterm Exam Papers " * 2) + str(n),
                    "children": [node]}
        return tree([node])

    def test_rel_path_is_independent_of_download_root_depth(self):
        deep = self._deep_tree()
        shallow_root = os.path.join("C:" + os.sep if os.name == "nt" else os.sep, "N")
        nested_root = os.path.join(
            shallow_root, *["a directory with a fairly long name"] * 3
        )
        first = sync.build_plan(deep, shallow_root)
        second = sync.build_plan(deep, nested_root)
        self.assertEqual(
            [e["rel_path"] for e in first], [e["rel_path"] for e in second]
        )

    def test_archived_file_survives_the_root_being_moved(self):
        """The end-to-end regression: relocating the folder must not re-download.

        The two roots differ enough in length to straddle the folder-shortening
        threshold, which is exactly what used to change the key and make an
        archived file look new. Neither root needs to exist - no file is opened.
        """
        deep = self._deep_tree()
        drive_letter = "C:" + os.sep if os.name == "nt" else os.sep
        original = os.path.join(drive_letter, "NTU")
        moved_to = os.path.join(drive_letter, "Users", "student", "Documents",
                                "School Archive", "Backup Copy", "NTU")

        entry = sync.build_plan(deep, original)[0]
        led = Ledger(moved_to)
        # Pretend it was pushed to Drive from the original location and reclaimed.
        led.record(entry["rel_path"], "d1", entry["modified"], 10)

        plan = sync.build_plan(deep, moved_to, ledger=led)
        self.assertEqual(plan[0]["status"], sync.ARCHIVED)
        self.assertEqual(plan[0]["drive_id"], "d1")

    def test_shallow_file_keeps_its_key_across_the_upgrade(self):
        """Undeep files were already keyed on their logical path; nothing to migrate."""
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("CE2003/Tut1.pdf", "d1", "2026-08-06T12:00:00Z", 10)
            plan = sync.build_plan(
                tree([file_node("Tut1.pdf", "Tut1.pdf", "2026-08-06T12:00:00Z")]),
                root, ledger=led,
            )
            self.assertEqual(plan[0]["status"], sync.ARCHIVED)
            self.assertEqual(plan[0]["drive_id"], "d1")

    def test_deep_file_archived_under_the_old_collapse_rules_is_migrated(self):
        """The upgrade path for a real ledger.

        The superseded code dropped an over-long folder and hoisted its children
        into the parent. Those keys must still be recognised, or this very fix
        would re-download every deep file already sitting in Drive.
        """
        deep = self._deep_tree()
        with TemporaryDirectory() as root:
            entry = sync.build_plan(deep, root)[0]

            # Rebuild the key exactly as the old code laid it out.
            legacy_dir = root
            for segment in entry["rel_path"].split("/")[:-1]:
                candidate = os.path.join(legacy_dir, segment, "")
                if len(os.path.abspath(candidate)) <= sync.MAX_FOLDER_PATH:
                    legacy_dir = candidate
            legacy_key = os.path.relpath(
                os.path.join(legacy_dir, "lecture.pdf"), root
            ).replace("\\", "/")
            self.assertNotEqual(legacy_key, entry["rel_path"],
                                "this tree must actually exercise collapsing")

            led = Ledger(root)
            led.record(legacy_key, "d1", entry["modified"], 10)
            plan = sync.build_plan(deep, root, ledger=led)

            self.assertEqual(plan[0]["status"], sync.ARCHIVED)
            self.assertEqual(plan[0]["drive_id"], "d1")
            self.assertIsNotNone(led.get(entry["rel_path"]))

    def test_shortened_branches_do_not_collide(self):
        """Two deep siblings must not be folded onto one path and overwrite."""
        long_title = "Past Midterm Exam Papers Section " * 3

        def branch(label):
            return {"type": "folder", "name": long_title + label,
                    "children": [file_node("notes.pdf", "notes.pdf")]}

        deep = tree([branch("A"), branch("B")])
        root = os.path.join("C:" + os.sep if os.name == "nt" else os.sep, "R" * 150)
        plan = sync.build_plan(deep, root)
        self.assertEqual(len(plan), 2)
        self.assertNotEqual(plan[0]["path"], plan[1]["path"])
        self.assertNotEqual(plan[0]["rel_path"], plan[1]["rel_path"])

    # ── resource identity ───────────────────────────────────────────────────
    # Paths are not identity. When a lecturer inserts a folder level in Learn every
    # file below it moves, and a path-keyed ledger called the whole subtree new.

    REST_LINK = ("https://ntulearn.ntu.edu.sg/learn/api/public/v1/courses/_1_1/"
                 "contents/_200_1/attachments/_3_1/download")
    WEBDAV_LINK = ("https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1-dt-content-"
                   "rid-64191670_1/xid-64191670_1?sig=a")

    def test_source_id_from_rest_attachment(self):
        self.assertEqual(sync.source_id(self.REST_LINK), "bb:_200_1/_3_1")

    def test_source_id_from_embedded_webdav_link(self):
        self.assertEqual(sync.source_id(self.WEBDAV_LINK), "xid:64191670_1")

    def test_source_id_ignores_the_query_signature(self):
        """Learn re-signs these links per session; the id must not move with it."""
        self.assertEqual(
            sync.source_id(self.WEBDAV_LINK),
            sync.source_id(self.WEBDAV_LINK.replace("sig=a", "sig=totally-different")),
        )

    def test_source_id_is_empty_for_links_without_one(self):
        self.assertEqual(sync.source_id(None), "")
        self.assertEqual(sync.source_id("https://ntulearn.ntu.edu.sg/webapps/x"), "")

    def _moved_tree(self, *folders):
        node = {"type": "file", "name": "Chapter 0.pdf", "filename": "Chapter 0.pdf",
                "modified": "2026-08-06T12:00:00Z",
                "predownload_link": self.REST_LINK}
        for name in reversed(folders):
            node = {"type": "folder", "name": name, "children": [node]}
        return {"type": "folder", "name": "SC2002", "children": [node]}

    def test_file_moved_to_a_new_folder_is_still_archived(self):
        """The core regression: a reorganisation must not re-download the course."""
        with TemporaryDirectory() as root:
            led = Ledger(root)
            before = sync.build_plan(self._moved_tree("Chapter 0"), root, ledger=led)[0]
            led.record(before["rel_path"], "d1", before["modified"], 10,
                       source_id=before["source_id"])

            # The lecturer inserts a level above it; the file itself is untouched.
            after = sync.build_plan(
                self._moved_tree("Lecture Slides", "Chapter 0"), root, ledger=led)[0]

            self.assertNotEqual(after["rel_path"], before["rel_path"])
            self.assertEqual(after["status"], sync.ARCHIVED)
            self.assertEqual(after["drive_id"], "d1")
            # The record followed the file rather than being duplicated.
            self.assertEqual(len(led), 1)
            self.assertIsNotNone(led.get(after["rel_path"]))

    def test_scan_backfills_a_resource_id_onto_an_old_record(self):
        """Records predating source ids gain one, so the next move is free."""
        with TemporaryDirectory() as root:
            led = Ledger(root)
            tree = self._moved_tree("Chapter 0")
            entry = sync.build_plan(tree, root)[0]
            led.record(entry["rel_path"], "d1", entry["modified"], 10)  # no source_id
            self.assertFalse(led.get(entry["rel_path"]).get("source_id"))

            sync.build_plan(tree, root, ledger=led)
            self.assertEqual(led.get(entry["rel_path"])["source_id"],
                             "bb:_200_1/_3_1")


class TestRecoverRestructured(unittest.TestCase):
    """One-time rescue for archives written before resource ids existed."""

    def plan_entry(self, rel_path, status=sync.NEW):
        return {"rel_path": rel_path, "path": os.path.join("X", rel_path),
                "status": status, "modified": "2026-08-06T12:00:00Z",
                "source_id": "bb:_1_1/_2_1", "drive_id": None}

    def test_inserted_folder_level_is_recovered(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("SC2002/Chapter 0/Notes.pdf", "d1", "2026-08-06T12:00:00Z", 10)
            plan = [self.plan_entry("SC2002/Lecture Slides/Chapter 0/Notes.pdf")]

            recovered = sync.recover_restructured(plan, led, root)

            self.assertEqual(recovered, ["SC2002/Lecture Slides/Chapter 0/Notes.pdf"])
            self.assertEqual(plan[0]["status"], sync.ARCHIVED)
            self.assertEqual(plan[0]["drive_id"], "d1")
            self.assertIsNone(led.get("SC2002/Chapter 0/Notes.pdf"))

    def test_ambiguous_match_is_left_alone(self):
        """Two plausible records means guessing; re-downloading is the safe error."""
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("SC2002/Week 1/Notes.pdf", "d1", None, 10)
            led.record("SC2002/Week 2/Notes.pdf", "d2", None, 10)
            plan = [self.plan_entry("SC2002/Week 1/Week 2/Notes.pdf")]

            self.assertEqual(sync.recover_restructured(plan, led, root), [])
            self.assertEqual(plan[0]["status"], sync.NEW)

    def test_unrelated_path_is_not_claimed(self):
        """A shared filename is not enough; the old path must nest inside the new."""
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("SC2001/Tutorials/Notes.pdf", "d1", None, 10)
            plan = [self.plan_entry("SC2002/Lectures/Notes.pdf")]

            self.assertEqual(sync.recover_restructured(plan, led, root), [])
            self.assertEqual(plan[0]["status"], sync.NEW)

    def test_a_record_already_in_use_is_not_stolen(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("SC2002/Chapter 0/Notes.pdf", "d1", None, 10)
            plan = [
                self.plan_entry("SC2002/Chapter 0/Notes.pdf", status=sync.ARCHIVED),
                self.plan_entry("SC2002/New/Chapter 0/Notes.pdf"),
            ]

            self.assertEqual(sync.recover_restructured(plan, led, root), [])
            self.assertEqual(plan[1]["status"], sync.NEW)

    def test_one_record_is_claimed_only_once(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("SC2002/Chapter 0/Notes.pdf", "d1", None, 10)
            plan = [
                self.plan_entry("SC2002/A/Chapter 0/Notes.pdf"),
                self.plan_entry("SC2002/B/Chapter 0/Notes.pdf"),
            ]

            recovered = sync.recover_restructured(plan, led, root)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(
                [e["status"] for e in plan], [sync.ARCHIVED, sync.NEW])

    def test_no_ledger_is_a_noop(self):
        self.assertEqual(sync.recover_restructured([], None, "X"), [])


class TestSyncExtras(unittest.TestCase):
    def test_sanitised_names_used_for_paths(self):
        with TemporaryDirectory() as root:
            plan = sync.build_plan(
                tree([file_node("x", "Week 1 / Notes: draft.pdf")]), root
            )
            self.assertNotIn("/", os.path.basename(plan[0]["path"]))
            self.assertNotIn(":", os.path.basename(plan[0]["path"]))


if __name__ == "__main__":
    unittest.main()
