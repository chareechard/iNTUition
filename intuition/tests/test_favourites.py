"""Scope guarantees for the Favourites restriction.

The dangerous failure mode is not an error - it is silently downloading all 41
enrolments when the user asked for their 7 starred courses. These tests pin that shut.
"""
import unittest
from datetime import date
from unittest.mock import patch

import requests

from intuition import api, rest
from intuition.auth import AuthenticationError

BbRouter = "expires:9999999999,user:abc,v:2,xsrf:tok"

FAVOURITES = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")]
EVERYTHING = [("A", "_1_1"), ("B", "_2_1"), ("C", "_3_1")]


class TestFavouritesScope(unittest.TestCase):
    def test_default_scope_is_the_current_semester(self):
        """Favourites is no longer the default; the semester label drives scope."""
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")]
            got = api.get_courses(BbRouter, today=date(2026, 8, 10))
        # The server is asked for every enrolment; narrowing happens locally.
        self.assertFalse(mocked.call_args.kwargs["favorites_only"])
        self.assertEqual(got, [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")])

    def test_favourites_scope_still_available(self):
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = FAVOURITES
            api.get_courses(BbRouter, scope=api.SCOPE_FAVOURITES)
        self.assertTrue(mocked.call_args.kwargs["favorites_only"])

    def test_legacy_favorites_only_flag_still_maps_to_favourites(self):
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = FAVOURITES
            api.get_courses(BbRouter, favorites_only=True)
        self.assertTrue(mocked.call_args.kwargs["favorites_only"])

    def test_there_is_no_everything_scope(self):
        """`all` was removed deliberately - it could drag years of stale material in."""
        self.assertEqual(set(api.SCOPES), {"semester", "favourites"})
        with self.assertRaises(ValueError):
            api.get_courses(BbRouter, scope="all")

    def test_deprecated_favorites_only_false_falls_back_to_semester(self):
        """False used to mean "everything"; with no such scope it means the default."""
        with patch.object(rest, "get_courses") as mocked:
            mocked.return_value = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")]
            got = api.get_courses(BbRouter, favorites_only=False, today=date(2026, 8, 10))
        self.assertEqual(got, [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")])

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            api.get_courses(BbRouter, scope="last-tuesday")

    def test_favourites_failure_does_not_fall_back_to_every_course(self):
        """The whole point: an error must not degrade into a wider download."""
        legacy_called = []

        with patch.object(rest, "get_courses",
                          side_effect=rest.RestUnavailable("boom")), \
             patch.object(api, "get_courses_legacy",
                          side_effect=lambda *a: legacy_called.append(1) or EVERYTHING):
            with self.assertRaises(AuthenticationError) as ctx:
                api.get_courses(BbRouter, scope=api.SCOPE_FAVOURITES)

        self.assertEqual(legacy_called, [], "must not reach the scraper")
        self.assertIn("--scope favourites", str(ctx.exception))

    def test_network_error_under_favourites_also_refuses(self):
        with patch.object(rest, "get_courses",
                          side_effect=requests.RequestException("offline")):
            with self.assertRaises(AuthenticationError):
                api.get_courses(BbRouter, scope=api.SCOPE_FAVOURITES)

    def test_semester_scope_may_fall_back_to_the_scraper(self):
        """Safe, because the semester filter still applies to whatever comes back."""
        scraped = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1"),
                   ("25S2-MH1101-CALCULUS II", "_2_1")]
        with patch.object(rest, "get_courses",
                          side_effect=rest.RestUnavailable("boom")), \
             patch.object(api, "get_courses_legacy", return_value=scraped):
            got = api.get_courses(BbRouter, today=date(2026, 8, 10))
        self.assertEqual(got, [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")],
                         "fallback must not smuggle in last semester")

    def test_legacy_scraper_cannot_serve_favourites(self):
        with patch.object(api, "get_courses_legacy", return_value=EVERYTHING) as legacy:
            with self.assertRaises(AuthenticationError) as ctx:
                api.get_courses(BbRouter, prefer_rest=False,
                                scope=api.SCOPE_FAVOURITES)
        legacy.assert_not_called()
        self.assertIn("--legacy", str(ctx.exception))

    def test_legacy_with_semester_scope_is_allowed(self):
        scraped = [("26S1-SC2005-OPERATING SYSTEMS", "_1_1"), ("Old admin course", "_9_1")]
        with patch.object(api, "get_courses_legacy", return_value=scraped):
            got = api.get_courses(BbRouter, prefer_rest=False, today=date(2026, 8, 10))
        self.assertEqual(got, [("26S1-SC2005-OPERATING SYSTEMS", "_1_1")])


if __name__ == "__main__":
    unittest.main()
