"""Tests for the REST content backend.

These stub out ``requests.get`` rather than using the synthetic HTTP server, because the
REST endpoints are absolute URLs baked into constants.py.
"""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from intuition import rest

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
                "id": "_800_1",
                "parentId": "_100_1",
                "position": 4,
                "title": "Inline Ultra slide",
                "modified": "2026-08-04T00:00:00.000Z",
                "body": ('<a href="https://ntulearn.ntu.edu.sg/bbcswebdav/'
                         'pid-800-dt-content-rid-99/xid-99?signed=yes" '
                         'data-bbfile=\'{"linkName":"Chapter 1",'
                         '"mimeType":"application/pdf"}\'>Chapter 1</a>'),
                "contentHandler": {"id": "resource/x-bb-document"},
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
    "/learn/api/public/v1/courses/{}/contents/_800_1/attachments".format(COURSE_ID): {
        "results": []
    },
}

# Live iNTUition answers /attachments on a folder with 400, not 404. Model that so the
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


def fake_get(url, headers=None, cookies=None, params=None, **_kwargs):
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
    def test_todo_items_are_course_scoped_and_normalized(self):
        calls = []

        def calendar_get(url, headers=None, cookies=None, params=None, **_kwargs):
            calls.append(dict(params or {}))
            return FakeResponse({"results": [{
                "id": "_42_1", "title": "Lab 1", "end": "2026-08-20T08:00:00Z",
                "type": "GradebookColumn",
                "dynamicCalendarItemProps": {"eventType": "Assignment"},
            }]})

        courses = [{"id": COURSE_ID, "name": "SC2005"}]
        with patch("intuition.rest._SESSION.get", calendar_get):
            items = rest.get_todo_items(BbRouter, courses)
        self.assertEqual(items[0]["source_id"], "_42_1")
        self.assertEqual(items[0]["course"], "SC2005")
        self.assertEqual(items[0]["kind"], "Assignment")
        self.assertEqual(calls[0]["courseId"], COURSE_ID)
        since = rest.datetime.fromisoformat(calls[0]["since"])
        until = rest.datetime.fromisoformat(calls[0]["until"])
        self.assertLessEqual(until - since, rest.timedelta(days=7, seconds=1))

    def test_get_courses_defaults_to_favourites(self):
        with patch("intuition.rest._SESSION.get", fake_get):
            self.assertEqual(
                rest.get_courses(BbRouter),
                [("26S1-SC2005-OPERATING SYSTEMS", COURSE_ID),
                 ("26S1-MH2500-PROBABILITY", "_7_1")],
            )

    def test_favourite_query_param_is_sent(self):
        seen = {}

        def capture(url, headers=None, cookies=None, params=None, **_kwargs):
            seen["url"] = url
            seen["params"] = params
            return FakeResponse(ROUTES["/learn/api/v1/users/me/memberships"])

        with patch("intuition.rest._SESSION.get", capture):
            rest.get_favorite_courses(BbRouter)
        self.assertIn("/learn/api/v1/users/me/memberships", seen["url"])
        self.assertEqual(seen["params"]["favorite"], "true")

    def test_rows_not_flagged_favourite_are_dropped(self):
        payload = {"results": [
            {"favorite": True, "course": {"id": "_1_1", "name": "Keep"}},
            {"favorite": False, "course": {"id": "_2_1", "name": "Drop"}},
        ]}
        with patch("intuition.rest._SESSION.get",
                   lambda *a, **k: FakeResponse(payload)):
            self.assertEqual(rest.get_favorite_courses(BbRouter), [("Keep", "_1_1")])

    def test_no_favourites_raises_actionable_error(self):
        with patch("intuition.rest._SESSION.get",
                   lambda *a, **k: FakeResponse({"results": []})):
            with self.assertRaises(rest.RestUnavailable) as ctx:
                rest.get_favorite_courses(BbRouter)
        self.assertIn("--scope favourites", str(ctx.exception))

    def test_get_courses_all_uses_public_api_and_skips_disabled(self):
        with patch("intuition.rest._SESSION.get", fake_get):
            self.assertEqual(
                rest.get_courses(BbRouter, favorites_only=False),
                [("19S2-CE2003-DIGITAL SYSTEMS DESIGN", COURSE_ID)],
            )

    def test_get_download_dir_tree(self):
        with patch("intuition.rest._SESSION.get", fake_get):
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
            [("folder", "Tutorial Solutions"), ("file", "Tut1_CE2003.pdf"),
             ("file", "Chapter 1.pdf")],
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

        def paging_get(url, headers=None, cookies=None, params=None, **_kwargs):
            calls.append((url, params))
            return FakeResponse(pages[len(calls) - 1])

        with patch("intuition.rest._SESSION.get", paging_get):
            self.assertEqual(
                rest.get_courses(BbRouter), [("A", "_1_1"), ("B", "_2_1")]
            )
        self.assertEqual(len(calls), 2)
        # First call carries our params, the nextPage URL carries its own.
        self.assertEqual(calls[0][1]["limit"], rest.PAGE_LIMIT)
        self.assertIsNone(calls[1][1])
        self.assertEqual(parse_qs(urlparse(calls[1][0]).query)["offset"], ["1"])

    def test_401_raises_rest_unavailable(self):
        def unauthorized(url, headers=None, cookies=None, params=None, **_kwargs):
            return FakeResponse({}, status_code=401)

        with patch("intuition.rest._SESSION.get", unauthorized):
            with self.assertRaises(rest.RestUnavailable):
                rest.get_courses(BbRouter)

    def test_xsrf_header_sent(self):
        captured = {}

        def capture(url, headers=None, cookies=None, params=None, **_kwargs):
            captured.update(headers or {})
            return FakeResponse(ROUTES["/learn/api/public/v1/users/me/courses"])

        with patch("intuition.rest._SESSION.get", capture):
            rest.get_courses(BbRouter)
        self.assertEqual(captured["X-Blackboard-XSRF"], "tok")


