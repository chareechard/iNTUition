from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from intuition.announcements import (
    Feed, clean, content_key, dedupe, _scrub_text, _scrub_links)


COURSE = {"id": "_1_1", "name": "26S1-SC2002-OBJECT ORIENTED DESIGN"}
COURSE_B = {"id": "_2_1", "name": "26S1-CZ2002-OBJECT ORIENTED DESIGN"}


def _cross_posts(day="2026-09-01"):
    body = ("<p>There will be no tutorial this week due to Students' Union Day.</p>"
            "<p>Best regards,<br>Prof Lim</p>")
    return (
        clean({"id": "sc", "title": "SC2002 No tutorial this week",
               "body": body, "created": day + "T09:00:00Z"}, COURSE),
        clean({"id": "cz", "title": "CZ2002 No tutorial this week",
               "body": body, "created": day + "T09:01:00Z"}, COURSE_B),
    )


def test_clean_preserves_text_and_links():
    row = clean({"id": "a", "title": "Lab 1", "body":
                 '<p>Install JDK</p><a href="https://example.test/jdk">Get it</a>',
                 "modified": "2026-08-11T09:00:00Z"}, COURSE)
    assert row["body"] == "Install JDK\nGet it"
    assert row["links"] == [{"title": "Get it", "url": "https://example.test/jdk"}]


def test_plain_urls_are_retained_as_links():
    row = clean({"id": "a", "body": "<p>Install from https://example.test/jdk.</p>"}, COURSE)
    assert row["links"][0]["url"] == "https://example.test/jdk"


def test_sync_aggregates_courses_and_survives_one_failure(tmp_path):
    feed = Feed(str(tmp_path))
    rows = [{"id": "a", "title": "Welcome", "body": "<p>Hello</p>",
             "draft": False,
             "modified": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}]

    def paged(_token, url, params=None):
        if "_2_1" in url:
            raise RuntimeError("403")
        return rows

    courses = [COURSE, {"id": "_2_1", "name": "SC2005"}]
    with patch("intuition.announcements.rest._get_paged", side_effect=paged):
        errors = feed.sync("token", courses)
    assert feed.snapshot()["total"] == 1
    assert len(errors) == 1 and "SC2005" in errors[0]


def test_sync_retains_recent_cached_items_during_empty_response(tmp_path):
    feed = Feed(str(tmp_path))
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    feed.items = [clean({"id": "cached", "title": "Still relevant",
                         "created": recent}, COURSE)]
    with patch("intuition.announcements.rest._get_paged", return_value=[]):
        feed.sync("token", [COURSE])
    assert [item["id"] for item in feed.items] == ["cached"]


def test_sync_rotates_items_older_than_seven_days(tmp_path):
    feed = Feed(str(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    feed.items = [
        clean({"id": "old", "created": old}, COURSE),
        clean({"id": "recent", "created": recent}, COURSE),
    ]
    with patch("intuition.announcements.rest._get_paged", return_value=[]):
        feed.sync("token", [COURSE])
    assert [item["id"] for item in feed.items] == ["recent"]


def test_read_state_persists(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "a", "title": "One"}, COURSE)]
    feed.save()
    assert feed.mark_read("a") is True
    restored = Feed(str(tmp_path)).snapshot()
    assert restored["unread"] == 0
    assert restored["items"][0]["read"] is True


def test_add_local_announcement_persists_and_shows_first(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "lms", "title": "Course post"}, COURSE)]
    feed.save()
    item = feed.add_local("Study group Fri 7pm", "warning")
    assert item["id"].startswith("local-")
    snap = Feed(str(tmp_path)).snapshot()
    assert snap["total"] == 2 and snap["unread"] == 2
    assert snap["items"][0]["id"] == item["id"]
    assert snap["items"][0]["local"] is True
    assert snap["items"][0]["priority"] == "warning"
    assert snap["items"][0]["read"] is False
    assert snap["items"][1]["local"] is False
    # "Personal" is not offered as a course filter.
    assert "Personal" not in snap["courses"]


