"""Tests for the REST content backend.

These stub out ``requests.get`` rather than using the fixture HTTP server, because the
REST endpoints are absolute URLs baked into constants.py.
"""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from ntu_learn_downloader import rest

COURSE_ID = "_306327_1"

# A small course: one folder containing a document with two attachments, one directly
# attached file, and one externally hosted (Zoom) link that cannot be downloaded.
ROUTES = {
    "/learn/api/public/v1/users/me/courses": {
        "results": [
            {
                "courseId": COURSE_ID,
                "course": {"id": COURSE_ID, "name": "19S2-CE2003-DIGITAL SYSTEMS DESIGN"},
            },
            {
                "courseId": "_9_1",
                "course": {
                    "id": "_9_1",
                    "name": "Archived course",
                    "availability": {"available": "Disabled"},
                },
            },
        ]
    },
    # ?recursive=true returns the whole tree flat, every item carrying parentId and
    # modified. Top-level items hang off the course root content id (_root_1), which is
    # itself absent from the results - that is how roots are identified.
    "/learn/api/public/v1/courses/{}/contents".format(COURSE_ID): {
        "results": [
            {
                "id": "_100_1",
                "parentId": "_root_1",
                "position": 0,
                "title": "Tutorials",
                "modified": "2026-08-01T00:00:00.000Z",
                "hasChildren": True,
                "contentHandler": {"id": "resource/x-bb-folder"},
            },
            {
                "id": "_400_1",
                "parentId": "_root_1",
                "position": 1,
                "title": "Recorded Lecture (Zoom)",
                "hasChildren": False,
                "contentHandler": {"id": "resource/x-bb-blti-link"},
            },
            {
                "id": "_500_1",
                "parentId": "_root_1",
                "position": 2,
                "title": "Hidden thing",
                "hasChildren": False,
                "availability": {"available": "No"},
                "contentHandler": {"id": "resource/x-bb-document"},
            },
            {
                "id": "_200_1",
                "parentId": "_100_1",
                "position": 0,
                "title": "Tutorial Solutions",
                "modified": "2026-08-02T00:00:00.000Z",
                "hasChildren": False,
                "contentHandler": {"id": "resource/x-bb-document"},
            },
            {
                # x-bb-file items carry no hasChildren key at all on the live instance,
                # and the display title differs from the real filename.
                "id": "_300_1",
                "parentId": "_100_1",
                "position": 1,
                "title": "Tut1 handout",
                "modified": "2026-08-03T00:00:00.000Z",
                "contentHandler": {
                    "id": "resource/x-bb-file",
                    "file": {
                        "fileName": "Tut1_CE2003.pdf",
                        "mimeType": "application/pdf",
                    },
                },
            },
            {
                "id": "_600_1",
                "parentId": "_100_1",
                "position": 2,
                "title": "T1 - MCQ",
                "availability": {"available": "PartiallyVisible"},
                "contentHandler": {"id": "resource/x-bb-asmt-test-link"},
            },
            {
                "id": "_700_1",
                "parentId": "_100_1",
                "position": 3,
                "title": "T1 - Welcoming the Future World",
                "contentHandler": {"id": "resource/x-plugin-scormengine"},
            },
        ]
    },
    "/learn/api/public/v1/courses/{}/contents/_200_1/attachments".format(COURSE_ID): {
        "results": [
            {"id": "_1_1", "fileName": "Tut1_soln.pdf"},
            {"id": "_2_1", "fileName": "Tut2_soln.pdf"},
        ]
    },
    "/learn/api/public/v1/courses/{}/contents/_300_1/attachments".format(COURSE_ID): {
        "results": [{"id": "_3_1", "fileName": "Tut1_CE2003.pdf"}]
    },
}

