import json
import os
import re
from typing import List, Optional, Tuple, Union, Dict
from urllib.parse import parse_qs, urlencode, urlparse

import bs4
import requests
from bs4 import BeautifulSoup


from intuition.constants import (
    GET_CONTENT_IDS_URL,
    GET_CONTENT_LIST_URL,
    GET_COURSES_URL,
    LOGINFS_HOSTNAME,
    LOGINFS_URL,
    NTULEARN_AUTH_SAML_URL,
    NTULEARN_URL,
    SAML_SSO_URL,
)
from intuition import semester
from intuition.auth import AuthenticationError, HOW_TO_GET_TOKEN
from intuition.utils import (
    REQUEST_TIMEOUT,
    get_content_id_from_listContent_url,
    is_download_link,
    make_GET_request,
)
from intuition.models import MODEL_TYPES, to_model, Folder
from intuition.parsing import (
    parse_content_page,
    parse_recorded_lecture_contents,
)


def authenticate(username: str, password: str) -> str:
    """DEPRECATED - NTU no longer supports scripted username/password authentication.

    As of the migration to Blackboard Learn SaaS, ntulearn.ntu.edu.sg federates to
    Microsoft Entra ID (login.microsoftonline.com) rather than ADFS
    (loginfs.ntu.edu.sg), and Entra enforces MFA. The ADFS form POST this function
    performed no longer exists in the login chain, so there is nothing to fix here -
    the flow itself is obsolete.

    Use ``intuition.auth.resolve`` with a browser-obtained BbRouter cookie
    instead. Raises AuthenticationError with instructions.
    """
    raise AuthenticationError(
        "Username/password login is no longer supported by iNTUition.\n\n"
        + HOW_TO_GET_TOKEN
    )


def _authenticate_adfs_legacy(username: str, password: str) -> str:
    """Historical ADFS SSO flow, retained for reference only. Does not work against
    the current iNTUition deployment.

    Hit the following endpoints:
    1. GET https://loginfs.ntu.edu.sg/adfs/ls/ to get blank BbRouter
    2. GET https://ntulearn.ntu.edu.sg/auth-saml/saml/login?apId=_140_1&redirectUrl=https%3A%2F%2Fntulearn.ntu.edu.sg%2Fwebapps%2Fportal%2Fexecute%2FdefaultTab'
        to get session cookies
    3. GET https://loginfs.ntu.edu.sg/adfs/ls/?SAMLRequest=<from redirect url> to SAML parameters
    4. GET https://loginfs.ntu.edu.sg/adfs/ls/ to get login form and client-request-id
    5. POST https://loginfs.ntu.edu.sg/adfs/ls/ with SAML params and login credentials to get SAML Response
    6. POST https://ntulearn.ntu.edu.sg/auth-saml/saml/SSO with SAML Response to get authenticated BbRouter

    Arguments:
        username {str} -- username including domain name (e.g. username@example.invalid)
        password {str} -- password

    Returns:
        str -- BbRouter token of format: 
        expires:{int},id:{str},signature:{str},site:{str},timeout:{int},user:{str},v:{int},xsrf:{str}
        If there is no user field then authentication has failed
    """
    sess = requests.Session()
    # endpoint 1
    __ntulearn(sess)

    if sess.cookies.get("BbRouter") is None:
        raise Exception("Expected BbRouter in returned cookies")

    # endpoint 2
    saml_response = __ntulearn_auth_saml(sess)
    login_url = saml_response.url
    parsed = urlparse(login_url)
    saml_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    saml_params

    # endpoint 3
    loginfs_response = __loginfs(sess, saml_params)
    soup = BeautifulSoup(loginfs_response.content.decode(), features="lxml")
    form = soup.find_all("form")[0]
    form_url = form.get("action")
    parsed_form_url = urlparse(form_url)
    saml_params = {
        k: v[0] for k, v in parse_qs(parsed_form_url.query).items()
    }  # overwrite saml_params

    # endpoint 4
    auth_response = __post_loginfs(sess, username, password, saml_params, login_url)
    soup = BeautifulSoup(auth_response.content.decode(), features="lxml")
    SAMLResponse = soup.find_all("input")[0].get("value")
    referer = login_url + "&client-request-id=" + saml_params["client-request-id"]

    # endpoint 5
    __ntulearn_SSO(sess, referer, SAMLResponse)

    # check that the BbRouter is authenticated
    BbRouter: str = sess.cookies.get("BbRouter")
    if "user" not in BbRouter:
        raise Exception(
            "Bbrouter: {} does not have user field, it is not authenticated".format(
                BbRouter
            )
        )

    return BbRouter


