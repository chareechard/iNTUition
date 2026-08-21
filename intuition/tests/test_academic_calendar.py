"""Teaching-week mapping, checked against NTU's published AY2026-27 calendar."""
import unittest
from datetime import date

from intuition import academic_calendar as cal


class TestSemesterOneWeeks(unittest.TestCase):
    def test_week_one_starts_10_august_2026(self):
        self.assertEqual(cal.get("26S1").monday_of(1), date(2026, 8, 10))

    def test_weeks_before_recess_are_consecutive(self):
        sem = cal.get("26S1")
        self.assertEqual(sem.monday_of(2), date(2026, 8, 17))
        self.assertEqual(sem.monday_of(7), date(2026, 9, 21))

    def test_recess_pushes_week_eight_a_week_later(self):
        """Week 8 is 5 Oct, not 28 Sep - the recess week sits in between."""
        sem = cal.get("26S1")
        self.assertEqual(sem.recess_monday(), date(2026, 9, 28))
        self.assertEqual(sem.monday_of(8), date(2026, 10, 5))

    def test_final_teaching_week(self):
        self.assertEqual(cal.get("26S1").monday_of(13), date(2026, 11, 9))

    def test_week_of_known_dates(self):
        self.assertEqual(cal.week_of(date(2026, 8, 10)), 1)
        self.assertEqual(cal.week_of(date(2026, 8, 14)), 1)   # Friday of week 1
        self.assertEqual(cal.week_of(date(2026, 10, 5)), 8)
        self.assertEqual(cal.week_of(date(2026, 11, 13)), 13)

    def test_recess_and_exams_are_not_teaching_weeks(self):
        self.assertIsNone(cal.week_of(date(2026, 9, 30)))
        self.assertIsNone(cal.week_of(date(2026, 11, 24)))

    def test_phases(self):
        self.assertEqual(cal.phase_of(date(2026, 8, 10)), "Teaching Week 1")
        self.assertEqual(cal.phase_of(date(2026, 9, 30)), "Recess Week")
        self.assertEqual(cal.phase_of(date(2026, 11, 18)), "Revision Week")
        self.assertEqual(cal.phase_of(date(2026, 11, 24)), "Examinations")
        self.assertEqual(cal.phase_of(date(2026, 8, 4)), "Before semester")

    def test_exams_cover_the_dates_on_the_registration_record(self):
        """SC2001 23 Nov, MH2500 24 Nov, MH2100 26 Nov 2026."""
        sem = cal.get("26S1")
        for d in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 26)):
            self.assertTrue(sem.exams[0] <= d <= sem.exams[1], d)

    def test_unverified_semester_returns_none_rather_than_guessing(self):
        self.assertIsNone(cal.get("27S2"))
        self.assertIsNone(cal.week_of(date(2027, 3, 1), "27S2"))
        self.assertIsNone(cal.phase_of(date(2027, 3, 1), "27S2"))


class TestWeekExpressions(unittest.TestCase):
    def test_range(self):
        self.assertEqual(cal.parse_weeks("Wk2-13"), list(range(2, 14)))

    def test_alternating_list(self):
        self.assertEqual(cal.parse_weeks("Wk1,3,5,7,9,11,13"),
                         [1, 3, 5, 7, 9, 11, 13])

    def test_partial_range(self):
        self.assertEqual(cal.parse_weeks("Wk1-10"), list(range(1, 11)))

    def test_teaching_prefix_tolerated(self):
        self.assertEqual(cal.parse_weeks("Teaching Wk1-13"), list(range(1, 14)))

    def test_blank_means_every_week(self):
        self.assertIsNone(cal.parse_weeks(""))
        self.assertIsNone(cal.parse_weeks(None))

    def test_out_of_range_weeks_dropped(self):
        self.assertEqual(cal.parse_weeks("Wk12-20"), [12, 13])

    def test_runs_in_week(self):
        self.assertFalse(cal.runs_in_week("Wk2-13", 1))
        self.assertTrue(cal.runs_in_week("Wk2-13", 2))
        self.assertTrue(cal.runs_in_week("Wk1,3,5,7,9,11,13", 1))
        self.assertFalse(cal.runs_in_week("Wk2,4,6,8,10,12", 1))

    def test_unknown_week_expression_shows_the_class(self):
        """Better to show a class that might not run than hide one that does."""
        self.assertTrue(cal.runs_in_week("", 5))
        self.assertTrue(cal.runs_in_week("every other Tuesday", 5))
        self.assertTrue(cal.runs_in_week("Wk2-13", None))


if __name__ == "__main__":
    unittest.main()