# Live NTULearn answers /attachments on a folder with 400, not 404. Model that so the
# walk is proven not to abort on it.
BAD_REQUEST_PATHS = {
    "/learn/api/public/v1/courses/{}/contents/_100_1/attachments".format(COURSE_ID)
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected status {}".format(self.status_code))


def fake_get(url, headers=None, cookies=None, params=None):
    path = urlparse(url).path
    if path in BAD_REQUEST_PATHS:
        return FakeResponse(
            {"status": 400, "message": "The Content Item does not support file attachments"},
            status_code=400,
        )
    if path not in ROUTES:
        return FakeResponse({}, status_code=404)
    return FakeResponse(ROUTES[path])


BbRouter = "expires:9999999999,user:abc,v:2,xsrf:tok"

# Ultra's internal memberships endpoint, as returned with ?favorite=true. Shape verified
# against the live instance: `favorite` sits on the membership, course under `course`,
# and paging.nextPage is an empty string rather than absent.
ROUTES["/learn/api/v1/users/me/memberships"] = {
    "results": [
        {"favorite": True, "courseId": COURSE_ID,
         "course": {"id": COURSE_ID, "name": "26S1-SC2005-OPERATING SYSTEMS"}},
        {"favorite": True, "courseId": "_7_1",
         "course": {"id": "_7_1", "name": "26S1-MH2500-PROBABILITY"}},
    ],
    "paging": {"nextPage": "", "previousPage": "", "offset": 0, "limit": 100},
}


class TestRest(unittest.TestCase):
    def test_get_courses_defaults_to_favourites(self):
        with patch("ntu_learn_downloader.rest.requests.get", fake_get):
            self.assertEqual(
                rest.get_courses(BbRouter),
                [("26S1-SC2005-OPERATING SYSTEMS", COURSE_ID),
                 ("26S1-MH2500-PROBABILITY", "_7_1")],
            )

    def test_favourite_query_param_is_sent(self):
        seen = {}

        def capture(url, headers=None, cookies=None, params=None):
            seen["url"] = url
            seen["params"] = params
            return FakeResponse(ROUTES["/learn/api/v1/users/me/memberships"])

        with patch("ntu_learn_downloader.rest.requests.get", capture):
            rest.get_favorite_courses(BbRouter)
        self.assertIn("/learn/api/v1/users/me/memberships", seen["url"])
        self.assertEqual(seen["params"]["favorite"], "true")

    def test_rows_not_flagged_favourite_are_dropped(self):
        payload = {"results": [
            {"favorite": True, "course": {"id": "_1_1", "name": "Keep"}},
            {"favorite": False, "course": {"id": "_2_1", "name": "Drop"}},
        ]}
        with patch("ntu_learn_downloader.rest.requests.get",
                   lambda *a, **k: FakeResponse(payload)):
            self.assertEqual(rest.get_favorite_courses(BbRouter), [("Keep", "_1_1")])

    def test_no_favourites_raises_actionable_error(self):
        with patch("ntu_learn_downloader.rest.requests.get",
                   lambda *a, **k: FakeResponse({"results": []})):
            with self.assertRaises(rest.RestUnavailable) as ctx:
                rest.get_favorite_courses(BbRouter)
        self.assertIn("--scope favourites", str(ctx.exception))

    def test_get_courses_all_uses_public_api_and_skips_disabled(self):
        with patch("ntu_learn_downloader.rest.requests.get", fake_get):
            self.assertEqual(
                rest.get_courses(BbRouter, favorites_only=False),
                [("19S2-CE2003-DIGITAL SYSTEMS DESIGN", COURSE_ID)],
            )

    def test_get_download_dir_tree(self):
        with patch("ntu_learn_downloader.rest.requests.get", fake_get):
            tree, skipped = rest.get_download_dir(BbRouter, "CE2003", COURSE_ID)

        self.assertEqual(tree["type"], "folder")
        self.assertEqual(tree["name"], "CE2003")

        # Externally hosted item reported, not silently dropped.
        self.assertEqual(skipped, ["Recorded Lecture (Zoom)"])

        # Unavailable item excluded.
        names = [c["name"] for c in tree["children"]]
        self.assertEqual(names, ["Tutorials"])

        tutorials = tree["children"][0]
        # The two-attachment document keeps its folder level; the single-attachment
        # file is collapsed into the file itself. Quizzes and SCORM packages are
        # dropped, and the folder's own 400 on /attachments did not abort the walk.
        self.assertEqual(
            [(c["type"], c["name"]) for c in tutorials["children"]],
            [("folder", "Tutorial Solutions"), ("file", "Tut1_CE2003.pdf")],
        )

        solutions = tutorials["children"][0]
        self.assertEqual(
            [c["filename"] for c in solutions["children"]],
            ["Tut1_soln.pdf", "Tut2_soln.pdf"],
        )
        self.assertTrue(
            solutions["children"][0]["predownload_link"].endswith(
                "/courses/{}/contents/_200_1/attachments/_1_1/download".format(COURSE_ID)
            )
        )

    def test_paging_is_followed(self):
        pages = [
            {
                "results": [{"courseId": "_1_1", "course": {"id": "_1_1", "name": "A"}}],
                "paging": {"nextPage": "/learn/api/public/v1/users/me/courses?offset=1"},
            },
            {"results": [{"courseId": "_2_1", "course": {"id": "_2_1", "name": "B"}}]},
        ]
        calls = []

        def paging_get(url, headers=None, cookies=None, params=None):
            calls.append((url, params))
            return FakeResponse(pages[len(calls) - 1])

        with patch("ntu_learn_downloader.rest.requests.get", paging_get):
            self.assertEqual(
                rest.get_courses(BbRouter), [("A", "_1_1"), ("B", "_2_1")]
            )
        self.assertEqual(len(calls), 2)
        # First call carries our params, the nextPage URL carries its own.
        self.assertEqual(calls[0][1]["limit"], rest.PAGE_LIMIT)
        self.assertIsNone(calls[1][1])
        self.assertEqual(parse_qs(urlparse(calls[1][0]).query)["offset"], ["1"])

    def test_401_raises_rest_unavailable(self):
        def unauthorized(url, headers=None, cookies=None, params=None):
            return FakeResponse({}, status_code=401)

        with patch("ntu_learn_downloader.rest.requests.get", unauthorized):
            with self.assertRaises(rest.RestUnavailable):
                rest.get_courses(BbRouter)

    def test_xsrf_header_sent(self):
        captured = {}

        def capture(url, headers=None, cookies=None, params=None):
            captured.update(headers or {})
            return FakeResponse(ROUTES["/learn/api/public/v1/users/me/courses"])

        with patch("ntu_learn_downloader.rest.requests.get", capture):
            rest.get_courses(BbRouter)
        self.assertEqual(captured["X-Blackboard-XSRF"], "tok")


if __name__ == "__main__":
    unittest.main()