def test_add_local_validates_and_normalises(tmp_path):
    feed = Feed(str(tmp_path))
    with pytest.raises(ValueError):
        feed.add_local("   ", "info")
    assert feed.add_local("Reminder", "loud")["priority"] == "info"
    assert len(feed.add_local("x" * 300)["title"]) == 240


def test_local_announcement_is_exempt_from_retention(tmp_path):
    feed = Feed(str(tmp_path))
    feed.local = [{"id": "local-old", "course": "Personal", "title": "Old note",
                   "body": "", "links": [],
                   "created": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
                   "modified": "", "priority": "info"}]
    with patch("intuition.announcements.rest._get_paged", return_value=[]):
        feed.sync("token", [COURSE])
    assert [item["id"] for item in feed.local] == ["local-old"]


def test_mark_read_accepts_local_ids(tmp_path):
    feed = Feed(str(tmp_path))
    item = feed.add_local("Buy textbook")
    assert feed.mark_read(item["id"]) is True
    assert Feed(str(tmp_path)).snapshot()["unread"] == 0
    assert feed.mark_read("local-missing") is False


def test_sync_preserves_local_read_flag(tmp_path):
    feed = Feed(str(tmp_path))
    item = feed.add_local("Consult Tue")
    feed.mark_read(item["id"])
    with patch("intuition.announcements.rest._get_paged", return_value=[]):
        feed.sync("token", [COURSE])
    assert item["id"] in feed.read


def test_remove_local(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "lms", "title": "Course post"}, COURSE)]
    a = feed.add_local("first")
    feed.add_local("second")
    feed.mark_read(a["id"])
    assert feed.remove_local(a["id"]) is True
    assert a["id"] not in feed.read
    assert [i["title"] for i in feed.local] == ["second"]
    assert feed.remove_local("local-nope") is False
    assert feed.remove_local("lms") is False
    assert [i["id"] for i in feed.items] == ["lms"]


def test_summarize_excludes_local_announcements(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "a", "title": "Lab briefing",
                         "body": "<p>Read chapter 3</p>"}, COURSE)]
    feed.add_local("My private reminder")
    with patch("intuition.announcements.ai_provider.complete_tier",
               return_value={"text": "FYI\n- SC2002: chapter 3",
                             "backend": "omniroute", "model": "auto"}) as complete:
        result = feed.summarize()
    prompt = complete.call_args.args[1]
    assert "Lab briefing" in prompt
    assert "My private reminder" not in prompt
    assert result["feed_ids"] == ["a"]


def test_tldr_is_cached_with_its_source_set(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "a", "title": "Lab", "body": "<p>Install JDK</p>"}, COURSE)]
    with patch("intuition.announcements.ai_provider.complete_tier",
               return_value={"text": "Prepare\n- SC2002: Install JDK",
                             "backend": "omniroute", "model": "auto"}) as complete:
        result = feed.summarize()
    assert complete.call_args.args[0] == "bulk"
    assert complete.call_args.kwargs["preferred"] is None
    system = complete.call_args.kwargs["system"]
    assert "decision-ready" in system
    assert "what the student must do" in system
    assert "administrative filler" in system
    assert "Install JDK" in result["text"]
    assert result["source_ids"] == ["a"] and result["feed_ids"] == ["a"]
    assert result["version"] == 2
    assert Feed(str(tmp_path)).snapshot()["summary"]["model"] == "auto"


def test_ai_extracts_only_changes_tied_to_known_announcements(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "a", "title": "Lecture moved",
                         "body": "<p>Monday lecture moved to 10:30 on 17 Aug.</p>"},
                        COURSE)]
    answer = ('[{"source_id":"a","course":"SC2002","date":"2026-08-17",'
              '"action":"change","type":"LEC","old_start":"09:30",'
              '"start":"10:30","end":"11:20","venue":"LT2","reason":"moved"},'
              '{"source_id":"invented","course":"SC2002","date":"2026-08-18",'
              '"action":"cancel"}]\nNo other certain changes.')
    with patch("intuition.announcements.ai_provider.complete",
               return_value={"text": answer}):
        changes = feed.detect_schedule_changes([
            {"course": "SC2002", "type": "LEC", "day": "MON",
             "start": "09:30", "end": "10:20", "venue": "LT1"}])
    assert len(changes) == 1
    assert changes[0]["source_title"] == "Lecture moved"


