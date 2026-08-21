"""Semester detection, exercised against course names taken from a live NTU account."""
import unittest
from datetime import date

from intuition import semester

# Verbatim from the live enrolment list - the naming is not uniform, which is the
# whole reason this module exists.
REAL_CURRENT = [
    "26S1-ML0004-CAREER DESIGN & WKPL READINESS (MAIN SITE)",
    "26S1-ML0004-CAREER DESIGN & WKPL READINESS (T087)",
    "26S1-SC2005-OPERATING SYSTEMS",
    "26S1-SC2002-SC2302-OBJECT ORIENTED DES & PROG",
    "26S1-SC2001-ALGORITHM DESIGN & ANALYSIS",
    "26S1-MH2500-PROBABILITY",
    "AY2026-2027, Semester 1, MH2100 (Calculus III)",
]

REAL_PAST = [
    "25S2-SC1007-TUT-MACS2-DATA STRUCTURES & ALGORITHMS (TUT)",
    "25S2-MH1301-DISCRETE MATHEMATICS",
    "25S2-MH1101-CALCULUS II",
    "AY2025-2026, Semester 2, MH1201 (Linear Algebra II)",
    "CC0015-HEALTH & WELLBEING (T002) AY2025/26 SEM 2",
]

REAL_UNDATED = [
    "TOP Seniors: Fostering a Community of Respect",
    "Personal Data Protection Act (PDPA) e-Learning Programme",
    "Risk Management For Student Activities Held Overseas",
    "CAO-SPMS Job/Internship Briefings",
]

AUG_2026 = date(2026, 8, 10)


class TestCurrentSemester(unittest.TestCase):
    def test_august_starts_semester_one(self):
        self.assertEqual(semester.current_semester(date(2026, 8, 1)), (26, 1))

    def test_december_is_still_semester_one(self):
        self.assertEqual(semester.current_semester(date(2026, 12, 31)), (26, 1))

    def test_january_rolls_to_semester_two_of_the_same_ay(self):
        """The behaviour asked for: 26S2 from Jan 2027 onwards."""
        self.assertEqual(semester.current_semester(date(2027, 1, 1)), (26, 2))

    def test_may_is_still_semester_two(self):
        self.assertEqual(semester.current_semester(date(2027, 5, 31)), (26, 2))

    def test_special_term_keeps_semester_two(self):
        self.assertEqual(semester.current_semester(date(2027, 6, 15)), (26, 2))
        self.assertEqual(semester.current_semester(date(2027, 7, 31)), (26, 2))

    def test_next_august_rolls_the_academic_year(self):
        self.assertEqual(semester.current_semester(date(2027, 8, 1)), (27, 1))

    def test_july_and_august_of_the_same_year_differ(self):
        self.assertEqual(semester.current_semester(date(2027, 7, 31)), (26, 2))
        self.assertEqual(semester.current_semester(date(2027, 8, 1)), (27, 1))

    def test_formatting(self):
        self.assertEqual(semester.format_semester((26, 1)), "26S1")
        self.assertEqual(semester.format_semester((7, 2)), "07S2")


class TestParsing(unittest.TestCase):
    def test_compact_form(self):
        self.assertEqual(
            semester.parse_course_semester("26S1-SC2005-OPERATING SYSTEMS"), (26, 1))

    def test_verbose_form(self):
        self.assertEqual(
            semester.parse_course_semester("AY2026-2027, Semester 1, MH2100 (Calculus III)"),
            (26, 1))

    def test_trailing_form(self):
        self.assertEqual(
            semester.parse_course_semester("CC0015-HEALTH & WELLBEING (T002) AY2025/26 SEM 2"),
            (25, 2))

    def test_undated_course_returns_none(self):
        for name in REAL_UNDATED:
            self.assertIsNone(semester.parse_course_semester(name), name)

    def test_empty_name(self):
        self.assertIsNone(semester.parse_course_semester(""))
        self.assertIsNone(semester.parse_course_semester(None))

    def test_does_not_match_inside_a_course_code(self):
        """SC2001 contains no semester; a loose \\d\\dS\\d would misfire on codes."""
        self.assertIsNone(semester.parse_course_semester("ABC12S3XYZ-SOMETHING"))
        self.assertIsNone(semester.parse_course_semester("MH2500-PROBABILITY"))

    def test_verbose_wins_over_a_stray_compact_match(self):
        self.assertEqual(
            semester.parse_course_semester("AY2025-2026, Semester 2, 26S1 mention"),
            (25, 2))


class TestCurrency(unittest.TestCase):
    def test_all_seven_real_current_courses_are_detected(self):
        """Including the odd one out that a 26S1- prefix match would drop."""
        for name in REAL_CURRENT:
            self.assertTrue(semester.is_current(name, AUG_2026), name)

    def test_previous_semester_courses_are_not_current(self):
        for name in REAL_PAST:
            self.assertFalse(semester.is_current(name, AUG_2026), name)

    def test_undated_courses_are_not_current(self):
        for name in REAL_UNDATED:
            self.assertFalse(semester.is_current(name, AUG_2026), name)

    def test_split_partitions_the_real_list(self):
        current, other, undated = semester.split_by_currency(
            REAL_CURRENT + REAL_PAST + REAL_UNDATED, AUG_2026)
        self.assertEqual(len(current), 7)
        self.assertEqual(len(other), 5)
        self.assertEqual(len(undated), 4)

    def test_the_same_list_in_january_flips_to_s2(self):
        """Time travel: in Jan 2027 the 26S1 courses stop being current."""
        current, other, _ = semester.split_by_currency(
            REAL_CURRENT + REAL_PAST, date(2027, 1, 15))
        self.assertEqual(current, [], "no 26S2 courses exist in this synthetic input yet")
        self.assertEqual(len(other), 12)

    def test_a_future_s2_course_becomes_current_in_january(self):
        name = "26S2-SC2006-SOFTWARE ENGINEERING"
        self.assertFalse(semester.is_current(name, AUG_2026))
        self.assertTrue(semester.is_current(name, date(2027, 1, 15)))
        self.assertTrue(semester.is_current(name, date(2027, 5, 1)))
        self.assertFalse(semester.is_current(name, date(2027, 8, 1)))


if __name__ == "__main__":
    unittest.main()
