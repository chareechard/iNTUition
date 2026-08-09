"""Scope guarantees for the Favourites restriction.

The dangerous failure mode is not an error - it is silently downloading all 41
enrolments when the user asked for their 7 starred courses. These tests pin that shut.
"""
import unittest
from unittest.mock import patch

import requests

from ntu_learn_downloader import api, rest
from ntu_learn_downloader.auth import AuthenticationError

BbRouter = "expires:9999999999,user:abc,v:2,xsrf:tok"

FAVOURITES = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")]
EVERYTHING = [("A", "_1_1"), ("B", "_2_1"), ("C", "_3_1")]


class TestFavouritesScope(unittest.TestCase):
    def test_default_is_favourites_only(self):
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = FAVOURITES
            api.get_courses(BbRouter)
        self.assertTrue(mocked.call_args.kwargs["favorites_only"])

    def test_all_courses_opt_out_is_passed_through(self):
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = EVERYTHING
            api.get_courses(BbRouter, favorites_only=False)
        self.assertFalse(mocked.call_args.kwargs["favorites_only"])

    def test_favourites_failure_does_not_fall_back_to_every_course(self):
        """The whole point: an error must not degrade into a wider download."""
        legacy_called = []

        with patch.object(rest, "get_courses",
                          side_effect=rest.RestUnavailable("boom")), \
             patch.object(api, "get_courses_legacy",
                          side_effect=lambda *a: legacy_called.append(1) or EVERYTHING):
            with self.assertRaises(AuthenticationError) as ctx:
                api.get_courses(BbRouter)

        self.assertEqual(legacy_called, [], "must not reach the scraper")
        self.assertIn("--all_courses", str(ctx.exception))

    def test_network_error_under_favourites_also_refuses(self):
        with patch.object(rest, "get_courses",
                          side_effect=requests.RequestException("offline")):
            with self.assertRaises(AuthenticationError):
                api.get_courses(BbRouter)

    def test_all_courses_still_falls_back_to_scraper(self):
        with patch.object(rest, "get_courses",
                          side_effect=rest.RestUnavailable("boom")), \
             patch.object(api, "get_courses_legacy", return_value=EVERYTHING):
            got = api.get_courses(BbRouter, favorites_only=False)
        self.assertEqual(got, EVERYTHING)

    def test_legacy_scraper_cannot_serve_favourites(self):
        with patch.object(api, "get_courses_legacy", return_value=EVERYTHING) as legacy:
            with self.assertRaises(AuthenticationError) as ctx:
                api.get_courses(BbRouter, prefer_rest=False)
        legacy.assert_not_called()
        self.assertIn("--legacy", str(ctx.exception))

    def test_legacy_with_all_courses_is_allowed(self):
        with patch.object(api, "get_courses_legacy", return_value=EVERYTHING):
            got = api.get_courses(BbRouter, prefer_rest=False, favorites_only=False)
        self.assertEqual(got, EVERYTHING)


if __name__ == "__main__":
    unittest.main()
