"""Blackboard Learn public REST API client.

This is the primary content backend. It works for both Ultra and Original course views,
unlike the HTML scraping in ``parsing.py`` which only understands the Original view's
``listContent.jsp`` markup.

Authentication reuses the ``BbRouter`` session cookie: Learn's own Ultra frontend calls
these same endpoints with that cookie, so a browser-obtained token is sufficient and no
OAuth application registration is required.
"""
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from ntu_learn_downloader.auth import parse_bbrouter
from ntu_learn_downloader.constants import (
    DOCUMENT_HANDLERS,
    FOLDER_HANDLERS,
    NTULEARN_URL,
    REST_ATTACHMENT_DOWNLOAD_URL,
    REST_CONTENT_ATTACHMENTS_URL,
    REST_CONTENT_CHILDREN_URL,
    REST_COURSE_CONTENTS_URL,
    REST_INTERNAL_MEMBERSHIPS_URL,
    REST_MY_COURSES_URL,
    REST_VERSION_URL,
)
from ntu_learn_downloader.models import Doc, Folder, RecordedLecture

PAGE_LIMIT = 100

# Content handlers that point at something hosted outside Learn (Zoom, Panopto, Kaltura,
# Echo360, ...). These cannot be downloaded with a Learn session cookie alone.
EXTERNAL_HANDLERS = frozenset(
    ["resource/x-bb-externallink", "resource/x-bb-blti-link", "resource/x-bb-toollink"]
)

# Interactive activities with no downloadable file behind them. Observed live in NTU
# courses; ignored quietly rather than reported, since there is nothing a user could do
# about them.
ACTIVITY_HANDLERS = frozenset(
    [
        "resource/x-plugin-scormengine",
        "resource/x-bb-asmt-test-link",
        "resource/x-bb-courselink",
    ]
)


class RestUnavailable(Exception):
    """Raised when the REST backend cannot serve this request at all."""


def _headers(BbRouter: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "NTULearn-Downloader",
        "X-Requested-With": "XMLHttpRequest",
    }
    xsrf = parse_bbrouter(BbRouter).get("xsrf")
    if xsrf:
        headers["X-Blackboard-XSRF"] = xsrf
    return headers


def _get(BbRouter: str, url: str, params: Optional[Dict] = None) -> Dict:
    response = requests.get(
        url, headers=_headers(BbRouter), cookies={"BbRouter": BbRouter}, params=params
    )
    if response.status_code == 401:
        raise RestUnavailable(
            "REST API rejected the session token (401). It has likely expired."
        )
    if response.status_code == 403:
        raise RestUnavailable("REST API denied access (403) to {}".format(url))
    if response.status_code in (400, 404):
        # Normal for sub-resources that do not apply to a given item. Verified against
        # the live instance: asking a folder for its attachments returns
        # 400 {"message": "The Content Item does not support file attachments"},
        # not 404, so a bare raise_for_status() here would abort the whole walk.
        return {}
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def _get_paged(BbRouter: str, url: str, params: Optional[Dict] = None) -> List[Dict]:
    """Follow Learn's ``paging.nextPage`` links and return the concatenated results."""
    params = dict(params or {})
    params.setdefault("limit", PAGE_LIMIT)
    results: List[Dict] = []
    next_url: Optional[str] = url

    while next_url:
        payload = _get(BbRouter, next_url, params=params)
        results.extend(payload.get("results", []))
        next_page = payload.get("paging", {}).get("nextPage")
        next_url = urljoin(NTULEARN_URL, next_page) if next_page else None
        # nextPage already carries offset/limit in its query string.
        params = None

    return results


def is_available(BbRouter: str) -> bool:
    """Cheap probe: can we list our own courses over REST?"""
    try:
        _get(BbRouter, REST_VERSION_URL)
        _get(BbRouter, REST_MY_COURSES_URL, params={"limit": 1})
        return True
    except (RestUnavailable, requests.RequestException, ValueError):
        return False


def get_favorite_courses(BbRouter: str) -> List[Tuple[str, str]]:
    """Return [(course name, course id)] for courses starred as Favourites in Ultra.

    Uses Ultra's internal memberships endpoint because the public API exposes no
    favourite flag. That endpoint is undocumented and could change without notice, so
    failures here are raised rather than quietly falling back to every enrolment -
    silently widening the scope from 7 courses to 41 would be worse than an error.
    """
    memberships = _get_paged(
        BbRouter,
        REST_INTERNAL_MEMBERSHIPS_URL,
        params={"expand": "course", "favorite": "true"},
    )

    courses: List[Tuple[str, str]] = []
    seen = set()
    for membership in memberships:
        # Defensive: only trust rows the server actually flagged.
        if membership.get("favorite") is False:
            continue
        course = membership.get("course") or {}
        course_pk = course.get("id") or membership.get("courseId")
        if not course_pk or course_pk in seen:
            continue
        seen.add(course_pk)
        courses.append((course.get("name") or course.get("courseId") or course_pk, course_pk))

    if not courses:
        raise RestUnavailable(
            "No Favourites found. Star the courses you want in NTULearn "
            "(Courses page -> the star on each card), or run with --all_courses."
        )
    return courses


