"""End-to-end test of the move pipeline against a stubbed Drive.

The behaviour that matters and is easy to get wrong: after files are pushed and the
local copies reclaimed, a *second* scan of the same unchanged course must report
everything as archived and queue nothing. Without the ledger this loops forever.
"""
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import drive, sync
from intuition.ledger import Ledger
from intuition.tests.test_drive import FakeMedia, FakeService

COURSE = "26S1-ML0004"
STAMP = "2026-08-06T12:49:13.403Z"


def course_tree():
    def f(name, modified=STAMP):
        return {
            "type": "file", "name": name, "filename": name, "modified": modified,
            "predownload_link": "https://ntulearn.ntu.edu.sg/x",
        }

    return {
        "type": "folder", "name": COURSE,
        "children": [
            {"type": "folder", "name": "Tutorial Materials",
             "children": [
                 {"type": "folder", "name": "Tutorial 1",
                  "children": [f("slides.pdf"), f("notes.pptx")]},
             ]},
            {"type": "folder", "name": "Important Information",
             "children": [f("guide.pdf")]},
        ],
    }


def fake_download(plan_entries):
    """Stand in for the network fetch: create each planned file on disk."""
    for e in plan_entries:
        os.makedirs(os.path.dirname(e["path"]), exist_ok=True)
        with open(e["path"], "wb") as fh:
            fh.write(b"x" * 32)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(drive, "_require_libs", lambda: None),
            patch.dict(
                "sys.modules",
                {"googleapiclient.http": type("m", (), {"MediaFileUpload": FakeMedia})},
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_scan_download_push_rescan(self):
        with TemporaryDirectory() as root:
            ledger = Ledger(root)
            mirror = drive.DriveMirror(FakeService(), root_folder="iNTUition")

            # 1. First scan: everything is new.
            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            self.assertEqual(sync.summarize(plan)[sync.NEW], 3)

            # 2. Fetch them.
            fake_download(plan)

            # 3. Rescan: now staged on disk.
            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            self.assertEqual(sync.summarize(plan)[sync.CURRENT], 3)
            pushable = [e for e in plan if e["status"] in sync.PUSHABLE]
            self.assertEqual(len(pushable), 3)

            # 4. Push: uploads, verifies, deletes local, records ledger.
            for entry in pushable:
                drive.push_file(mirror, entry, root, ledger, move=True)
            ledger.save()

            self.assertEqual(len(ledger), 3)
            for entry in pushable:
                self.assertFalse(os.path.exists(entry["path"]))

            # 5. The whole point: rescanning must not re-queue anything.
            plan = sync.build_plan(course_tree(), root, ledger=Ledger(root))
            counts = sync.summarize(plan)
            self.assertEqual(counts[sync.ARCHIVED], 3)
            self.assertEqual(counts[sync.NEW], 0)
            self.assertEqual(
                [e for e in plan if e["status"] in sync.DOWNLOADABLE], [],
                "archived files must not be queued for download again",
            )

    def test_upstream_edit_after_archiving_is_picked_up(self):
        with TemporaryDirectory() as root:
            ledger = Ledger(root)
            mirror = drive.DriveMirror(FakeService(), root_folder="iNTUition")

            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            fake_download(plan)
            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            for entry in plan:
                drive.push_file(mirror, entry, root, ledger, move=True)
            ledger.save()

            # Lecturer re-uploads one file with a later timestamp.
            tree = course_tree()
            tree["children"][1]["children"][0]["modified"] = "2026-09-01T00:00:00Z"

            plan = sync.build_plan(tree, root, ledger=Ledger(root))
            by_name = {e["filename"]: e["status"] for e in plan}
            self.assertEqual(by_name["guide.pdf"], sync.UPDATED)
            self.assertEqual(by_name["slides.pdf"], sync.ARCHIVED)

    def test_drive_folder_structure_mirrors_local_tree(self):
        with TemporaryDirectory() as root:
            ledger = Ledger(root)
            service = FakeService()
            mirror = drive.DriveMirror(service, root_folder="iNTUition")

            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            fake_download(plan)
            plan = sync.build_plan(course_tree(), root, ledger=ledger)
            for entry in plan:
                drive.push_file(mirror, entry, root, ledger, move=True)

            store = service._files.store
            folders = {f["name"] for f in store.values()
                       if f["mimeType"] == drive.FOLDER_MIME}
            self.assertEqual(
                folders,
                {"iNTUition", COURSE, "Tutorial Materials", "Tutorial 1",
                 "Important Information"},
            )

            # slides.pdf must sit under .../Tutorial Materials/Tutorial 1
            by_name = {f["name"]: f for f in store.values()}
            tut1_id = next(fid for fid, f in store.items() if f["name"] == "Tutorial 1")
            self.assertIn(tut1_id, by_name["slides.pdf"]["parents"])

            # Each folder is created exactly once even across three files.
            created = [n for n, _ in service._files.created_folders]
            self.assertEqual(len(created), len(set(created)))


if __name__ == "__main__":
    unittest.main()
