"""Row-level OWA extraction, against a fake page.

Playwright is not installed in CI and a live tenant is not reproducible, so what
is pinned here is the logic that decides *which* rows get opened and what comes
out of them - the parts that cost money or lose mail when they are wrong. The
selectors themselves can only be verified against a real inbox.
"""
import os
import unittest
from datetime import date
from tempfile import TemporaryDirectory

from intuition import owa


class FakeElement:
    def __init__(self, text="", attrs=None):
        self._text = text
        self._attrs = attrs or {}

    def get_attribute(self, name):
        return self._attrs.get(name)

    def inner_text(self):
        return self._text


class FakeRow:
    """A message-list row. ``stale`` makes it behave like a recycled DOM node."""

    def __init__(self, rid, label="", classes="", stale=False, body="body"):
        self.rid = rid
        self._attrs = {"data-convid": rid, "aria-label": label, "class": classes}
        self.stale = stale
        self.body = body
        self.clicked = False

    def get_attribute(self, name):
        if self.stale:
            raise owa._stale_row_error()("row is not attached to the DOM")
        return self._attrs.get(name)

    def click(self):
        self.clicked = True

    def scroll_into_view_if_needed(self):
        pass


class FakePage:
    # Which subject selector this tenant's deploy ring answers. The prototype's
    # nested form matched nothing on a live tenant, so the default here is the
    # shape that actually shipped.
    subject_selector = '[id*="_SUBJECT"]'

    def __init__(self, rows):
        self.rows = rows
        self.opened = []
        self.marked_read = 0
        self._current = None

    def subject_el(self, selector, row):
        if selector != self.subject_selector:
            return None
        return FakeElement("", {"title": "Subject for " + row.rid})

    # -- list ------------------------------------------------------------
    def query_selector_all(self, selector):
        return list(self.rows)

    # -- reading pane ----------------------------------------------------
    def wait_for_selector(self, selector, timeout=None, state=None):
        return None

    def query_selector(self, selector):
        if selector == owa.READING_PANE_SELECTOR:
            return self
        row = self._current
        if selector == owa.READING_PANE_BODY_SELECTOR:
            return FakeElement(row.body)
        if selector in owa.READING_PANE_SUBJECT_SELECTORS:
            return self.subject_el(selector, row)
        if selector == owa.READING_PANE_SENDER_SELECTOR:
            return FakeElement("spms@ntu.edu.sg")
        if selector == owa.READING_PANE_TIMESTAMP_SELECTOR:
            return FakeElement("Mon 10/08/2026 09:14")
        return None

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        def press(self, keys):
            self.page.marked_read += 1

    class _Mouse:
        def wheel(self, dx, dy):
            pass

    @property
    def keyboard(self):
        return FakePage._Keyboard(self)

    @property
    def mouse(self):
        return FakePage._Mouse()

    def wait_for_function(self, *a, **kw):
        # The list never grows: every fake page renders all its rows at once.
        raise owa._stale_row_error()("no growth")


def _patched_extract(page):
    """extract_email needs to know which row the pane is showing."""
    original = owa.extract_email

    def extract(pg, row, index, mark_read=False):
        pg._current = row
        pg.opened.append(row.rid)
        return original(pg, row, index, mark_read=mark_read)
    return extract


class TestRowFields(unittest.TestCase):
    def test_the_id_prefers_the_per_message_item_id(self):
        """Thread siblings share a convid; keying on it loses one of them."""
        row = FakeRow("conv-1")
        row._attrs["id"] = "item-9"
        self.assertEqual(owa.row_email_id(row, 3), "item-9")

    def test_the_conversation_id_is_only_a_fallback(self):
        self.assertEqual(owa.row_email_id(FakeRow("conv-1"), 3), "conv-1")

    def test_a_row_without_ids_falls_back_to_its_index(self):
        row = FakeRow("x")
        row._attrs = {}
        self.assertEqual(owa.row_email_id(row, 7), "email-7")

    def test_unread_is_read_from_either_the_label_or_the_class(self):
        self.assertTrue(owa._is_unread(FakeRow("a", label="Unread, from SPMS")))
        self.assertTrue(owa._is_unread(FakeRow("b", classes="row unread")))
        self.assertFalse(owa._is_unread(FakeRow("c", label="From SPMS")))

    def test_an_explicit_date_in_the_label_is_parsed(self):
        self.assertEqual(owa.row_date(FakeRow("a", label="SPMS 8/10/2026 renewal")),
                         date(2026, 8, 10))

    def test_todays_rows_have_no_explicit_date(self):
        """OWA shows a bare time for today, which must not read as 'no cutoff'."""
        self.assertIsNone(owa.row_date(FakeRow("a", label="SPMS 11:38 AM renewal")))

    def test_an_impossible_date_is_not_a_crash(self):
        self.assertIsNone(owa.row_date(FakeRow("a", label="13/45/2026")))


