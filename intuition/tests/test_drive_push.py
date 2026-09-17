"""Cover the CLI push path, which walks the disk instead of a Blackboard plan."""
import os
import unittest
from tempfile import TemporaryDirectory

from intuition import drive_push, sync
from intuition.ledger import STORAGE_DIR, Ledger


def make_file(root, rel, body=b"data"):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    return path


class TestCollectFiles(unittest.TestCase):
    def test_records_a_timestamp_for_every_file(self):
        """Without a stamp the ledger can never prove an archived file went stale.

        collect_files has no Blackboard metadata to draw on, so it uses the local
        mtime - the same value classify() compares against while the file is on disk.
        """
        with TemporaryDirectory() as root:
            make_file(root, "CE2003/a.pdf")
            files = drive_push.collect_files(root)
            self.assertEqual(len(files), 1)
            stamp = files[0]["modified"]
            self.assertTrue(stamp, "a stamp is required, not None")
            self.assertIsNotNone(sync.parse_iso8601(stamp))

    def test_recorded_stamp_lets_a_later_upstream_change_be_detected(self):
        """The end-to-end point of the stamp: an archived file can go stale."""
        with TemporaryDirectory() as root:
            path = make_file(root, "CE2003/a.pdf")
            entry = drive_push.collect_files(root)[0]
            led = Ledger(root)
            led.record(entry["rel_path"].replace("\\", "/"), "d1",
                       entry["modified"], entry["size"])
            os.remove(path)  # the move pipeline reclaimed the local copy

            fresh = sync.classify(path, "2099-01-01T00:00:00Z",
                                  led.get("CE2003/a.pdf"))
            self.assertEqual(fresh, sync.UPDATED)
            unchanged = sync.classify(path, "2000-01-01T00:00:00Z",
                                      led.get("CE2003/a.pdf"))
            self.assertEqual(unchanged, sync.ARCHIVED)

    def test_skips_state_directory_and_dummy_markers(self):
        with TemporaryDirectory() as root:
            make_file(root, "CE2003/a.pdf")
            make_file(root, "CE2003/.Lecture 1.mp4")
            make_file(root, ".intuition/db.sqlite3")
            make_file(root, "{}/drive_ledger.json".format(STORAGE_DIR))
            rels = [e["rel_path"] for e in drive_push.collect_files(root)]
            self.assertEqual(rels, [os.path.join("CE2003", "a.pdf")])


if __name__ == "__main__":
    unittest.main()