# How the course list is narrowed. There is deliberately no "everything" option: a
# sync tool that can be pointed at every enrolment you have ever had is one mis-click
# away from dragging years of stale material into Drive.
SCOPE_SEMESTER = "semester"      # courses labelled with the semester in progress today
SCOPE_FAVOURITES = "favourites"  # courses starred in Ultra
SCOPES = (SCOPE_SEMESTER, SCOPE_FAVOURITES)


def load_excluded_courses(download_root: str = ".") -> List[str]:
    """Load list of course patterns to exclude from sync."""
    filepath = os.path.join(download_root, ".intuition", "excluded_courses.json")
    if os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    # No course-specific exclusions are assumed for a new installation
    return []


def save_excluded_courses(excluded: List[str], download_root: str = "."):
    """Persist excluded course patterns."""
    folder = os.path.join(download_root, ".intuition")
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, "excluded_courses.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(excluded, f, indent=2)


def is_course_excluded(course_name: str, download_root: str = ".") -> bool:
    """Return True if course_name matches any excluded course pattern."""
    excluded = load_excluded_courses(download_root)
    norm_name = course_name.upper()
    for rule in excluded:
        rule_upper = rule.upper().strip()
        if not rule_upper:
            continue
        if rule_upper in norm_name:
            return True
        tokens = [t.strip("()[]") for t in rule_upper.replace("-", " ").split() if t.strip("()[]")]
        if tokens and len(tokens) >= 2 and all(t in norm_name for t in tokens):
            return True
    return False


def get_courses(
    BbRouter: str, prefer_rest: bool = True, favorites_only: bool = None,
    scope: str = SCOPE_SEMESTER,
    today=None, include_undated: bool = False,
    download_root: str = ".",
) -> List[Tuple[str, str]]:
    """Return the courses in scope, as [(course name, course_id)].

    Scope defaults to the semester currently in progress, derived from today's date and
    the semester label in each course name. That stays correct on its own as terms roll
    over, unlike Ultra's Favourites star which has to be re-curated by hand.

    Arguments:
        BbRouter {str} -- authentication token
        prefer_rest {bool} -- set False to force the legacy HTML scraper
        scope {str} -- "semester" (default), "favourites", or "all"
        today {date} -- override the reference date, for testing
        include_undated {bool} -- under semester scope, also keep courses whose name
            states no semester at all (admin and compliance modules, mostly)
        favorites_only {bool} -- deprecated alias; True maps to scope="favourites"

    Returns:
        List[Tuple[str, str]] -- list of tuples (course name, course_id)
    """
    # Back-compat for the previous boolean flag. False no longer means "everything";
    # it simply leaves the default semester scope in place.
    if favorites_only is True:
        scope = SCOPE_FAVOURITES
    if scope not in SCOPES:
        raise ValueError("Unknown scope {!r}, expected one of {}".format(scope, SCOPES))

    def narrow(courses):
        kept = []
        for name, cid in courses:
            if is_course_excluded(name, download_root=download_root):
                continue
            if scope != SCOPE_SEMESTER:
                kept.append((name, cid))
                continue
            parsed = semester.parse_course_semester(name)
            if parsed == semester.current_semester(today):
                kept.append((name, cid))
            elif parsed is None and include_undated:
                kept.append((name, cid))
        return kept

    if prefer_rest:
        from intuition import rest

        try:
            # Semester scope needs the full enrolment list to filter from; only the
            # favourites scope asks the server to narrow it.
            courses = rest.get_courses(
                BbRouter, favorites_only=(scope == SCOPE_FAVOURITES))
            if courses:
                narrowed = narrow(courses)
                if not narrowed and scope == SCOPE_SEMESTER:
                    raise AuthenticationError(
                        "No courses are labelled {} (the semester in progress). "
                        "Checked {} enrolment(s). Use --scope favourites if your "
                        "courses are named differently, or --include_undated to keep "
                        "unlabelled ones.".format(
                            semester.format_semester(semester.current_semester(today)),
                            len(courses)))
                return narrowed
        except rest.RestSessionExpired as e:
            # The scraper authenticates with this same BbRouter cookie, so retrying
            # against it cannot succeed - it would just return an empty course list
            # and hide the fact that the token needs to be refreshed.
            raise AuthenticationError(
                "Your NTU Learn session token was rejected ({}).\n\n{}"
                .format(e, HOW_TO_GET_TOKEN))
        except (rest.RestUnavailable, requests.RequestException, ValueError) as e:
            if scope == SCOPE_FAVOURITES:
                # The scraper has no notion of a starred course, so falling back would
                # silently return every enrolment instead of the chosen few.
                raise AuthenticationError(
                    "Could not read your Favourites ({}).\n"
                    "Drop --scope favourites to use the current semester instead."
                    .format(e))
            print("REST course listing unavailable ({}), falling back to scraper".format(e))

    if scope == SCOPE_FAVOURITES and not prefer_rest:
        raise AuthenticationError(
            "--legacy cannot read Favourites: the Original-view scraper does not "
            "expose them. Drop --scope favourites, or drop --legacy.")

    # The scraper is reached only under semester scope, where narrow() applies the same
    # filter to whatever it returns - so this fallback cannot widen the scope.
    return narrow(get_courses_legacy(BbRouter))