class TestScrapeSince(unittest.TestCase):
    def setUp(self):
        self._real = owa.extract_email

    def tearDown(self):
        owa.extract_email = self._real

    def scrape(self, rows, cutoff=date(2026, 8, 1), **kw):
        page = FakePage(rows)
        owa.extract_email = _patched_extract(page)
        return page, owa.scrape_since(page, cutoff, **kw)

    def test_it_stops_at_the_first_row_older_than_the_cutoff(self):
        """Newest-first sort is what makes an early stop safe - and cheap."""
        rows = [FakeRow("a", label="Unread 8/9/2026"),
                FakeRow("b", label="Unread 7/2/2026"),   # before cutoff
                FakeRow("c", label="Unread 8/8/2026")]   # never reached
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["a"])
        self.assertFalse(rows[2].clicked)

    def test_pinned_rows_are_skipped_without_being_opened(self):
        rows = [FakeRow("pin", label="Pinned Unread 1/1/2020"),
                FakeRow("a", label="Unread 8/9/2026")]
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["a"])
        self.assertFalse(rows[0].clicked)

    def test_a_pinned_row_does_not_trigger_the_cutoff(self):
        """Pinned rows keep their old date at the top; stopping there loses the inbox."""
        rows = [FakeRow("pin", label="Pinned Unread 1/1/2020"),
                FakeRow("a", label="Unread 8/9/2026"),
                FakeRow("b", label="Unread 8/5/2026")]
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["a", "b"])

    def test_read_rows_are_skipped_by_default(self):
        rows = [FakeRow("read", label="8/9/2026"),
                FakeRow("new", label="Unread 8/9/2026")]
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["new"])

    def test_all_mode_includes_read_rows_and_marks_nothing(self):
        rows = [FakeRow("read", label="8/9/2026"),
                FakeRow("new", label="Unread 8/9/2026")]
        page, out = self.scrape(rows, unread_only=False)
        self.assertEqual([e["email_id"] for e in out], ["read", "new"])
        self.assertEqual(page.marked_read, 0)

    def test_scraped_rows_are_explicitly_marked_read(self):
        """OWA's passive auto-mark is debounced; without this a run re-bills itself."""
        page, out = self.scrape([FakeRow("a", label="Unread 8/9/2026")])
        self.assertEqual(page.marked_read, 1)

    def test_a_stale_row_is_skipped_not_fatal(self):
        rows = [FakeRow("a", label="Unread 8/9/2026"),
                FakeRow("gone", stale=True),
                FakeRow("b", label="Unread 8/8/2026")]
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["a", "b"])

    def test_two_rows_sharing_an_id_are_only_opened_once(self):
        """A duplicate is a duplicate click, a paid call, and a clobbered row."""
        rows = [FakeRow("dup", label="Unread 8/9/2026"),
                FakeRow("dup", label="Unread 8/9/2026"),
                FakeRow("b", label="Unread 8/9/2026")]
        page, out = self.scrape(rows)
        self.assertEqual([e["email_id"] for e in out], ["dup", "b"])
        self.assertEqual(page.opened, ["dup", "b"])

    def test_max_emails_caps_what_gets_opened(self):
        rows = [FakeRow("r{}".format(n), label="Unread 8/9/2026") for n in range(10)]
        page, out = self.scrape(rows, max_emails=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(page.opened), 3)

    def test_max_scan_bounds_rows_even_when_none_qualify(self):
        rows = [FakeRow("r{}".format(n), label="8/9/2026") for n in range(50)]
        page, out = self.scrape(rows, max_scan=5)
        self.assertEqual(out, [])

    def test_the_subject_survives_either_deploy_rings_markup(self):
        """One dead subject selector must not silently empty every subject.

        This is the defect a live backtest caught: the prototype's nested form
        matched nothing, so all 20 scraped rows arrived with no subject at all -
        weakening the prefilter and the prompt without ever failing.
        """
        for ring in owa.READING_PANE_SUBJECT_SELECTORS:
            page = FakePage([FakeRow("a", label="Unread 8/9/2026")])
            page.subject_selector = ring
            owa.extract_email = _patched_extract(page)
            out = owa.scrape_since(page, date(2026, 8, 1))
            self.assertEqual(out[0]["subject"], "Subject for a", ring)

    def test_a_ring_answering_no_subject_selector_is_not_a_crash(self):
        page = FakePage([FakeRow("a", label="Unread 8/9/2026")])
        page.subject_selector = "nothing-matches-this"
        owa.extract_email = _patched_extract(page)
        self.assertEqual(owa.scrape_since(page, date(2026, 8, 1))[0]["subject"], "")

    def test_the_extracted_record_carries_what_triage_needs(self):
        page, out = self.scrape([FakeRow("a", label="Unread 8/9/2026", body="Apply now")])
        self.assertEqual(out[0], {
            "email_id": "a",
            "sender": "spms@ntu.edu.sg",
            "subject": "Subject for a",
            "timestamp": "Mon 10/08/2026 09:14",
            "body_content": "Apply now",
        })


class TestSession(unittest.TestCase):
    def test_the_session_lives_beside_the_other_project_state(self):
        path = owa.session_path("NTU")
        self.assertEqual(path, os.path.join("NTU", ".intuition",
                                            "owa_session.json"))

    def test_linked_is_false_without_a_saved_session(self):
        with TemporaryDirectory() as d:
            self.assertFalse(owa.linked(d))

    def test_opening_without_a_session_says_what_to_run(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(owa.SessionExpired) as caught:
                owa.Mailbox(d).open()
            self.assertIn("--login", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
