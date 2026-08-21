import os
import unittest
from datetime import date, datetime
from tempfile import TemporaryDirectory

from intuition import schedule

# The shape STARS / the NTU class-schedule page produces when copied.
STARS_TEXT = """
SC2005   10160   LEC/STUDIO   LE1   MON   0930-1030   LT19    Teaching Wk1-13
                 TUT          T1    WED   1330-1430   TR+15   Teaching Wk2-13
SC2001   10021   LEC/STUDIO   LE1   TUE   1130-1230   LT2A    Teaching Wk1-13
MH2500   12034   LEC/STUDIO   LE1   THU   0830-1030   LT23    Teaching Wk1-13
"""

ICS_TEXT = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:SC2005 Operating Systems LEC
DTSTART:20260810T093000
DTEND:20260810T103000
LOCATION:LT19
END:VEVENT
BEGIN:VEVENT
SUMMARY:MH2100 Calculus III TUT
DTSTART:20260812T133000
DTEND:20260812T143000
LOCATION:TR+15
END:VEVENT
END:VCALENDAR
"""


class TestStarsParsing(unittest.TestCase):
    def test_parses_every_row_with_a_day_and_a_time(self):
        rows = schedule.parse_ntu_timetable(STARS_TEXT)
        self.assertEqual(len(rows), 4)

    def test_fields_are_extracted(self):
        rows = schedule.parse_ntu_timetable(STARS_TEXT)
        first = rows[0]
        self.assertEqual(first["course"], "SC2005")
        self.assertEqual(first["index"], "10160")
        self.assertEqual(first["day"], "MON")
        self.assertEqual(first["start"], "09:30")
        self.assertEqual(first["end"], "10:30")
        self.assertEqual(first["venue"], "LT19")
        self.assertIn("1-13", first["weeks"])

    def test_continuation_row_inherits_the_course_above_it(self):
        """NTU omits the repeated course code on a course's later rows."""
        rows = schedule.parse_ntu_timetable(STARS_TEXT)
        tut = next(r for r in rows if r["day"] == "WED")
        self.assertEqual(tut["course"], "SC2005")
        self.assertEqual(tut["type"], "TUT")

    def test_rows_without_a_time_are_ignored(self):
        self.assertEqual(schedule.parse_ntu_timetable("SC2005 LEC MON LT19"), [])

    def test_rows_without_a_day_are_ignored(self):
        self.assertEqual(schedule.parse_ntu_timetable("SC2005 LEC 0930-1030"), [])

    def test_full_day_names_accepted(self):
        rows = schedule.parse_ntu_timetable("SC2005 10160 LEC Monday 0930-1030 LT19")
        self.assertEqual(rows[0]["day"], "MON")

    def test_en_dash_time_range(self):
        rows = schedule.parse_ntu_timetable("SC2005 10160 LEC MON 0930–1030 LT19")
        self.assertEqual(rows[0]["start"], "09:30")


