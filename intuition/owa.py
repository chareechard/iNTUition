"""Read NTU mail out of Outlook Web App, so Inbound has a source of its own.

This is the last piece that lived only in the Cerberus prototype. It is ported
rather than reinvented: the selectors here were calibrated against a live NTU
tenant and the awkward parts - virtualized list scrolling, stale row handles,
OWA's debounced auto-mark-as-read - are exactly the things that are expensive to
rediscover.

What it does *not* do, deliberately: no credentials are ever handled here. A
human completes NTU's SSO and MFA in a real browser window once (``login``), and
what is persisted is the resulting session state - cookies and localStorage -
which later headless runs replay. This process never sees a password or an MFA
code.

Playwright is an optional dependency, imported lazily, so the dashboard and the
rest of the package run without it installed. Only ``login`` and ``Mailbox``
need it.

Known fragility, stated plainly: this reads a rendered UI. A tenant on a
different OWA deploy ring can render a materially different DOM, and then the
selectors below need adjusting against a live inbox with devtools. That is the
cost of this approach; the alternative is a Graph API app registration.
"""
import os
import re
import sys
import time
from datetime import date
from typing import Any, Dict, List, Optional

STORAGE_DIR = ".intuition"
SESSION_FILENAME = "owa_session.json"

DEFAULT_OWA_URL = "https://outlook.office.com/mail/"
OFFICE_LOGIN_URL = "https://office.com"

LIST_ITEM_SELECTOR = 'div[role="option"]'
UNREAD_ATTR_HINT = "unread"     # substring OWA embeds in aria-label for unread rows
# OWA prefixes pinned rows' aria-label with "Pinned " and keeps them stuck to the
# top regardless of date, so they are never representative of a date window.
PINNED_ATTR_HINT = "pinned"
# OWA renders an explicit M/D/YYYY in the aria-label once a message is no longer
# from today; today's rows show a bare time ("11:38 AM"). Presence of this
# pattern therefore means the row is NOT from today.
_EXPLICIT_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")

# Any one of these means we are inside a rendered inbox, not the portal or a
# login page.
INBOX_READY_SELECTORS = [
    '[data-app-css="OutlookMail"]',
    "div#MainModule",
    'div[role="main"][aria-label*="Message list"]',
    'div[aria-label="Folder pane"]',
]
INBOX_SHELL_SELECTOR = 'div[role="main"], div#MainModule, [data-app-css="OutlookMail"]'

READING_PANE_SELECTOR = ('div[aria-label="Reading Pane"], '
                         'div[role="region"][aria-label*="Reading"]')
READING_PANE_BODY_SELECTOR = 'div[aria-label="Message body"][role="document"]'
# Field selectors inside the Reading Pane. Semantic id-suffix / data-testid hooks
# are preferred over hashed CSS classes ("TtcXM", "JdFsz"), which churn across
# deploy rings while these held up across messages on a live tenant.
#
# Subject is tried in order and the first non-empty answer wins. The nested form
# came from the prototype and matches nothing on the ring this tenant is on now -
# the subject element carries `title` itself rather than wrapping a child that
# does - which is why every scraped row used to arrive with an empty subject.
# Keeping both means whichever shape a ring renders, one of them lands.
READING_PANE_SUBJECT_SELECTORS = ('[id*="_SUBJECT"] [title]', '[id*="_SUBJECT"]')
READING_PANE_SENDER_SELECTOR = '[id$="_FROM"]'
READING_PANE_TIMESTAMP_SELECTOR = 'div[data-testid="SentReceivedSavedTime"]'

_SCROLL_GROWTH_TIMEOUT_MS = 5000
_MAX_STALL_RETRIES = 8


class SessionExpired(RuntimeError):
    """The saved session no longer yields an authenticated inbox."""


def session_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, SESSION_FILENAME)


def linked(download_root: str) -> bool:
    """Whether a saved session exists. Not whether it is still valid."""
    return os.path.isfile(session_path(download_root))


