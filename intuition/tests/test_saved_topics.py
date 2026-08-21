import unittest
from tempfile import TemporaryDirectory

from intuition import saved_topics


class TestStore(unittest.TestCase):
    def test_add_persists_across_instances(self):
        with TemporaryDirectory() as root:
            store = saved_topics.Store(root)
            item = store.add("Predicting exam stress", "Use calendar load to flag weeks.")
            self.assertTrue(item["id"])
            store.save()

            reloaded = saved_topics.Store(root)
            self.assertEqual(len(reloaded.items), 1)
            self.assertEqual(reloaded.items[0]["title"], "Predicting exam stress")

    def test_starring_the_same_title_twice_does_not_duplicate(self):
        with TemporaryDirectory() as root:
            store = saved_topics.Store(root)
            first = store.add("Same idea", "First phrasing.")
            second = store.add("Same idea", "Different phrasing, ignored.")
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(store.items), 1)

    def test_rejects_blank_title_or_topic(self):
        with TemporaryDirectory() as root:
            store = saved_topics.Store(root)
            with self.assertRaises(ValueError):
                store.add("", "A topic")
            with self.assertRaises(ValueError):
                store.add("A title", "   ")

    def test_remove_returns_false_for_missing_item(self):
        with TemporaryDirectory() as root:
            store = saved_topics.Store(root)
            self.assertFalse(store.remove("nope"))
            store.add("T", "An idea")
            self.assertTrue(store.remove(store.items[0]["id"]))
            self.assertEqual(store.items, [])

    def test_caps_at_max_saved_dropping_the_oldest(self):
        with TemporaryDirectory() as root:
            store = saved_topics.Store(root)
            for i in range(saved_topics.MAX_SAVED + 5):
                store.add("Title {}".format(i), "Idea {}".format(i))
            self.assertEqual(len(store.items), saved_topics.MAX_SAVED)
            # Newest survives, oldest ("Title 0") was dropped.
            titles = [i["title"] for i in store.items]
            self.assertIn("Title {}".format(saved_topics.MAX_SAVED + 4), titles)
            self.assertNotIn("Title 0", titles)


if __name__ == "__main__":
    unittest.main()
