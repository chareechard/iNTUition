import json
import os
import unittest
from tempfile import TemporaryDirectory

from ntu_learn_downloader.ledger import Ledger, ledger_path


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

    def test_save_does_not_leave_tmp_file(self):
        with TemporaryDirectory() as root:
            led = Ledger(root)
            led.record("a.pdf", "d1", None, 1)
            led.save()
            self.assertFalse(os.path.exists(ledger_path(root) + ".tmp"))


if __name__ == "__main__":
    unittest.main()
