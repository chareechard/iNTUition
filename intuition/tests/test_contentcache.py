"""Cache behaviour, and the request-count guarantees that justify it."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import rest
from intuition.contentcache import ContentCache, NEGATIVE_TTL, cache_path
from intuition.tests.test_rest import (
    BbRouter,
    COURSE_ID,
    fake_get,
)


class TestContentCache(unittest.TestCase):
    def test_miss_then_hit(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            self.assertIsNone(c.get_attachments(COURSE_ID, "_1_1", "T1"))
            c.put_attachments(COURSE_ID, "_1_1", "T1", [{"id": "_a_1", "fileName": "x.pdf"}])
            self.assertEqual(
                c.get_attachments(COURSE_ID, "_1_1", "T1"),
                [{"id": "_a_1", "fileName": "x.pdf"}],
            )

    def test_changed_stamp_invalidates(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments(COURSE_ID, "_1_1", "T1", [{"id": "_a_1"}])
            self.assertIsNone(c.get_attachments(COURSE_ID, "_1_1", "T2"))

    def test_absent_stamp_never_caches_or_serves(self):
        """Without a stamp we cannot prove freshness, so refuse to cache."""
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments(COURSE_ID, "_1_1", None, [{"id": "_a_1"}])
            self.assertIsNone(c.get_attachments(COURSE_ID, "_1_1", None))

    def test_survives_reload(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments(COURSE_ID, "_1_1", "T1", [{"id": "_a_1"}])
            c.save()
            self.assertEqual(
                ContentCache(root).get_attachments(COURSE_ID, "_1_1", "T1"),
                [{"id": "_a_1"}],
            )

    def test_corrupt_cache_is_ignored(self):
        with TemporaryDirectory() as root:
            p = cache_path(root)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("{ not json")
            self.assertEqual(ContentCache(root).courses, {})

    def test_prune_drops_deleted_items(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments(COURSE_ID, "_keep_1", "T", [])
            c.put_attachments(COURSE_ID, "_gone_1", "T", [])
            c.prune(COURSE_ID, ["_keep_1"])
            self.assertIn("_keep_1", c.courses[COURSE_ID])
            self.assertNotIn("_gone_1", c.courses[COURSE_ID])

    def test_courses_are_isolated(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments("_a_1", "_1_1", "T", [{"id": "x"}])
            self.assertIsNone(c.get_attachments("_b_1", "_1_1", "T"))

    def test_legacy_empty_result_is_refetched(self):
        """Old permanent negative rows caused newly published SC2002 files to vanish."""
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.courses = {COURSE_ID: {"_1_1": {"modified": "T1", "attachments": []}}}
            self.assertIsNone(c.get_attachments(COURSE_ID, "_1_1", "T1"))

    def test_empty_result_expires_even_when_parent_stamp_does_not(self):
        with TemporaryDirectory() as root:
            c = ContentCache(root)
            c.put_attachments(COURSE_ID, "_1_1", "T1", [])
            c.courses[COURSE_ID]["_1_1"]["checked_at"] = (
                datetime.now(timezone.utc) - NEGATIVE_TTL - timedelta(seconds=1)
            ).isoformat()
            self.assertIsNone(c.get_attachments(COURSE_ID, "_1_1", "T1"))


class TestScanRequestCost(unittest.TestCase):
    """The efficiency claim, pinned as a test rather than a promise."""

    def _count(self, cache):
        calls = {"contents": 0, "attachments": 0, "children": 0}

        def counting(url, headers=None, cookies=None, params=None, **_kwargs):
            if "/attachments" in url:
                calls["attachments"] += 1
            elif "/children" in url:
                calls["children"] += 1
            elif "/contents" in url:
                calls["contents"] += 1
            return fake_get(url, headers=headers, cookies=cookies, params=params)

        with patch("intuition.rest._SESSION.get", counting):
            tree, _ = rest.get_download_dir(BbRouter, "CE2003", COURSE_ID, cache=cache)
        return calls, tree

    def test_recursive_listing_replaces_per_folder_walk(self):
        with TemporaryDirectory() as root:
            calls, _ = self._count(ContentCache(root))
        self.assertEqual(calls["contents"], 1)
        self.assertEqual(calls["children"], 0, "must not walk folders one by one")

    def test_second_scan_makes_no_attachment_requests(self):
        with TemporaryDirectory() as root:
            cache = ContentCache(root)
            cold, tree_cold = self._count(cache)
            warm, tree_warm = self._count(cache)

        self.assertGreater(cold["attachments"], 0)
        self.assertEqual(warm["attachments"], 0, "unchanged course must cost 1 request")
        self.assertEqual(warm["contents"], 1)
        self.assertEqual(tree_cold, tree_warm, "cached scan must yield the same tree")

    def test_changed_item_is_refetched(self):
        with TemporaryDirectory() as root:
            cache = ContentCache(root)
            self._count(cache)
            # Professor edits the Tutorial Solutions item.
            cache.courses[COURSE_ID]["_200_1"]["modified"] = "1999-01-01T00:00:00.000Z"
            after, _ = self._count(cache)
        self.assertEqual(after["attachments"], 1, "only the changed item is refetched")

    def test_scan_without_cache_still_works(self):
        calls, tree = self._count(None)
        self.assertEqual(calls["contents"], 1)
        self.assertGreater(calls["attachments"], 0)
        self.assertEqual(tree["type"], "folder")


if __name__ == "__main__":
    unittest.main()
