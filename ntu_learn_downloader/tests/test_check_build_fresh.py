import os
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from tools import check_build_fresh as fresh  # noqa: E402


class TestStaleAssets(unittest.TestCase):
    """The check must notice the exact failure it exists for: a bundled asset
    that no longer matches the source it was built from."""

    def setUp(self):
        self._saved = (fresh.SOURCE_STATIC, fresh.BUNDLE_CANDIDATES)
        self.tmp = TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "src")
        self.bundle = os.path.join(self.tmp.name, "bundle")
        os.makedirs(os.path.join(self.source, "vendor"))
        os.makedirs(os.path.join(self.bundle, "vendor"))
        fresh.SOURCE_STATIC = self.source
        fresh.BUNDLE_CANDIDATES = (self.bundle,)

    def tearDown(self):
        fresh.SOURCE_STATIC, fresh.BUNDLE_CANDIDATES = self._saved
        self.tmp.cleanup()

    def write(self, root, rel, text):
        path = os.path.join(root, rel.replace("/", os.sep))
        with open(path, "w") as handle:
            handle.write(text)

    def test_matching_tree_is_clean(self):
        self.write(self.source, "dashboard.html", "<main>")
        self.write(self.bundle, "dashboard.html", "<main>")
        self.assertEqual(fresh.stale_assets(), ([], []))

    def test_edited_asset_is_reported_as_differing(self):
        self.write(self.source, "dashboard.html", "<main>new</main>")
        self.write(self.bundle, "dashboard.html", "<main>old</main>")
        missing, differing = fresh.stale_assets()
        self.assertEqual((missing, differing), ([], ["dashboard.html"]))

    def test_asset_absent_from_bundle_is_reported_as_missing(self):
        self.write(self.source, "vendor/katex.css", "body{}")
        missing, differing = fresh.stale_assets()
        self.assertEqual((missing, differing), (["vendor/katex.css"], []))

    def test_no_bundle_raises_rather_than_reporting_clean(self):
        fresh.BUNDLE_CANDIDATES = (os.path.join(self.tmp.name, "absent"),)
        with self.assertRaises(fresh.NoBuild):
            fresh.stale_assets()


if __name__ == "__main__":
    unittest.main()