class TestDuplicateNames(unittest.TestCase):
    """Two different uploads can share one display name.

    Verified live in SC2002: one "Source code" document links two separate
    CallingMethods.java resources. They resolved to a single local path, so one
    overwrote the other and the sync plan listed the file twice.
    """

    def _doc(self, name, link):
        from intuition.models import Doc
        return Doc(name=name, link=link, filename=name, modified=None)

    def test_same_name_different_resources_are_disambiguated(self):
        docs = rest._uniquify_doc_names([
            self._doc("CallingMethods.java",
                      "https://x/bbcswebdav/pid-1-dt-content-rid-64191670_1/xid-64191670_1"),
            self._doc("CallingMethods.java",
                      "https://x/bbcswebdav/pid-1-dt-content-rid-64191673_1/xid-64191673_1"),
        ])
        names = [d.filename for d in docs]
        self.assertEqual(len(set(names)), 2, "the two files must not collide")
        # The first keeps its name so existing ledger keys are untouched.
        self.assertEqual(names[0], "CallingMethods.java")
        self.assertEqual(names[1], "CallingMethods-64191673_1.java")

    def test_distinct_names_are_left_alone(self):
        docs = rest._uniquify_doc_names([
            self._doc("A.java", "https://x/xid-1_1"),
            self._doc("B.java", "https://x/xid-2_1"),
        ])
        self.assertEqual([d.filename for d in docs], ["A.java", "B.java"])

    def test_three_way_clash_still_resolves(self):
        docs = rest._uniquify_doc_names([
            self._doc("N.java", "https://x/xid-1_1"),
            self._doc("N.java", "https://x/xid-2_1"),
            self._doc("N.java", "https://x/xid-2_1"),
        ])
        self.assertEqual(len(set(d.filename for d in docs)), 3)

    def test_name_without_an_xid_still_disambiguates(self):
        docs = rest._uniquify_doc_names([
            self._doc("N.pdf", "https://x/a"),
            self._doc("N.pdf", "https://x/b"),
        ])
        names = [d.filename for d in docs]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(names[1].endswith(".pdf"))

    def test_body_duplicates_survive_into_the_tree(self):
        """End-to-end: both copies reach the plan under distinct filenames."""
        item = {
            "id": "_1_1", "title": "Source code",
            "contentHandler": {"id": "resource/x-bb-document"},
            "body": (
                '<a href="https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1-dt-content-'
                'rid-64191670_1/xid-64191670_1?sig=a" data-bbfile=\'{"linkName":'
                '"CallingMethods.java","mimeType":"text/x-java-source"}\'>x</a>'
                '<a href="https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1-dt-content-'
                'rid-64191673_1/xid-64191673_1?sig=b" data-bbfile=\'{"linkName":'
                '"CallingMethods.java","mimeType":"text/x-java-source"}\'>y</a>'
            ),
        }
        docs = rest._uniquify_doc_names(rest._docs_from_body(item))
        self.assertEqual(len(docs), 2)
        self.assertEqual(len(set(d.filename for d in docs)), 2)


