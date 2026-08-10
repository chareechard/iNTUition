import os
import unittest
from tempfile import TemporaryDirectory

from ntu_learn_downloader import rnd


class TestBoard(unittest.TestCase):
    def test_add_and_read_back(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            item = b.add("Scheduler simulator", course="sc2005",
                         notes="visualise round-robin", link="https://x.test")
            self.assertEqual(item["title"], "Scheduler simulator")
            self.assertEqual(item["course"], "SC2005")   # normalised
            self.assertEqual(item["status"], rnd.IDEA)
            self.assertEqual(len(b), 1)

    def test_title_is_required(self):
        with TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                rnd.Board(root).add("   ")

    def test_newest_first(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            b.add("first")
            b.add("second")
            self.assertEqual(b.items[0]["title"], "second")

    def test_persists(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            b.add("Flashcards from transcripts", course="MH2500")
            b.save()
            again = rnd.Board(root)
            self.assertEqual(len(again), 1)
            self.assertEqual(again.items[0]["course"], "MH2500")

    def test_corrupt_file_is_ignored(self):
        with TemporaryDirectory() as root:
            p = rnd.board_path(root)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("{ not json")
            self.assertEqual(len(rnd.Board(root)), 0)

    def test_advance_walks_the_cycle(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            i = b.add("x")
            self.assertEqual(b.advance(i["id"])["status"], rnd.RESEARCHING)
            self.assertEqual(b.advance(i["id"])["status"], rnd.PROTOTYPING)
            self.assertEqual(b.advance(i["id"])["status"], rnd.BUILT)
            self.assertEqual(b.advance(i["id"])["status"], rnd.IDEA)

    def test_parked_is_not_in_the_click_cycle(self):
        """A stray click must not bury an item, and un-parking returns it to idea."""
        self.assertNotIn(rnd.PARKED, rnd.CYCLE)
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            i = b.add("x")
            b.update(i["id"], status=rnd.PARKED)
            self.assertEqual(b.advance(i["id"])["status"], rnd.IDEA)

    def test_update_fields(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            i = b.add("x")
            b.update(i["id"], title="y", course="sc2001", notes="n")
            got = b.get(i["id"])
            self.assertEqual(got["title"], "y")
            self.assertEqual(got["course"], "SC2001")
            self.assertEqual(got["notes"], "n")

    def test_update_rejects_an_unknown_status(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            i = b.add("x")
            b.update(i["id"], status="banana")
            self.assertEqual(b.get(i["id"])["status"], rnd.IDEA)

    def test_update_and_advance_on_a_missing_id(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            self.assertIsNone(b.update("nope", title="x"))
            self.assertIsNone(b.advance("nope"))

    def test_remove(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            i = b.add("x")
            self.assertTrue(b.remove(i["id"]))
            self.assertFalse(b.remove(i["id"]))
            self.assertEqual(len(b), 0)

    def test_counts_and_courses(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            a = b.add("a", course="SC2005")
            b.add("b", course="MH2500")
            b.advance(a["id"])
            self.assertEqual(b.counts()[rnd.IDEA], 1)
            self.assertEqual(b.counts()[rnd.RESEARCHING], 1)
            self.assertEqual(b.courses(), ["MH2500", "SC2005"])

    def test_snapshot_puts_active_work_before_ideas(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            b.add("an idea")
            proto = b.add("in progress")
            b.update(proto["id"], status=rnd.PROTOTYPING)
            order = [i["status"] for i in b.snapshot()["items"]]
            self.assertEqual(order[0], rnd.PROTOTYPING)

    def test_snapshot_is_newest_first_within_a_status_group(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            older = b.add("older")
            newer = b.add("newer")
            b.update(older["id"], notes="touched first")
            b.update(newer["id"], notes="touched second")
            ideas = [i["title"] for i in b.snapshot()["items"]
                     if i["status"] == rnd.IDEA]
            self.assertEqual(ideas[0], "newer")

    def test_ids_are_unique(self):
        with TemporaryDirectory() as root:
            b = rnd.Board(root)
            ids = {b.add("t{}".format(n))["id"] for n in range(30)}
            self.assertEqual(len(ids), 30)


if __name__ == "__main__":
    unittest.main()
