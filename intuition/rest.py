"""Blackboard Learn public REST API client.

This is the primary content backend. It works for both Ultra and Original course views,
unlike the HTML scraping in ``parsing.py`` which only understands the Original view's
``listContent.jsp`` markup.

Authentication reuses the ``BbRouter`` session cookie: Learn's own Ultra frontend calls
these same endpoints with that cookie, so a browser-obtained token is sufficient and no
OAuth application registration is required.
"""
import hashlib
import json
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from intuition.auth import parse_bbrouter
from intuition.utils import REQUEST_TIMEOUT
from intuition.constants import (
    DOCUMENT_HANDLERS,
    FOLDER_HANDLERS,
    NTULEARN_URL,
    REST_ATTACHMENT_DOWNLOAD_URL,
    REST_CALENDAR_ITEMS_URL,
    REST_CONTENT_ATTACHMENTS_URL,
    REST_CONTENT_CHILDREN_URL,
    REST_COURSE_CONTENTS_URL,
    REST_INTERNAL_MEMBERSHIPS_URL,
    REST_ME_URL,
    REST_MY_COURSES_URL,
    REST_VERSION_URL,
)
from intuition.models import Doc, Folder, RecordedLecture

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


class RestSessionExpired(RestUnavailable):
    """The session token itself was rejected (401), not just this endpoint.

    Kept distinct from its parent so callers can skip the Original-view scraper
    fallback: that scraper authenticates with the same BbRouter cookie, so retrying
    against it after a 401 cannot succeed - it just silently returns an empty
    result and hides the fact that re-authentication is needed.
    """


class RestForbidden(RestUnavailable):
    """A single resource was refused (403) while the session itself is still good.

    Kept distinct from its parent so a per-item refusal can be skipped without
    condemning the whole course, while callers that only care that a request failed
    keep working unchanged.
    """


# One pooled session for the whole process. A full scan issues a few hundred small
# sequential requests; without pooling each one paid a fresh TLS handshake, which is
# both slow and the kind of burst that gets a client throttled.
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            backoff_factor=0.5,
            # 429 and the 5xx family are transient by definition. 403 is deliberately
            # absent: Learn uses it for genuine permission decisions, and retrying
            # those just multiplies the load that may have caused the throttle.
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
    ),
)


def _headers(BbRouter: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "iNTUition",
        "X-Requested-With": "XMLHttpRequest",
    }
    xsrf = parse_bbrouter(BbRouter).get("xsrf")
    if xsrf:
        headers["X-Blackboard-XSRF"] = xsrf
    return headers


def _get(BbRouter: str, url: str, params: Optional[Dict] = None) -> Dict:
    response = _SESSION.get(
        url, headers=_headers(BbRouter), cookies={"BbRouter": BbRouter}, params=params,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 401:
        raise RestSessionExpired(
            "REST API rejected the session token (401). It has likely expired."
        )
    if response.status_code == 403:
        raise RestForbidden("REST API denied access (403) to {}".format(url))
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


def get_todo_items(BbRouter: str, courses: List[Dict], days: int = 7) -> List[Dict]:
    """Return upcoming items from the same course calendars that feed Learn To Do.

    Active Neural Queries is an immediate-attention queue, so only entries due from now
    through the next seven days are requested. Query each in-scope course separately:
    besides matching the dashboard's semester/favourites scope, this avoids the broad
    all-calendars request that Anthology explicitly discourages.
    """
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=max(1, min(int(days), 7)))
    window = {"since": now.isoformat(), "until": until.isoformat()}
    found: List[Dict] = []
    seen = set()
    for course in courses:
        course_id = str(course.get("id") or "")
        if not course_id:
            continue
        params = dict(window, courseId=course_id)
        for item in _get_paged(BbRouter, REST_CALENDAR_ITEMS_URL, params):
            item_id = str(item.get("id") or "")
            title = str(item.get("title") or "").strip()
            if not item_id or not title or item_id in seen:
                continue
            seen.add(item_id)
            props = item.get("dynamicCalendarItemProps") or {}
            found.append({
                "source_id": item_id,
                "title": title,
                "course": str(course.get("name") or item.get("calendarName") or ""),
                "due": str(item.get("end") or item.get("start") or ""),
                "kind": str(props.get("eventType") or item.get("type") or ""),
            })
    return sorted(found, key=lambda item: item.get("due") or "")


def is_available(BbRouter: str) -> bool:
    """Cheap probe: can we list our own courses over REST?"""
    try:
        _get(BbRouter, REST_VERSION_URL)
        _get(BbRouter, REST_MY_COURSES_URL, params={"limit": 1})
        return True
    except (RestUnavailable, requests.RequestException, ValueError):
        return False