def refusing(*forbidden_paths):
    """fake_get, but the given paths answer 403 Access Denied."""
    def _get(url, headers=None, cookies=None, params=None, **_kwargs):
        if urlparse(url).path in forbidden_paths:
            return FakeResponse(
                {"status": 403, "message": "Access Denied"}, status_code=403
            )
        return fake_get(url, headers=headers, cookies=cookies, params=params)
    return _get


class TestForbidden(unittest.TestCase):
    """403 handling.

    A 403 on one item means that item is restricted; a 403 on the course itself
    means the whole request was refused. Conflating the two let a single
    date-released file abort an entire course scan - and because the caller then
    falls back to the Original-view scraper, which returns nothing for an Ultra
    course, the course silently came back empty.
    """

    ITEM_ATTACHMENTS = (
        "/learn/api/public/v1/courses/{}/contents/_200_1/attachments".format(COURSE_ID)
    )
    COURSE_CONTENTS = "/learn/api/public/v1/courses/{}/contents".format(COURSE_ID)

    def test_restricted_item_does_not_abort_the_course(self):
        with patch("intuition.rest._SESSION.get",
                   refusing(self.ITEM_ATTACHMENTS)):
            tree, skipped = rest.get_download_dir(BbRouter, "CE2003", COURSE_ID)

        # The rest of the course still came through.
        tutorials = tree["children"][0]
        self.assertEqual(
            [(c["type"], c["name"]) for c in tutorials["children"]],
            [("file", "Tut1_CE2003.pdf"), ("file", "Chapter 1.pdf")],
        )

    def test_restricted_item_is_reported_not_silently_dropped(self):
        with patch("intuition.rest._SESSION.get",
                   refusing(self.ITEM_ATTACHMENTS)):
            _tree, skipped = rest.get_download_dir(BbRouter, "CE2003", COURSE_ID)

        self.assertIn("Tutorial Solutions (access denied)", skipped)
        # The externally hosted item is still reported alongside it.
        self.assertIn("Recorded Lecture (Zoom)", skipped)

    def test_course_level_denial_is_still_fatal(self):
        with patch("intuition.rest._SESSION.get",
                   refusing(self.COURSE_CONTENTS)):
            with self.assertRaises(rest.RestUnavailable):
                rest.get_download_dir(BbRouter, "CE2003", COURSE_ID)

    def test_forbidden_is_a_kind_of_unavailable(self):
        """Existing `except RestUnavailable` handlers must keep catching 403s."""
        self.assertTrue(issubclass(rest.RestForbidden, rest.RestUnavailable))

    def test_403_is_not_retried(self):
        """Retrying a permission decision only adds load to a throttled server."""
        calls = []

        def counting(url, headers=None, cookies=None, params=None, **_kwargs):
            calls.append(url)
            return FakeResponse({"status": 403}, status_code=403)

        with patch("intuition.rest._SESSION.get", counting):
            with self.assertRaises(rest.RestForbidden):
                rest._get(BbRouter, "https://ntulearn.ntu.edu.sg/x")
        self.assertEqual(len(calls), 1)

    def test_session_is_pooled_with_backoff(self):
        """A scan issues hundreds of sequential calls; it must reuse the connection."""
        adapter = rest._SESSION.get_adapter("https://ntulearn.ntu.edu.sg/")
        retry = adapter.max_retries
        self.assertGreaterEqual(retry.total, 1)
        self.assertGreater(retry.backoff_factor, 0)
        self.assertIn(429, retry.status_forcelist)
        self.assertNotIn(403, retry.status_forcelist)


if __name__ == "__main__":
    unittest.main()