def test_week_start_language_is_sent_to_the_detector(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "lab", "title": "Welcome", "body":
                         "<p>Your first lab session will be during week 6 or 7, "
                         "depending on your group.</p>"}, COURSE)]
    with patch("intuition.announcements.ai_provider.complete",
               side_effect=AssertionError("explicit week pattern should not need AI")):
        changes = feed.detect_schedule_changes([{
            "course": "SC2002", "type": "LAB", "day": "WED", "start": "14:30",
            "end": "16:20", "venue": "HWLAB3", "weeks": "Wk1,3,5,7,9,11,13"}],
            preferred="cli")
    assert changes[0]["weeks"] == "Wk7,9,11,13"
    assert "week 6 or 7" in changes[0]["reason"]


def test_ordinal_week_starts_follow_the_students_group_pattern(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "welcome", "title": "Welcome", "body":
                         "<p>Lab starts from either third week or fourth week "
                         "respectively for different groups. Tutorial starts from "
                         "fourth week for all groups.</p>"}, COURSE)]
    sessions = [
        {"course": "SC2002", "type": "LAB", "day": "WED", "start": "10:30",
         "end": "12:20", "venue": "SWLAB3", "weeks": "Wk1,3,5,7,9,11,13"},
        {"course": "SC2002", "type": "TUT", "day": "THU", "start": "14:30",
         "end": "15:20", "venue": "TR+17", "weeks": "Wk2-13"},
    ]
    with patch("intuition.announcements.ai_provider.complete",
               side_effect=AssertionError("explicit ordinal weeks should not need AI")):
        changes = feed.detect_schedule_changes(sessions, preferred="cli")
    by_type = {change["type"]: change["weeks"] for change in changes}
    assert by_type == {"LAB": "Wk3,5,7,9,11,13",
                       "TUT": "Wk4,5,6,7,8,9,10,11,12,13"}


def test_assessment_dates_are_extracted_for_temporal_protocol(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "exam", "title": "Midterm details",
                         "body": "<p>Midterm: 2 September 2026, 14:30, LT2.</p>"},
                        COURSE)]
    answer = ('[{"source_id":"exam","source_title":"x","course":"SC2002",'
              '"date":"2026-09-02","kind":"midterm","start":"14:30",'
              '"end":"","venue":"LT2","details":"Midterm"}]')
    with patch("intuition.announcements.ai_provider.complete",
               return_value={"text": answer}):
        events = feed.detect_important_dates(preferred="cli")
    assert events[0]["date"] == "2026-09-02"
    assert events[0]["kind"] == "midterm"
    assert events[0]["source_title"] == "Midterm details"


def test_graded_assignment_due_date_is_extracted(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({
        "id": "assignment", "title": "Assignment 2 deadline",
        "body": "<p>Graded Assignment 2 (15%) is due 18 September 2026 at 23:59.</p>"
    }, COURSE)]
    answer = ('[{"source_id":"assignment","source_title":"x",'
              '"course":"SC2002","date":"2026-09-18","kind":"assignment",'
              '"start":"23:59","end":"","venue":"","details":"Assignment 2 (15%) due"}]')
    with patch("intuition.announcements.ai_provider.complete",
               return_value={"text": answer}):
        events = feed.detect_important_dates(preferred="cli")
    assert events[0]["kind"] == "assignment"
    assert events[0]["date"] == "2026-09-18"
    assert events[0]["start"] == "23:59"


# ── Data cleaning ─────────────────────────────────────────────────────────────

