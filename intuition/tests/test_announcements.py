from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from intuition.announcements import Feed, clean


COURSE = {"id": "_1_1", "name": "26S1-SC2002-OBJECT ORIENTED DESIGN"}


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
