import os
import unittest
from tempfile import TemporaryDirectory

from intuition import materials


class FakeLedger:
    def __init__(self, entries):
        self.entries = entries


def touch(root, rel, size=1024):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


class TestSelection(unittest.TestCase):
    def test_scoped_to_the_entrys_own_course(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OPERATING SYSTEMS/Lecture 3.txt")
            touch(root, "26S1-MH2500-PROBABILITY/Hand01.pdf")
            picked = [m["rel"] for m in materials.select("SC2005", root)]
            self.assertEqual(len(picked), 1)
            self.assertIn("SC2005", picked[0])

    def test_an_untagged_entry_shares_nothing(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OPERATING SYSTEMS/Lecture 3.txt")
            self.assertEqual(materials.select("", root), [])
            self.assertEqual(materials.select(None, root), [])

    def test_media_is_never_selected(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/Lecture 3.mp4", size=5000)
            touch(root, "26S1-SC2005-OS/Lecture 3.wav", size=5000)
            touch(root, "26S1-SC2005-OS/Lecture 3.txt")
            picked = [m["name"] for m in materials.select("SC2005", root)]
            self.assertEqual(picked, ["Lecture 3.txt"])

    def test_transcripts_rank_above_documents(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/aaa slides.pdf")
            touch(root, "26S1-SC2005-OS/zzz lecture.txt")
            picked = [m["name"] for m in materials.select("SC2005", root)]
            self.assertEqual(picked[0], "zzz lecture.txt")

    def test_file_count_is_capped(self):
        with TemporaryDirectory() as root:
            for n in range(30):
                touch(root, "26S1-SC2005-OS/L{:02d}.txt".format(n))
            self.assertEqual(len(materials.select("SC2005", root)),
                             materials.MAX_FILES)

    def test_total_size_is_capped(self):
        with TemporaryDirectory() as root:
            for n in range(6):
                touch(root, "26S1-SC2005-OS/L{}.pdf".format(n), size=3 * 1024 * 1024)
            picked = materials.select("SC2005", root, max_bytes=7 * 1024 * 1024)
            self.assertLessEqual(sum(p["size"] for p in picked), 7 * 1024 * 1024)
            self.assertLess(len(picked), 6)

    def test_a_single_oversized_file_is_skipped(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/huge.pdf", size=materials.MAX_FILE_BYTES + 1)
            touch(root, "26S1-SC2005-OS/small.pdf", size=10)
            self.assertEqual([m["name"] for m in materials.select("SC2005", root)],
                             ["small.pdf"])

    def test_drive_only_files_are_offered(self):
        """Move mode deletes local copies, so most of the term lives only in Drive."""
        with TemporaryDirectory() as root:
            ledger = FakeLedger({
                "26S1-SC2005-OS/Lecture 1.pdf": {"drive_id": "d1", "size": 2048},
                "26S1-MH2500-PROB/Other.pdf": {"drive_id": "d2", "size": 2048},
            })
            picked = materials.select("SC2005", root, ledger=ledger)
            self.assertEqual(len(picked), 1)
            self.assertEqual(picked[0]["drive_id"], "d1")
            self.assertIsNone(picked[0]["local"])

    def test_a_local_copy_wins_over_the_drive_record(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/Lecture 1.pdf")
            ledger = FakeLedger({"26S1-SC2005-OS/Lecture 1.pdf":
                                 {"drive_id": "d1", "size": 2048}})
            picked = materials.select("SC2005", root, ledger=ledger)
            self.assertEqual(len(picked), 1)
            self.assertIsNotNone(picked[0]["local"])

    def test_the_storage_dir_is_never_offered(self):
        with TemporaryDirectory() as root:
            touch(root, ".intuition/ledger.json")
            touch(root, ".intuition/research/materials/SC2005 notes.txt")
            self.assertEqual(materials.select("SC2005", root), [])


class TestStaging(unittest.TestCase):
    def test_files_land_flat_in_the_sandbox_and_are_removed_after(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/Lecture 1.txt")
            sandbox = os.path.join(root, "sandbox")
            os.makedirs(sandbox)
            picked = materials.select("SC2005", root)
            landed = materials.stage(picked, sandbox)
            self.assertEqual([m["name"] for m in landed], ["Lecture 1.txt"])
            self.assertTrue(os.path.isfile(
                os.path.join(sandbox, "materials", "Lecture 1.txt")))

            materials.clear(sandbox)
            self.assertFalse(os.path.exists(os.path.join(sandbox, "materials")))

    def test_same_named_files_from_different_folders_do_not_collide(self):
        with TemporaryDirectory() as root:
            touch(root, "26S1-SC2005-OS/wk1/Notes.txt")
            touch(root, "26S1-SC2005-OS/wk2/Notes.txt")
            sandbox = os.path.join(root, "sandbox")
            os.makedirs(sandbox)
            landed = materials.stage(materials.select("SC2005", root), sandbox)
            self.assertEqual(len(landed), 2)
            self.assertEqual(len({m["name"] for m in landed}), 2)

    def test_staging_replaces_whatever_the_last_run_left(self):
        with TemporaryDirectory() as root:
            sandbox = os.path.join(root, "sandbox")
            stale = os.path.join(sandbox, "materials")
            os.makedirs(stale)
            open(os.path.join(stale, "old.txt"), "w").write("stale")
            touch(root, "26S1-SC2005-OS/New.txt")
            materials.stage(materials.select("SC2005", root), sandbox)
            self.assertEqual(sorted(os.listdir(stale)), ["New.txt"])

    def test_a_drive_file_without_a_service_is_skipped_not_fatal(self):
        with TemporaryDirectory() as root:
            sandbox = os.path.join(root, "sandbox")
            os.makedirs(sandbox)
            specs = [{"rel": "26S1-SC2005-OS/x.pdf", "name": "x.pdf", "size": 10,
                      "local": None, "drive_id": "d1"}]
            self.assertEqual(materials.stage(specs, sandbox, service=None), [])


if __name__ == "__main__":
    unittest.main()