class TestIcsParsing(unittest.TestCase):
    def test_reads_events(self):
        rows = schedule.parse_ics(ICS_TEXT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["course"], "SC2005")
        self.assertEqual(rows[0]["day"], "MON")        # 10 Aug 2026 is a Monday
        self.assertEqual(rows[0]["start"], "09:30")
        self.assertEqual(rows[0]["venue"], "LT19")

    def test_autodetects_format(self):
        self.assertEqual(len(schedule.parse(ICS_TEXT)), 2)
        self.assertEqual(len(schedule.parse(STARS_TEXT)), 4)

    def test_folded_lines_are_unfolded(self):
        folded = ICS_TEXT.replace("SUMMARY:SC2005 Operating Systems LEC",
                                  "SUMMARY:SC2005 Operating\n  Systems LEC")
        self.assertEqual(schedule.parse_ics(folded)[0]["course"], "SC2005")


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace(schedule.parse(STARS_TEXT))
            s.save()
            self.assertEqual(len(schedule.Schedule(root)), 4)

    def test_dedupe_drops_identical_sessions(self):
        rows = schedule.parse(STARS_TEXT) + schedule.parse(STARS_TEXT)
        self.assertEqual(len(schedule.dedupe(rows)), 4)

    def test_sessions_sorted_by_day_then_time(self):
        rows = schedule.dedupe(schedule.parse(STARS_TEXT))
        days = [r["day"] for r in rows]
        self.assertEqual(days, sorted(days, key=schedule.DAYS.index))

    def test_corrupt_file_is_ignored(self):
        with TemporaryDirectory() as root:
            p = schedule.schedule_path(root)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("{not json")
            self.assertEqual(len(schedule.Schedule(root)), 0)

    def test_for_day(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace(schedule.parse(STARS_TEXT))
            self.assertEqual(len(s.for_day("MON")), 1)
            self.assertEqual(len(s.for_day("SUN")), 0)

    def test_reload_if_changed_picks_up_external_assessments(self):
        with TemporaryDirectory() as root:
            first = schedule.Schedule(root)
            second = schedule.Schedule(root)
            second.set_announcement_important_dates([{
                "source_id": "drive:outline", "course": "MH2500",
                "date": "2026-09-08", "kind": "midterm", "details": "Test 1",
            }])
            second.save()
            self.assertTrue(first.reload_if_changed())
            self.assertEqual(first.important_dates[0]["course"], "MH2500")


class TestUpcoming(unittest.TestCase):
    def _sched(self, root):
        s = schedule.Schedule(root)
        s.replace(schedule.parse(STARS_TEXT))
        return s

    def test_next_session_later_the_same_day(self):
        with TemporaryDirectory() as root:
            s = self._sched(root)
            # Monday 08:00 -> the 09:30 lecture is next, 90 minutes away.
            got = s.upcoming(datetime(2026, 8, 10, 8, 0), limit=1)
            self.assertEqual(got[0]["course"], "SC2005")
            self.assertEqual(got[0]["in_minutes"], 90)

    def test_rolls_into_the_next_day_once_today_is_done(self):
        with TemporaryDirectory() as root:
            s = self._sched(root)
            got = s.upcoming(datetime(2026, 8, 10, 23, 0), limit=1)
            self.assertEqual(got[0]["day"], "TUE")
            self.assertEqual(got[0]["date"], "2026-08-11")

    def test_results_are_ordered_and_limited(self):
        with TemporaryDirectory() as root:
            s = self._sched(root)
            got = s.upcoming(datetime(2026, 8, 10, 0, 0), limit=3)
            self.assertEqual(len(got), 3)
            self.assertEqual([g["in_minutes"] for g in got],
                             sorted(g["in_minutes"] for g in got))

    def test_empty_schedule_yields_nothing(self):
        with TemporaryDirectory() as root:
            self.assertEqual(schedule.Schedule(root).upcoming(), [])



class TestStarsPdf(unittest.TestCase):
    """Parsed against the real registration printout committed alongside the repo."""

    PDF = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),
        "Course(s) Registered - Academic Year 2026,Semester 1.pdf")

    def setUp(self):
        if not os.path.exists(self.PDF):
            self.skipTest("sample STARS PDF not present")
        self.result = schedule.parse_stars_pdf(self.PDF)

    def test_all_eighteen_sessions_found(self):
        self.assertEqual(len(self.result["sessions"]), 18)

    def test_day_columns_recovered_from_the_grid(self):
        counts = {d: len([s for s in self.result["sessions"] if s["day"] == d])
                  for d in ("MON", "TUE", "WED", "THU", "FRI")}
        self.assertEqual(counts, {"MON": 1, "TUE": 3, "WED": 5, "THU": 6, "FRI": 3})

    def test_a_known_session_is_exact(self):
        mon = [s for s in self.result["sessions"] if s["day"] == "MON"][0]
        self.assertEqual(mon["course"], "SC2001")
        self.assertEqual(mon["type"], "LEC")
        self.assertEqual(mon["start"], "12:30")
        self.assertEqual(mon["end"], "13:20")
        self.assertEqual(mon["venue"], "LT1A")
        self.assertEqual(mon["index"], "10154")

    def test_week_expressions_preserved(self):
        by = {(s["course"], s["type"], s["day"]): s for s in self.result["sessions"]}
        self.assertEqual(by[("SC2005", "LAB", "THU")]["weeks"], "Wk2,4,6,8,10,12")
        self.assertEqual(by[("SC2002", "LAB", "WED")]["weeks"], "Wk1,3,5,7,9,11,13")
        self.assertEqual(by[("ML0004", "TUT", "FRI")]["weeks"], "Wk1-10")

    def test_venue_with_punctuation(self):
        wed = {(s["course"], s["type"]): s for s in self.result["sessions"]
               if s["day"] == "WED"}
        self.assertEqual(wed[("MH2500", "TUT")]["venue"], "LHS-TR+53")

    def test_registered_courses_and_exams(self):
        codes = sorted(c["course"] for c in self.result["courses"])
        self.assertEqual(codes, ["MH2100", "MH2500", "ML0004",
                                 "SC2001", "SC2002", "SC2005"])
        exams = {e["course"]: e["when"] for e in self.result["exams"]}
        self.assertEqual(len(exams), 3)
        self.assertIn("23-Nov-2026", exams["SC2001"])

    def test_rejects_a_pdf_that_is_not_a_stars_printout(self):
        with TemporaryDirectory() as d:
            fake = os.path.join(d, "x.pdf")
            open(fake, "wb").write(b"%PDF-1.4\n%%EOF\n")
            with self.assertRaises(Exception):
                schedule.parse_stars_pdf(fake)


