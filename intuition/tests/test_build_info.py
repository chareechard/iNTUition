import os
import unittest
from tempfile import TemporaryDirectory

from intuition import build_info


class TestBuildInfo(unittest.TestCase):
    def test_page_path_points_at_the_served_page(self):
        self.assertTrue(os.path.isfile(build_info.page_path()))

    def test_page_built_formats_the_mtime(self):
        with TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "dashboard.html")
            with open(page, "w") as handle:
                handle.write("<!-- -->")
            os.utime(page, (1786600000, 1786600000))
            self.assertRegex(build_info.page_built(page),
                             r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_page_built_is_none_when_absent(self):
        self.assertIsNone(build_info.page_built(os.path.join("no", "such", "page")))

    def test_summary_reports_source_tree_when_not_frozen(self):
        summary = build_info.summary()
        self.assertFalse(summary["frozen"])
        self.assertEqual(summary["name"], "iNTUition")
        self.assertTrue(summary["version"])
        self.assertIsNotNone(summary["page_built"])


if __name__ == "__main__":
    unittest.main()