def test_scrub_text_drops_mail_chrome_and_signoff_but_keeps_substance():
    body = (
        "﻿Tutorial 3 is due on 12 September at 23:59.\n"
        "Venue: LT19.\n\n\n"
        "--------\n"
        "Do not reply to this email\n"
        "This is an automated message from NTULearn\n"
        "Best regards,\nProf Tan")
    out = _scrub_text(body)
    assert "Tutorial 3 is due on 12 September at 23:59." in out
    assert "Venue: LT19." in out
    assert "Do not reply" not in out
    assert "automated message" not in out
    assert "Best regards" not in out
    assert "\n\n\n" not in out and "﻿" not in out


def test_scrub_text_keeps_a_terse_body_that_is_only_a_signoff():
    assert _scrub_text("Thanks, see you Monday!") == "Thanks, see you Monday!"


def test_scrub_links_dedups_and_drops_dead_and_self_referential():
    links = [
        {"title": "https://ex.test/a", "url": "https://ex.test/a"},
        {"title": "Assignment brief", "url": "https://ex.test/a"},
        {"title": "Email the TA", "url": "mailto:ta@ntu.edu.sg"},
        {"title": "This announcement",
         "url": "https://ntulearn.ntu.edu.sg/webapps/blackboard/execute/announcement?x"},
    ]
    out = _scrub_links(links)
    assert out == [{"title": "Assignment brief", "url": "https://ex.test/a"}]


def test_clean_applies_scrubbing():
    row = clean({"id": "a", "title": "  Lab   1  ", "body":
                 "<p>Install JDK 21 today.</p><p>Do not reply to this email</p>"
                 '<p>Guide: <a href="https://ex.test/jdk">https://ex.test/jdk</a></p>'
                 '<p>Questions: <a href="mailto:prof@ntu.edu.sg">email the prof</a></p>',
                 "created": "2026-08-11T09:00:00Z"}, COURSE)
    assert row["title"] == "Lab 1"
    assert "Install JDK 21 today." in row["body"]
    assert "Do not reply" not in row["body"]
    # mailto link dropped; bare-URL title kept (no nicer alternative present).
    assert row["links"] == [{"title": "https://ex.test/jdk", "url": "https://ex.test/jdk"}]


# ── Deterministic de-duplication ─────────────────────────────────────────────

def test_content_key_matches_cross_posted_shells_ignores_course_code_prefix():
    sc, cz = _cross_posts()
    assert content_key(sc) == content_key(cz)


def test_content_key_separates_genuinely_different_posts():
    a, _ = _cross_posts()
    b = clean({"id": "b", "title": "SC2002 Midterm venue", "body":
               "<p>The midterm is in LT1 not LT2.</p>",
               "created": "2026-09-01T09:00:00Z"}, COURSE)
    assert content_key(a) != content_key(b)


def test_dedupe_collapses_cross_posts_and_unions_courses_and_read_state():
    sc, cz = _cross_posts()
    read = {"cz"}
    out, collapsed = dedupe([sc, cz], read)
    assert collapsed == 1
    assert len(out) == 1
    survivor = out[0]
    assert survivor["id"] == "sc"                      # earliest release, stable
    assert survivor["cross_posted"] == ["26S1-CZ2002-OBJECT ORIENTED DESIGN"]
    assert survivor["duplicate_ids"] == ["cz"]
    assert read == {"sc"}                              # read state followed the merge


def test_sync_collapses_cross_posts_from_multiple_shells(tmp_path):
    feed = Feed(str(tmp_path))
    body = "<p>No lab this week; catch-up in week 10.</p>"
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def paged(_token, url, params=None):
        cid = "sc" if "_1_1" in url else "cz"
        return [{"id": cid, "title": "No lab this week", "body": body,
                 "draft": False, "created": day}]

    with patch("intuition.announcements.rest._get_paged", side_effect=paged):
        feed.sync("token", [COURSE, COURSE_B])
    snap = feed.snapshot()
    assert snap["total"] == 1
    survivor = snap["items"][0]
    both = {survivor["course"], *survivor["cross_posted"]}
    assert both == {COURSE["name"], COURSE_B["name"]}
    assert survivor["duplicate_ids"] and survivor["id"] not in survivor["duplicate_ids"]


