"""Course exclusion. An instruction not to upload something must hold everywhere."""
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ntu_learn_downloader import api, rest
from ntu_learn_downloader.drive_push import collect_files

COURSES = [
    ("26S1-ML0004-CAREER DESIGN & WKPL READINESS (MAIN SITE)", "_1_1"),
    ("26S1-ML0004-CAREER DESIGN & WKPL READINESS (T087)", "_2_1"),
    ("26S1-SC2005-OPERATING SYSTEMS", "_3_1"),
    ("26S1-MH2500-PROBABILITY", "_4_1"),
]


class TestCourseExclusion(unittest.TestCase):
    def test_is_excluded_is_case_insensitive_substring(self):
        self.assertTrue(api.is_excluded("26S1-ML0004-CAREER DESIGN", ["ml0004"]))
        self.assertTrue(api.is_excluded("26S1-ML0004-CAREER DESIGN", ["ML0004"]))
        self.assertFalse(api.is_excluded("26S1-SC2005-OPERATING SYSTEMS", ["ML0004"]))

    def test_empty_exclude_keeps_everything(self):
        self.assertFalse(api.is_excluded("anything", []))
        self.assertFalse(api.is_excluded("anything", None))
        self.assertFalse(api.is_excluded("anything", ["", "   "]))

    def test_excluded_courses_never_reach_the_course_list(self):
        with patch.object(rest, "get_courses", return_value=COURSES):
            got = api.get_courses("tok", exclude=["ML0004"])
        self.assertEqual([n for n, _ in got],
                         ["26S1-SC2005-OPERATING SYSTEMS", "26S1-MH2500-PROBABILITY"])

    def test_both_ml0004_sections_are_removed(self):
        with patch.object(rest, "get_courses", return_value=COURSES):
            got = api.get_courses("tok", exclude=["ML0004"])
        self.assertFalse(any("ML0004" in n for n, _ in got))

    def test_exclusion_also_applies_to_the_legacy_scraper(self):
        with patch.object(rest, "get_courses", side_effect=rest.RestUnavailable("x")), \
             patch.object(api, "get_courses_legacy", return_value=COURSES):
            got = api.get_courses("tok", favorites_only=False, exclude=["ML0004"])
        self.assertFalse(any("ML0004" in n for n, _ in got))


class TestPushExclusion(unittest.TestCase):
    """The push-side guard: even a file already on disk must be held back."""

    def _stage(self, root, rel):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write("x")
        return full

    def test_collect_files_skips_excluded_paths(self):
        with TemporaryDirectory() as root:
            self._stage(root, os.path.join("26S1-ML0004-CAREER", "video.mp4"))
            self._stage(root, os.path.join("26S1-SC2005-OS", "notes.pdf"))
            kept = [f["rel_path"] for f in collect_files(root, exclude=["ML0004"])]
        self.assertEqual(len(kept), 1)
        self.assertIn("notes.pdf", kept[0])

    def test_collect_files_without_exclude_returns_all(self):
        with TemporaryDirectory() as root:
            self._stage(root, os.path.join("26S1-ML0004-CAREER", "video.mp4"))
            self._stage(root, os.path.join("26S1-SC2005-OS", "notes.pdf"))
            self.assertEqual(len(collect_files(root)), 2)

    def test_exclusion_matches_anywhere_in_the_relative_path(self):
        with TemporaryDirectory() as root:
            self._stage(root, os.path.join("Some Course", "ML0004 handout.pdf"))
            self.assertEqual(collect_files(root, exclude=["ML0004"]), [])


if __name__ == "__main__":
    unittest.main()