def get_courses_legacy(BbRouter: str) -> List[Tuple[str, str]]:
    """Scrape the Original-view global course nav menu.

    Only returns anything on installs that still expose the Original base navigation.
    """
    cookies = {"BbRouter": BbRouter}
    headers = {
        "Connection": "keep-alive",
        "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
        "X-Prototype-Version": "1.7",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.113 Safari/537.36",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept-Language": "en-US,en;q=0.9",
    }

    params = (
        ("cmd", "view"),
        ("serviceLevel", "blackboard.data.course.Course$ServiceLevel:FULL"),
    )
    response = requests.get(
        GET_COURSES_URL,
        headers=headers,
        params=params,  # type: ignore
        cookies=cookies,
        timeout=REQUEST_TIMEOUT,
    )

    # parse response
    soup = BeautifulSoup(response.content, features="lxml")
    links = soup.find_all("a")

    courses: List[Tuple[str, str]] = []
    for link in links:
        name = link.contents[0]
        if isinstance(name, bs4.element.Tag):
            name = name.text
        # expect fullLink to be of form:
        # link javascript:globalNavMenu.goToUrl('/webapps/blackboard/execute/launcher?type=Course&id=_302242_1&url='); return false;
        fullLink = link.get("onclick")
        if not fullLink:
            # Ultra base navigation renders plain hrefs with no onclick handler.
            continue
        matches = re.search(r"type=Course&id=_(\S+)&url=", fullLink)
        if matches is None:
            print("Unable to parse link to get course id: {}".format(fullLink))
            continue
        groups = matches.groups()
        if len(groups) != 1:
            print("Unable to parse link to get course id: {}".format(fullLink))
            continue
        course_id = groups[0]
        courses.append((name, course_id))
    return courses


def get_content_ids(BbRouter: str, course_id: str) -> List[Tuple[str, str]]:
    """returns list of tuples of content name and content ids associated to the course_id

    Arguments:
        BbRouter {str} -- authentication token
        course_id {str} -- course id

    Returns:
        List[Tuple[str, str]] -- list of tuple (content name, content_id)
    """
    params = (
        ("method", "search"),
        ("context", "course_entry"),
        ("course_id", course_id),
    )
    response = make_GET_request(BbRouter, GET_CONTENT_IDS_URL, params)

    soup = BeautifulSoup(response.content.decode(), features="lxml")
    ll = soup.find("ul", {"id": "courseMenuPalette_contents"})
    result: List[Tuple[str, str]] = []
    for c in ll:
        a = c.find("a")
        if a is None:
            continue
        url = a.get("href")
        name = a.text
        content_id = get_content_id_from_listContent_url(url)
        if content_id:
            result.append((name, content_id))
    return result


def get_contents(
    BbRouter: str, course_id: str, content_id: str
) -> List[MODEL_TYPES]:
    # NOTE e.g. "course_id": "_306327_1", "content_id": "_1790226_1"
    soup = make_get_contents_request(BbRouter, course_id, content_id)
    children = [to_model(c) for c in parse_content_page(soup)]
    return children


def make_get_contents_request(
    BbRouter: str, course_id: str, content_id: str
) -> BeautifulSoup:
    params = (("course_id", course_id), ("content_id", content_id))
    response = make_GET_request(BbRouter, GET_CONTENT_LIST_URL, params)
    soup = BeautifulSoup(response.content.decode(), features="lxml")
    return soup


def get_recorded_lecture_contents(BbRouter: str, link: str) -> str:
    """get html of AcuStudio

    Arguments:
        BbRouter {str} -- authentication token
        link {str} -- predownload link to AcuStudio

    Returns:
        str -- html of page
    """
    response = make_GET_request(BbRouter, NTULEARN_URL + link)
    return response.content.decode()


def get_recorded_lecture_download_link(BbRouter: str, predownload_link: str) -> str:
    """get actual mp4 download link from link to Acustudio. This takses a while which is why we 
    defer it until we actially want to download the mp4, hence the need to expose this endpoint

    Arguments:
        BbRouter {str} -- authentication token
        predownload_link {str} -- link to Acustudio

    Returns:
        str -- download link to mp4
    """
    html = get_recorded_lecture_contents(BbRouter, predownload_link)
    return parse_recorded_lecture_contents(html)