def _browsers_path() -> str:
    """Where ``playwright install`` leaves browsers for this user.

    Mirrors the driver's own default: a per-platform cache directory plus
    ``ms-playwright``.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ms-playwright")


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SessionExpired(
            "Playwright is not installed. Run:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium")
    # Under PyInstaller, Playwright's transport forces PLAYWRIGHT_BROWSERS_PATH=0, which
    # points its registry at .local-browsers inside the bundle. The packaged app ships
    # the Node driver but deliberately not the browsers - chromium alone is over 400MB -
    # so send the registry back to the per-user directory the unfrozen install uses.
    # setdefault on both sides, so an explicit override from the environment still wins.
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())
    return sync_playwright


_STALE = None


def _stale_row_error():
    """The exception class a recycled/detached row handle raises.

    Resolved lazily so this module imports without Playwright; falls back to a
    private sentinel rather than ``Exception``, so a missing Playwright can never
    turn a real bug into a silently skipped row.
    """
    global _STALE
    if _STALE is None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            _STALE = PlaywrightError
        except ImportError:
            class _NoPlaywright(Exception):
                pass
            _STALE = _NoPlaywright
    return _STALE


# --------------------------------------------------------------------------
# Interactive login
# --------------------------------------------------------------------------

def login(download_root: str, owa_url: str = DEFAULT_OWA_URL,
          timeout_s: int = 15 * 60, on_status=None) -> str:
    """Open a real browser, wait for the human to finish SSO, save the session.

    Returns the path the session was written to. Blocks until the inbox renders
    or ``timeout_s`` elapses.
    """
    say = on_status or (lambda m: None)
    sync_playwright = _playwright()
    path = session_path(download_root)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(OFFICE_LOGIN_URL, wait_until="domcontentloaded")
            say("Complete the NTU login in the browser window that opened.")
            if not _wait_for_inbox(page, timeout_s):
                raise SessionExpired(
                    "Timed out waiting for a rendered inbox. Nothing was saved.")
            context.storage_state(path=path)
            say("Session saved to {}".format(path))
        finally:
            browser.close()
    return path


def _wait_for_inbox(page, timeout_s: int) -> bool:
    """Poll for any inbox-ready selector until one appears or time runs out."""
    from playwright.sync_api import TimeoutError as PWTimeout
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for selector in INBOX_READY_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=3000, state="attached")
                return True
            except PWTimeout:
                continue
        time.sleep(2.0)
    return False


# --------------------------------------------------------------------------
# Row-level extraction. These take a page object and nothing else, so they are
# testable against a fake and shared by every scrape mode below.
# --------------------------------------------------------------------------

def row_email_id(row: Any, fallback_index: int) -> str:
    """A stable id for one *message*.

    ``id`` carries the Exchange item id and is distinct per message; ``data-convid``
    identifies the whole conversation, so every message in a thread shares it. The
    prototype preferred the conversation id, which a live backtest caught costing
    real damage: two messages sent a minute apart were classified twice, and since
    the store keys on this, the second would have overwritten the first - losing a
    flagged mail silently. Item id first, conversation id only as a fallback.
    """
    return (row.get_attribute("id") or row.get_attribute("data-convid")
            or "email-{}".format(fallback_index))


def _is_unread(row: Any) -> bool:
    label = (row.get_attribute("aria-label") or "").lower()
    classes = (row.get_attribute("class") or "").lower()
    return UNREAD_ATTR_HINT in label or "unread" in classes


def _is_pinned(row: Any) -> bool:
    return PINNED_ATTR_HINT in (row.get_attribute("aria-label") or "").lower()


def row_date(row: Any) -> Optional[date]:
    """Best-effort date read from the list row, without clicking it.

    No read-state side effect, and far cheaper than opening every row just to
    find out whether it is in range. Returns None for today's rows - OWA omits an
    explicit date for those.
    """
    match = _EXPLICIT_DATE.search(row.get_attribute("aria-label") or "")
    if not match:
        return None
    try:
        month, day, year = (int(p) for p in match.group().split("/"))
        return date(year, month, day)
    except ValueError:
        return None


def _pane_text(pane: Any, selector: str, attribute: Optional[str] = None) -> str:
    if pane is None:
        return ""
    el = pane.query_selector(selector)
    if el is None:
        return ""
    if attribute:
        return (el.get_attribute(attribute) or el.inner_text() or "").strip()
    return (el.inner_text() or "").strip()


def _first_text(pane: Any, selectors, attribute: Optional[str] = None) -> str:
    """First selector that yields text wins, so one dead selector is not fatal."""
    for selector in selectors:
        text = _pane_text(pane, selector, attribute)
        if text:
            return text
    return ""


def extract_email(page: Any, row: Any, index: int,
                  mark_read: bool = False) -> Dict[str, Any]:
    """Click a row to load the Reading Pane and scrape the rendered fields.

    Clicking is the only way to make OWA render a body - there is no read path
    that avoids it. OWA *usually* marks the message read as a side effect, but
    that is tenant-configurable and debounced by a few seconds, so a fast
    headless run can finish before it fires and re-scrape (and re-bill) the same
    mail next time. ``mark_read`` sends OWA's Ctrl+Q instead of trusting it.
    """
    row.click()
    page.wait_for_selector(READING_PANE_SELECTOR, timeout=10000)
    # The pane container mounts before its content does; wait on the body
    # specifically, as it is the heaviest piece and implies the rest has settled.
    page.wait_for_selector(READING_PANE_BODY_SELECTOR, timeout=15000)

    if mark_read:
        page.keyboard.press("Control+q")

    pane = page.query_selector(READING_PANE_SELECTOR)
    return {
        "email_id": row_email_id(row, index),
        "sender": _pane_text(pane, READING_PANE_SENDER_SELECTOR),
        "subject": _first_text(pane, READING_PANE_SUBJECT_SELECTORS, "title"),
        "timestamp": _pane_text(pane, READING_PANE_TIMESTAMP_SELECTOR),
        "body_content": _pane_text(pane, READING_PANE_BODY_SELECTOR),
    }


def _scroll_for_more(page: Any, current_row_count: int) -> bool:
    """Nudge OWA's virtualized list to mount more rows. True if the count grew.

    Both a scroll-into-view and a real wheel event are needed: the list mounts
    more rows off its own scroll-container listener, which
    ``scroll_into_view_if_needed`` alone does not always trigger headless.
    """
    stale = _stale_row_error()
    try:
        rows = page.query_selector_all(LIST_ITEM_SELECTOR)
        if rows:
            rows[-1].scroll_into_view_if_needed()
    except stale:
        pass    # row went stale; the wheel nudge below still helps
    page.mouse.wheel(0, 2400)
    try:
        page.wait_for_function(
            "([sel, prev]) => document.querySelectorAll(sel).length > prev",
            arg=[LIST_ITEM_SELECTOR, current_row_count],
            timeout=_SCROLL_GROWTH_TIMEOUT_MS)
        return True
    except stale:
        return False


def scrape_since(page: Any, cutoff: date, unread_only: bool = True,
                 max_scan: int = 500, max_emails: int = 200,
                 on_progress=None) -> List[Dict[str, Any]]:
    """Scrape rows dated on or after ``cutoff``, newest first, skipping pinned.

    Relies on OWA's default newest-first sort: once a row's date falls before the
    cutoff, every row after it does too, so the scan stops rather than walking
    the whole mailbox. Read and unread rows interleave, but the date stays
    monotonically non-increasing either way, so the same early stop holds when
    ``unread_only`` filters rows in.

    The list is virtualized - only a viewport of rows exists in the DOM - so rows
    below the initial render are reached by nudging and then polling the DOM
    until the count grows. Rows are deduplicated by their stable id, since a
    virtualized list recycles DOM nodes and index-based tracking would not
    survive that.

    ``max_scan`` bounds rows looked at; ``max_emails`` bounds rows actually
    clicked, each of which costs one model call downstream.
    """
    stale = _stale_row_error()
    results = []       # type: List[Dict[str, Any]]
    seen = set()       # type: set
    scanned = 0
    stalls = 0

    while True:
        rows = page.query_selector_all(LIST_ITEM_SELECTOR)
        # Snapshot ids while every handle is still fresh: by the time the loop
        # below reaches a later row, clicks on earlier ones may have reflowed it.
        # Reading the id is itself a DOM call, so a row can already be gone here -
        # that must cost one row, not the whole scan.
        # ``seen`` is only updated once a row is actually reached below, so a batch
        # holding the same id twice would queue it twice - a duplicate click, a
        # duplicate paid classification, and a duplicate store write. Dedupe within
        # the batch as well as against previous ones.
        fresh = []
        batch = set()
        for row in rows:
            try:
                rid = row_email_id(row, scanned)
            except stale:
                continue
            if rid in seen or rid in batch:
                continue
            batch.add(rid)
            fresh.append((row, rid))

        hit_cutoff = False
        for row, rid in fresh:
            if scanned >= max_scan:
                return results
            seen.add(rid)
            scanned += 1
            try:
                if _is_pinned(row):
                    continue
                when = row_date(row)
                if when is not None and when < cutoff:
                    hit_cutoff = True
                    break
                if unread_only and not _is_unread(row):
                    continue
                if on_progress:
                    on_progress(len(results) + 1, max_emails)
                results.append(extract_email(page, row, scanned,
                                             mark_read=unread_only))
            except stale:
                # Virtualization recycled this row's node between the query and
                # here. It is already marked seen; skip it rather than losing the
                # whole scan to one bad row.
                continue
            if len(results) >= max_emails:
                return results

        if hit_cutoff or scanned >= max_scan or not rows:
            return results

        if _scroll_for_more(page, len(rows)):
            stalls = 0
        else:
            stalls += 1
            if stalls >= _MAX_STALL_RETRIES:
                return results


# --------------------------------------------------------------------------
# Session replay
# --------------------------------------------------------------------------

class Mailbox:
    """A headless OWA session replaying the state saved by ``login``.

    Use as a context manager; ``fetch`` is the only thing callers need.
    """

    def __init__(self, download_root: str, owa_url: str = DEFAULT_OWA_URL):
        self.path = session_path(download_root)
        self.owa_url = owa_url
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self):
        from playwright.sync_api import TimeoutError as PWTimeout
        if not os.path.isfile(self.path):
            raise SessionExpired(
                "No saved session at {}. Run:\n"
                "    python -m intuition.triage_run --login".format(
                    self.path))
        sync_playwright = _playwright()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(storage_state=self.path)
        self._page = self._context.new_page()
        self._page.goto(self.owa_url, wait_until="domcontentloaded")

        try:
            self._page.wait_for_selector(INBOX_SHELL_SELECTOR, timeout=20000)
        except PWTimeout:
            self._raise_if_login_page()
            raise SessionExpired(
                "Inbox shell did not render in time; treating the session as invalid.")
        self._raise_if_login_page()

        try:
            self._page.wait_for_selector(LIST_ITEM_SELECTOR, timeout=15000)
        except PWTimeout:
            # An empty inbox is valid. Only a login redirect is an error.
            self._raise_if_login_page()
        return self

    def _raise_if_login_page(self):
        url = (self._page.url if self._page else "").lower()
        if ("login.microsoftonline.com" in url or "login.live.com" in url
                or "/oauth2/" in url):
            raise SessionExpired(
                "Session expired: redirected to the NTU login page ({}). Re-run "
                "--login to refresh it.".format(url))

    def fetch(self, cutoff: date, unread_only: bool = True,
              max_emails: int = 200, on_progress=None) -> List[Dict[str, Any]]:
        if self._page is None:
            raise RuntimeError("open() must be called before fetch()")
        return scrape_since(self._page, cutoff, unread_only=unread_only,
                            max_emails=max_emails, on_progress=on_progress)

    def close(self):
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = self._page = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
