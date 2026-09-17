import unittest
from tempfile import TemporaryDirectory

from intuition import profile


class TestStore(unittest.TestCase):
    def test_defaults_are_ntu_macs_y2_mathematical_and_computer_sciences(self):
        with TemporaryDirectory() as root:
            data = profile.Store(root).get()
            self.assertEqual(data["programme"], "NTU MACS")
            self.assertEqual(data["year"], "Y2")
            self.assertEqual(data["field"], "Mathematical and Computer Sciences")

    def test_update_persists_across_instances(self):
        with TemporaryDirectory() as root:
            store = profile.Store(root)
            store.update(programme="NTU CCDS", year="Y3")
            store.save()

            reloaded = profile.Store(root)
            data = reloaded.get()
            self.assertEqual(data["programme"], "NTU CCDS")
            self.assertEqual(data["year"], "Y3")
            # A field left out of the update keeps its previous value.
            self.assertEqual(data["field"], "Mathematical and Computer Sciences")

    def test_blank_update_falls_back_to_the_default_rather_than_emptying(self):
        with TemporaryDirectory() as root:
            store = profile.Store(root)
            store.update(field="   ")
            self.assertEqual(store.get()["field"], "Mathematical and Computer Sciences")

    def test_summary_reads_as_one_line(self):
        with TemporaryDirectory() as root:
            store = profile.Store(root)
            self.assertEqual(
                store.summary(),
                "NTU MACS, Year 2, Mathematical and Computer Sciences")


if __name__ == "__main__":
    unittest.main()