def get_file_download_link(BbRouter: str, link: str) -> str:
    """Get the actual download link (contains file name in url)

    Arguments:
        BbRouter {str} -- authentication token
        link {str} -- link

    Returns:
        str -- file download link
    """
    cookies = {"BbRouter": BbRouter}
    headers = requests.head(link, allow_redirects=True, cookies=cookies)
    return headers.url


def get_download_dir(
    BbRouter: str, course_name: str, course_id: str, prefer_rest: bool = True,
    cache=None,
):
    """Return dict with directory structure of downloadable items (documents and lectures).

    Tries the REST API first so that Ultra courses work, and falls back to scraping
    listContent.jsp for legacy Original courses.

    Arguments:
        BbRouter {str} -- authentication token
        course_name {str} -- name of course
        course_id {str} -- course id
        prefer_rest {bool} -- set False to force the legacy HTML scraper
    """
    if prefer_rest:
        from intuition import rest

        try:
            folder, skipped = rest.get_download_dir(
                BbRouter, course_name, course_id, cache=cache
            )
            if skipped:
                print(
                    "  note: {} item(s) yielded no files - externally hosted "
                    "(Zoom/Panopto/Kaltura) or access denied: {}".format(
                        len(skipped), ", ".join(skipped[:5])
                    )
                )
            return folder
        except rest.RestSessionExpired as e:
            # Same cookie, same rejection - the scraper fallback below cannot
            # succeed either, so don't let it mask the auth failure with an
            # empty-looking result.
            raise AuthenticationError(
                "Your NTU Learn session token was rejected ({}).\n\n{}"
                .format(e, HOW_TO_GET_TOKEN))
        except (rest.RestUnavailable, requests.RequestException, ValueError) as e:
            print("  REST listing unavailable ({}), falling back to scraper".format(e))

    return get_download_dir_legacy(BbRouter, course_name, course_id)


def get_download_dir_legacy(BbRouter: str, course_name: str, course_id: str):
    """Original course view scraper (listContent.jsp).

    Arguments:
        BbRouter {str} -- authentication token
        course_name {str} -- name of course
        course_id {str} -- course id

    Returns:
        Dict or JSON dump -- Folder dict object, attributes shown below:

        Folder
        - type: "folder"
        - name: string
        - contents: List[Folder, File, RecordedLecture]

        File
        - type: "file"
        - name: string
        - download_link: string

        RecordedLecture
        - type: "recorded_lecture"
        - name: string
        - predownload_link: string
    """

    content_names_ids = get_content_ids(BbRouter, course_id)
    children = [
        Folder(
            name=content_name,
            link=None,
            details="{} folder. Generated by iNTUition".format(content_name),
            children=get_contents(BbRouter, course_id, content_id),
        )
        for content_name, content_id in content_names_ids
    ]

    folder = Folder(
        name=course_name,
        link=None,
        details="Top level folder for {}. Generated by iNTUition".format(
            course_name
        ),
        children=children,
    )
    return folder.serialize(BbRouter)


def __ntulearn(session):
    headers = {
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://loginfs.ntu.edu.sg/adfs/ls/",
        "Accept-Language": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = session.get(NTULEARN_URL, headers=headers, allow_redirects=True)
    return response


def __ntulearn_auth_saml(session):
    headers = {
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://ntulearn.ntu.edu.sg/",
        "Accept-Language": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = session.get(
        NTULEARN_AUTH_SAML_URL, headers=headers, allow_redirects=True
    )
    return response


def __loginfs(session, saml_params):

    headers = {
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://ntulearn.ntu.edu.sg/",
        "Accept-Language": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    params = (
        ("SAMLRequest", saml_params["SAMLRequest"]),
        ("SigAlg", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"),
        ("Signature", saml_params["Signature"]),
    )

    response = requests.get(LOGINFS_URL, headers=headers, params=params)
    return response


def __post_loginfs(session, username, password, saml_params, login_url):
    headers = {
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Origin": "https://loginfs.ntu.edu.sg",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Referer": login_url,
        "Accept-Language": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    params = (
        ("SAMLRequest", saml_params["SAMLRequest"]),
        ("SigAlg", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"),
        ("Signature", saml_params["Signature"]),
        ("client-request-id", saml_params["client-request-id"]),
    )

    data = {
        "UserName": username,
        "Password": password,
        "AuthMethod": "FormsAuthentication",
    }

    response = session.post(LOGINFS_URL, headers=headers, params=params, data=data)
    return response


def __ntulearn_SSO(session, referer: str, SAMLResponse: str):
    headers = {
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Origin": LOGINFS_HOSTNAME,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": referer,
        "Accept-Language": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    data = {"SAMLResponse": SAMLResponse}

    response = session.post(SAML_SSO_URL, headers=headers, data=data)
    return response