def get_courses(BbRouter: str, favorites_only: bool = True) -> List[Tuple[str, str]]:
    """Return [(course name, course id)] for the user's courses.

    By default this is restricted to Ultra Favourites. Pass ``favorites_only=False``
    for every enrolment.

    Course ids are returned in Learn's primary key form (``_123_1``), which is what the
    other REST endpoints - and ``listContent.jsp`` - expect.
    """
    if favorites_only:
        return get_favorite_courses(BbRouter)

    memberships = _get_paged(
        BbRouter, REST_MY_COURSES_URL, params={"expand": "course", "fields": "courseId,course"}
    )

    courses: List[Tuple[str, str]] = []
    seen = set()
    for membership in memberships:
        course = membership.get("course") or {}
        course_pk = course.get("id") or membership.get("courseId")
        if not course_pk or course_pk in seen:
            continue
        # course.name is the display name; courseId is the human course code.
        name = course.get("name") or course.get("courseId") or course_pk
        if course.get("availability", {}).get("available") == "Disabled":
            continue
        seen.add(course_pk)
        courses.append((name, course_pk))
    return courses


def _docs_from_item(
    BbRouter: str, course_id: str, item: Dict, cache=None
) -> List[Doc]:
    """Attachments for one content item, served from cache when it has not changed."""
    content_id = item["id"]
    modified = item.get("modified")

    attachments = None
    if cache is not None:
        attachments = cache.get_attachments(course_id, content_id, modified)

    if attachments is None:
        payload = _get(
            BbRouter,
            REST_CONTENT_ATTACHMENTS_URL.format(
                course_id=course_id, content_id=content_id
            ),
        )
        attachments = [
            {"id": a["id"], "fileName": a.get("fileName")}
            for a in payload.get("results", [])
            if a.get("id")
        ]
        if cache is not None:
            cache.put_attachments(course_id, content_id, modified, attachments)

    return [
        Doc(
            name=a.get("fileName") or a["id"],
            link=REST_ATTACHMENT_DOWNLOAD_URL.format(
                course_id=course_id, content_id=content_id, attachment_id=a["id"]
            ),
            filename=a.get("fileName"),
            modified=modified,
        )
        for a in attachments
    ]


def get_download_dir(
    BbRouter: str, course_name: str, course_id: str, cache=None
) -> Tuple[Dict, List[str]]:
    """Build the download tree for a course over REST.

    Uses ``?recursive=true`` so the entire content tree arrives in one request instead
    of one request per folder, then rebuilds the hierarchy locally from ``parentId``.
    Attachment lookups are served from ``cache`` for items whose ``modified`` stamp is
    unchanged, so an untouched course costs a single request.

    Returns (serialized Folder dict, list of titles skipped as externally hosted).
    """
    items = _get_paged(
        BbRouter,
        REST_COURSE_CONTENTS_URL.format(course_id=course_id),
        params={"recursive": "true", "limit": PAGE_LIMIT},
    )
    if not items:
        raise RestUnavailable(
            "REST API returned no content for course {}".format(course_id)
        )

    by_parent: Dict[Optional[str], List[Dict]] = {}
    for item in items:
        by_parent.setdefault(item.get("parentId"), []).append(item)
    for siblings in by_parent.values():
        siblings.sort(key=lambda i: i.get("position", 0))

    # Top-level items hang off the course's own root content id, which is not itself
    # one of the returned items.
    item_ids = {i["id"] for i in items}
    roots = [i for i in items if i.get("parentId") not in item_ids]

    if cache is not None:
        cache.prune(course_id, item_ids)

    skipped: List[str] = []

    def build(item: Dict):
        title = (item.get("title") or item.get("id") or "").strip()
        handler = (item.get("contentHandler") or {}).get("id", "")

        if item.get("availability", {}).get("available") == "No":
            return None
        if handler in EXTERNAL_HANDLERS:
            skipped.append(title)
            return None
        if handler in ACTIVITY_HANDLERS:
            return None

        children: List[object] = []
        for sub in by_parent.get(item["id"], []):
            built = build(sub)
            if built is not None:
                children.append(built)

        if handler in DOCUMENT_HANDLERS:
            children.extend(_docs_from_item(BbRouter, course_id, item, cache=cache))

        if not children:
            return None
        if handler in DOCUMENT_HANDLERS and len(children) == 1 and isinstance(children[0], Doc):
            return children[0]
        return Folder(name=title, link=None, details="", children=children)

    children = []
    for root in roots:
        built = build(root)
        if built is not None:
            children.append(built)

    folder = Folder(
        name=course_name,
        link=None,
        details="Top level folder for {}. Generated by NTULearn Downloader".format(
            course_name
        ),
        children=children,
    )
    return folder.serialize(BbRouter), skipped
