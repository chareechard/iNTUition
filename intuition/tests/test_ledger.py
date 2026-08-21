import json
import os
import unittest
from tempfile import TemporaryDirectory

from intuition.ledger import Ledger, ledger_path


class TestLedger(unittest.TestCase):
    def test_record_and_get_roundtrip(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("CE2003/Tut1.pdf", "drive123", "2026-08-06T12:00:00Z", 4096)
            led.save()

            reloaded = Ledger(root)
            entry = reloaded.get("CE2003/Tut1.pdf")
            self.assertEqual(entry["drive_id"], "drive123")
            self.assertEqual(entry["remote_modified"], "2026-08-06T12:00:00Z")
            self.assertEqual(entry["size"], 4096)
            self.assertIn("uploaded_at", entry)

    def test_windows_and_posix_separators_are_the_same_key(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record(os.path.join("CE2003", "Tut1.pdf"), "d1", None, 1)
            self.assertIsNotNone(led.get("CE2003/Tut1.pdf"))
            self.assertIsNotNone(led.get("CE2003\\Tut1.pdf"))

    def test_missing_file_gives_empty_ledger(self):
        with TemporaryDirectory() as root:
            self.assertEqual(len(Ledger(root)), 0)

    def test_corrupt_file_does_not_raise(self):
        with TemporaryDirectory() as root:
            path = ledger_path(root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("{ this is not json")
            self.assertEqual(len(Ledger(root)), 0)

    def test_non_dict_json_is_rejected(self):
        with TemporaryDirectory() as root:
            path = ledger_path(root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(["not", "a", "dict"], f)
            self.assertEqual(len(Ledger(root)), 0)

    def test_forget(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("a.pdf", "d1", None, 1)
            led.forget("a.pdf")
            self.assertIsNone(led.get("a.pdf"))

    def test_migrate_moves_a_record_onto_a_new_key(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("Course\\Deep\\a.pdf", "d1", "2026-08-06T12:00:00Z", 7)
            self.assertTrue(led.migrate("Course\\Deep\\a.pdf", "Course/Deep/Real/a.pdf"))
            moved = led.get("Course/Deep/Real/a.pdf")
            self.assertEqual(moved["drive_id"], "d1")
            self.assertEqual(moved["remote_modified"], "2026-08-06T12:00:00Z")
            self.assertIsNone(led.get("Course/Deep/a.pdf"))
            self.assertEqual(len(led), 1)

    def test_migrate_is_a_noop_for_a_missing_or_identical_key(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("a.pdf", "d1", None, 1)
            self.assertFalse(led.migrate("nothing.pdf", "b.pdf"))
            # Separator-only differences normalise to the same key.
            self.assertFalse(led.migrate("a.pdf", "a.pdf"))
            self.assertEqual(len(led), 1)

    def test_migrate_does_not_clobber_an_existing_record(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("old.pdf", "old-id", None, 1)
            led.record("new.pdf", "new-id", None, 2)
            led.migrate("old.pdf", "new.pdf")
            # The record already in the current scheme wins; the stale one is dropped.
            self.assertEqual(led.get("new.pdf")["drive_id"], "new-id")
            self.assertIsNone(led.get("old.pdf"))

    def test_find_by_source_locates_a_moved_record(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1, source_id="bb:_1_1/_2_1")
            found = led.find_by_source("bb:_1_1/_2_1")
            self.assertIsNotNone(found)
            key, entry = found
            self.assertEqual(key, "A/x.pdf")
            self.assertEqual(entry["drive_id"], "d1")

    def test_find_by_source_misses_cleanly(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1)
            self.assertIsNone(led.find_by_source("bb:nope"))
            self.assertIsNone(led.find_by_source(""))

    def test_index_follows_a_migration(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1, source_id="bb:_1_1/_2_1")
            led.migrate("A/x.pdf", "B/A/x.pdf")
            key, _entry = led.find_by_source("bb:_1_1/_2_1")
            self.assertEqual(key, "B/A/x.pdf")

    def test_index_survives_a_reload_from_disk(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1, source_id="bb:_1_1/_2_1")
            led.save()
            self.assertIsNotNone(Ledger(root).find_by_source("bb:_1_1/_2_1"))

    def test_attach_source_backfills_and_indexes(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1)
            self.assertTrue(led.attach_source("A/x.pdf", "bb:_1_1/_2_1"))
            self.assertIsNotNone(led.find_by_source("bb:_1_1/_2_1"))
            # Idempotent, and refuses an empty id or a missing record.
            self.assertFalse(led.attach_source("A/x.pdf", "bb:_1_1/_2_1"))
            self.assertFalse(led.attach_source("A/x.pdf", ""))
            self.assertFalse(led.attach_source("nothing.pdf", "bb:z"))

    def test_forgotten_record_leaves_no_stale_index_entry(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("A/x.pdf", "d1", None, 1, source_id="bb:_1_1/_2_1")
            led.forget("A/x.pdf")
            self.assertIsNone(led.find_by_source("bb:_1_1/_2_1"))

    def test_save_does_not_leave_tmp_file(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("a.pdf", "d1", None, 1)
            led.save()
            self.assertFalse(os.path.exists(ledger_path(root) + ".tmp"))


if __name__ == "__main__":
    unittest.main()