def test_load_self_heals_a_legacy_duplicated_file(tmp_path):
    seed = Feed(str(tmp_path))
    sc, cz = _cross_posts(day=(datetime.now(timezone.utc)
                               - timedelta(days=1)).date().isoformat())
    # Write a file that predates de-duplication (both shell copies present).
    seed.items = [sc, cz]
    seed.clean_version = 0
    Feed.save(seed)
    reloaded = Feed(str(tmp_path))
    assert len(reloaded.items) == 1


def test_purge_duplicates_endpoint_style_call(tmp_path):
    feed = Feed(str(tmp_path))
    sc, cz = _cross_posts(day=(datetime.now(timezone.utc)
                               - timedelta(days=1)).date().isoformat())
    feed.items = [dict(sc), dict(cz)]
    feed.summary = {"text": "stale"}
    folded = feed.purge_duplicates()
    assert folded == 1
    assert feed.summary is None
    assert [i["id"] for i in feed.items] == ["sc"]
    assert Feed(str(tmp_path)).snapshot()["total"] == 1


# ── AI near-duplicate resolution ────────────────────────────────────────────

def _near_dupes(tmp_path):
    feed = Feed(str(tmp_path))
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    feed.items = [
        clean({"id": "v1", "title": "Venues for Test 1",
               "body": "<p>Test 1 venue list is attached. Check the table for your "
                       "tutorial group and note the assigned hall.</p>",
               "created": day}, COURSE),
        clean({"id": "v2", "title": "UPDATED: Venues for Test 1",
               "body": "<p>Updated venue list for Test 1 attached. Find your tutorial "
                       "group in the table and note the assigned hall.</p>",
               "created": day}, COURSE),
        clean({"id": "x", "title": "Reading week reminder",
               "body": "<p>No classes during reading week.</p>", "created": day}, COURSE),
    ]
    feed.save()
    return feed


def test_resolve_duplicates_with_ai_folds_near_dupes_and_caches_verdict(tmp_path):
    feed = _near_dupes(tmp_path)
    with patch("intuition.announcements.ai_provider.status",
               return_value={"ready": True}), \
         patch("intuition.announcements.ai_provider.complete_tier",
               return_value={"text": '[["v1","v2"]]'}) as call:
        folded = feed.resolve_duplicates_with_ai(preferred=None)
    assert folded == 1
    assert call.call_count == 1
    ids = sorted(i["id"] for i in feed.items)
    assert ids == ["v1", "x"]
    assert "v2" in feed.items[[i["id"] for i in feed.items].index("v1")]["duplicate_ids"]

    # A second pass re-uses the cached verdict - no further model call.
    reloaded = Feed(str(tmp_path))
    with patch("intuition.announcements.ai_provider.status",
               return_value={"ready": True}), \
         patch("intuition.announcements.ai_provider.complete_tier",
               side_effect=AssertionError("verdict should be cached")):
        assert reloaded.resolve_duplicates_with_ai(preferred=None) == 0
    assert sorted(i["id"] for i in reloaded.items) == ["v1", "x"]


def test_resolve_duplicates_with_ai_is_best_effort_on_failure(tmp_path):
    feed = _near_dupes(tmp_path)
    with patch("intuition.announcements.ai_provider.status",
               return_value={"ready": True}), \
         patch("intuition.announcements.ai_provider.complete_tier",
               side_effect=RuntimeError("gateway down")):
        assert feed.resolve_duplicates_with_ai(preferred=None) == 0
    assert len(feed.items) == 3  # deterministic feed untouched


def test_resolve_duplicates_with_ai_skips_model_when_feed_is_unambiguous(tmp_path):
    feed = Feed(str(tmp_path))
    feed.items = [clean({"id": "a", "title": "Lecture 1", "body": "<p>Chapter 1</p>",
                         "created": datetime.now(timezone.utc).isoformat()}, COURSE)]
    feed.save()
    with patch("intuition.announcements.ai_provider.complete_tier",
               side_effect=AssertionError("must not call the model")):
        assert feed.resolve_duplicates_with_ai() == 0