class TestWeekAwareStore(unittest.TestCase):
    def test_announcement_changes_overlay_only_the_named_date(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace([{"course": "SC2005", "type": "LEC", "day": "MON",
                        "start": "09:30", "end": "10:20", "venue": "LT1",
                        "weeks": "", "index": ""}])
            self.assertEqual(s.set_announcement_overrides([
                {"date": "2026-08-10", "course": "SC2005", "action": "change",
                 "type": "LEC", "old_start": "09:30", "start": "10:30",
                 "end": "11:20", "venue": "LT2", "source_id": "a"},
                {"date": "not-a-date", "course": "SC2005", "action": "cancel"},
            ]), 1)
            week = s.dynamic_week(date(2026, 8, 10), 1)
            self.assertEqual(week["MON"][0]["start"], "10:30")
            self.assertEqual(week["MON"][0]["venue"], "LT2")
            self.assertTrue(week["MON"][0]["dynamic"])
            self.assertEqual(s.week(1)["MON"][0]["start"], "09:30")

    def test_cancellation_and_makeup_are_applied(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace([{"course": "SC2005", "type": "TUT", "day": "TUE",
                        "start": "11:30", "end": "12:20", "venue": "TR1",
                        "weeks": "", "index": ""}])
            s.set_announcement_overrides([
                {"date": "2026-08-11", "course": "SC2005", "action": "cancel",
                 "type": "TUT", "old_start": "11:30"},
                {"date": "2026-08-12", "course": "SC2005", "action": "add",
                 "type": "TUT", "start": "13:30", "end": "14:20", "venue": "TR2"},
            ])
            week = s.dynamic_week(date(2026, 8, 10), 1)
            self.assertEqual(week["TUE"], [])
            self.assertEqual(week["WED"][0]["venue"], "TR2")

    def test_announcement_can_replace_a_recurring_week_pattern(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace([{"course": "SC2001", "type": "LAB", "day": "WED",
                        "start": "14:30", "end": "16:20", "venue": "HWLAB3",
                        "weeks": "Wk1,3,5,7,9,11,13", "index": "10154"}])
            self.assertEqual(s.set_announcement_overrides([{
                "date": "", "course": "SC2001", "action": "pattern", "type": "LAB",
                "old_start": "14:30", "weeks": "Wk7,9,11,13",
                "source_id": "a", "source_title": "Welcome",
            }]), 1)
            self.assertEqual(s.dynamic_week(date(2026, 8, 10), 5)["WED"], [])
            row = s.dynamic_week(date(2026, 8, 24), 7)["WED"][0]
            self.assertTrue(row["dynamic"])
            self.assertEqual(row["weeks"], "Wk7,9,11,13")

    def test_for_day_filters_by_teaching_week(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace([
                {"course": "SC2005", "type": "LEC", "day": "THU", "start": "12:30",
                 "end": "13:20", "venue": "LT1A", "weeks": "", "index": ""},
                {"course": "SC2005", "type": "TUT", "day": "THU", "start": "15:30",
                 "end": "16:20", "venue": "TR+8", "weeks": "Wk2-13", "index": ""},
            ])
            self.assertEqual(len(s.for_day("THU")), 2)        # unfiltered
            self.assertEqual(len(s.for_day("THU", 1)), 1)     # tutorial not in wk1
            self.assertEqual(len(s.for_day("THU", 2)), 2)

    def test_exams_and_courses_survive_a_save(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.replace([], exams=[{"course": "SC2001", "when": "23-Nov-2026"}],
                      courses=[{"course": "SC2001", "index": "10154"}])
            s.save()
            again = schedule.Schedule(root)
            self.assertEqual(again.exams[0]["course"], "SC2001")
            self.assertEqual(again.courses[0]["index"], "10154")

    def test_announcement_important_dates_survive_a_save(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            self.assertEqual(s.set_announcement_important_dates([
                {"source_id": "a", "source_title": "Presentation",
                 "course": "sc2002", "date": "2026-10-08",
                 "kind": "presentation", "start": "10:00"},
                {"source_id": "b", "source_title": "Assignment",
                 "course": "sc2002", "date": "2026-10-09",
                 "kind": "assignment", "start": "23:59"},
                {"course": "SC2002", "date": "unknown", "kind": "final"},
            ]), 2)
            s.save()
            again = schedule.Schedule(root)
            self.assertEqual(again.important_dates[0]["course"], "SC2002")
            self.assertEqual(again.important_dates[1]["kind"], "assignment")

    def test_assessment_timeline_labels_recess_between_weeks_seven_and_eight(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.important_dates = [{
                "source_id": "drive:outline", "course": "SC2002",
                "date": "2026-09-30", "kind": "quiz", "start": "14:30",
                "end": "15:30", "details": "Recess checkpoint",
            }]
            row = s.assessment_timeline("26S1")[0]
            self.assertEqual(row["phase"], "Recess Week")
            self.assertIsNone(row["teaching_week"])
            self.assertEqual(row["day"], "WED")
            self.assertEqual(row["timeslot"], "14:30-15:30")

    def test_future_important_date_survives_announcement_rotation(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.important_dates = [{"source_id": "old", "source_title": "Final",
                                  "course": "SC2002", "date": "2099-11-20",
                                  "kind": "final", "start": "", "end": "",
                                  "venue": "", "details": ""}]
            self.assertEqual(s.set_announcement_important_dates([]), 0)
            self.assertEqual(len(s.important_dates), 1)

    def test_distinct_same_time_assessments_are_not_merged(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            events = [
                {"source_id": "a", "course": "SC2005", "date": "2026-10-08",
                 "kind": "quiz", "start": "12:30", "details": "Lecture Quiz 4"},
                {"source_id": "a", "course": "SC2005", "date": "2026-10-08",
                 "kind": "quiz", "start": "12:30", "details": "Lab Quiz 1"},
            ]
            self.assertEqual(s.set_announcement_important_dates(events), 2)
            self.assertEqual(len(s.important_dates), 2)

    def test_announcement_amends_matching_document_assessment(self):
        with TemporaryDirectory() as root:
            s = schedule.Schedule(root)
            s.set_announcement_important_dates([{
                "source_id": "drive:outline", "source_title": "Course outline",
                "course": "SC2005", "date": "2026-09-03", "kind": "quiz",
                "start": "12:30", "details": "Lecture Quiz 2",
            }])
            s.set_announcement_important_dates([{
                "source_id": "announcement-2", "source_title": "Quiz 2 moved",
                "course": "SC2005", "date": "2026-09-04", "kind": "quiz",
                "start": "14:30", "details": "Lecture Quiz 2 moved to Friday",
            }])
            self.assertEqual(len(s.important_dates), 1)
            self.assertEqual(s.important_dates[0]["date"], "2026-09-04")
            self.assertTrue(s.important_dates[0]["amended"])
            self.assertEqual(s.important_dates[0]["origin"], "announcement")


if __name__ == "__main__":
    unittest.main()