def get_me(BbRouter: str) -> Dict:
    """Who this session belongs to.

    NTU populates these fields unusually: ``name.given`` holds the school code
    (e.g. "SPMS") and ``name.family`` holds the person's full name. Verified live.
    So the display name is taken from ``family``, with ``given`` surfaced separately
    as the unit rather than glued on the front.
    """
    me = _get(BbRouter, REST_ME_URL)
    if not me:
        raise RestUnavailable("Could not read the signed-in user")

    name = me.get("name") or {}
    family = (name.get("family") or "").strip()
    given = (name.get("given") or "").strip()
    username = (me.get("userName") or "").strip()

    display = family or given or username or "unknown"
    roles = [r for r in (me.get("institutionRoleIds") or []) if r]
    # NTU_All / NTU_Student are plumbing; STUDENT and the like are the useful ones.
    role = next((r for r in roles if not r.upper().startswith("NTU_")), roles[0] if roles else "")

    return {
        "display": display,
        "unit": given,
        "username": username,
        "role": role.replace("_", " ").title() if role else "",
        "id": me.get("id"),
    }


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
            "No Favourites found. Star the courses you want in iNTUition "
            "(Courses page -> the star on each card), or drop --scope favourites."
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


def _docs_from_body(item: Dict) -> List[Doc]:
    """Files embedded in an Ultra document body rather than its attachments API.

    SC2002 uses Blackboard's editor ``data-bbfile`` links for nearly all slides. The
    public attachment endpoint truthfully returns an empty list for those documents,
    so ignoring the body made a successful scan look like an empty course.
    """
    body = item.get("body") or ""
    if "bbcswebdav" not in body:
        return []
    out, seen = [], set()
    for anchor in BeautifulSoup(body, "lxml").find_all("a", href=True):
        href = anchor.get("href", "")
        if "/bbcswebdav/" not in href or "ntulearn.ntu.edu.sg" not in href:
            continue
        # Query signatures may differ for the same resource; xid is the stable part.
        identity = href.split("?", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        meta = {}
        try:
            meta = json.loads(anchor.get("data-bbfile") or "{}")
        except (TypeError, ValueError):
            pass
        name = (meta.get("linkName") or anchor.get_text(" ", strip=True)
                or item.get("title") or "embedded file").strip()
        mime = meta.get("mimeType") or ""
        if not os.path.splitext(name)[1] and mime:
            name += mimetypes.guess_extension(mime) or ""
        out.append(Doc(name=name, filename=name, link=href,
                       modified=item.get("modified")))
    return out


_XID = re.compile(r"/xid-([\w.-]+)")


def _resource_token(link: str) -> str:
    """A short stable id for one Learn resource, taken from its ``xid`` when present."""
    match = _XID.search(link or "")
    if match:
        return match.group(1)
    return hashlib.sha256((link or "").encode("utf-8")).hexdigest()[:8]


def _uniquify_doc_names(docs: List[Doc]) -> List[Doc]:
    """Give same-named files under one item distinct filenames.

    An Ultra document can link two genuinely different uploads under one display
    name - verified live in SC2002, where "Source code" links two separate
    CallingMethods.java resources (xid-64191670_1 and xid-64191673_1). Both resolved
    to the same local path, so one silently overwrote the other while the plan
    counted the file twice and downloaded it twice.

    The first occurrence keeps its name, so paths already recorded in the Drive
    ledger are left alone; only the later duplicates take a resource-id suffix.
    """
    seen = set()
    for doc in docs:
        name = doc.filename or doc.name
        if name not in seen:
            seen.add(name)
            continue
        stem, extension = os.path.splitext(name)
        unique = "{}-{}{}".format(stem, _resource_token(doc.link), extension)
        while unique in seen:
            unique = "{}-{}{}".format(
                stem, hashlib.sha256(unique.encode("utf-8")).hexdigest()[:8], extension
            )
        seen.add(unique)
        doc.name = unique
        doc.filename = unique
    return docs


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
    forbidden: List[str] = []

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
            docs: List[Doc] = []
            try:
                docs.extend(_docs_from_item(BbRouter, course_id, item, cache=cache))
            except RestForbidden:
                # An item this account may not read: a date-released file, a
                # group-restricted handout. Treat it like the 400/404 sub-resource
                # cases in _get and carry on. Aborting here failed the whole course,
                # and the caller's fallback to the Original-view scraper returns
                # nothing for an Ultra course - so one restricted file was enough to
                # make an entire course look empty.
                forbidden.append(title)
            docs.extend(_docs_from_body(item))
            # Across both sources, so an attachment and a body link sharing a name
            # cannot collide either.
            children.extend(_uniquify_doc_names(docs))

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
        details="Top level folder for {}. Generated by iNTUition".format(
            course_name
        ),
        children=children,
    )
    if forbidden:
        # Reported alongside the externally-hosted ones: from the user's side both
        # mean "this item yielded no files", and both are worth seeing.
        skipped.extend("{} (access denied)".format(t) for t in forbidden)
    return folder.serialize(BbRouter), skipped
