"""NTU academic calendar: which teaching week a date falls in.

A timetable row says "Wk2-13" or "Wk1,3,5,7,9,11,13"; that only becomes a real date
once you know when Teaching Week 1 starts. This module holds that mapping.

Source
------
NTU's published Academic Calendar (Semester) for AY2026-27, dated 10 March 2026:
https://www.ntu.edu.sg/admissions/matriculation/academic-calendars

Semester 1 2026 reads off that calendar as:

    Orientation      3 - 7 Aug 2026
    Teaching Wk 1-7  10 Aug - 25 Sep
    Recess           28 Sep - 4 Oct
    Teaching Wk 8-13 5 Oct - 13 Nov
    Revision         16 - 21 Nov
    Examinations     23 - 28 Nov

Cross-checked against a registration record whose exams fall 23, 24 and 26 Nov 2026.

Only semesters that have been verified against the published calendar are encoded.
Anything else returns None rather than a guess - a wrong week number would silently
mis-file a class, which is worse than showing nothing.
"""
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

TOTAL_TEACHING_WEEKS = 13


class Semester:
    """One semester's shape, expressed as the Monday of each teaching week."""

    def __init__(self, key: str, label: str, week1_monday: date,
                 recess_after_week: int, revision: Tuple[date, date],
                 exams: Tuple[date, date]):
        self.key = key
        self.label = label
        self.week1_monday = week1_monday
        self.recess_after_week = recess_after_week
        self.revision = revision
        self.exams = exams

    def monday_of(self, week: int) -> Optional[date]:
        """Monday of the given teaching week, accounting for the recess break."""
        if not 1 <= week <= TOTAL_TEACHING_WEEKS:
            return None
        offset = week - 1
        if week > self.recess_after_week:
            offset += 1  # the recess week occupies a calendar week of its own
        return self.week1_monday + timedelta(weeks=offset)

    def week_of(self, when: date) -> Optional[int]:
        """Teaching week containing ``when``, or None outside teaching weeks."""
        for week in range(1, TOTAL_TEACHING_WEEKS + 1):
            monday = self.monday_of(week)
            if monday and monday <= when <= monday + timedelta(days=6):
                return week
        return None

    def recess_monday(self) -> date:
        return self.week1_monday + timedelta(weeks=self.recess_after_week)

    def phase_of(self, when: date) -> str:
        """A human label for whatever part of the semester ``when`` sits in."""
        week = self.week_of(when)
        if week:
            return "Teaching Week {}".format(week)
        recess = self.recess_monday()
        if recess <= when <= recess + timedelta(days=6):
            return "Recess Week"
        if self.revision[0] <= when <= self.revision[1]:
            return "Revision Week"
        if self.exams[0] <= when <= self.exams[1]:
            return "Examinations"
        if when < self.week1_monday:
            return "Before semester"
        return "Outside teaching weeks"

    def all_weeks(self) -> List[Dict]:
        out = []
        for w in range(1, TOTAL_TEACHING_WEEKS + 1):
            monday = self.monday_of(w)
            out.append({"week": w, "monday": monday.isoformat(),
                        "friday": (monday + timedelta(days=4)).isoformat()})
        return out


# Verified against the published AY2026-27 calendar.
SEMESTERS: Dict[str, Semester] = {
    "26S1": Semester(
        key="26S1",
        label="AY2026-27 Semester 1",
        week1_monday=date(2026, 8, 10),
        recess_after_week=7,
        revision=(date(2026, 11, 16), date(2026, 11, 21)),
        exams=(date(2026, 11, 23), date(2026, 11, 28)),
    ),
}


def get(sem_key: str) -> Optional[Semester]:
    return SEMESTERS.get((sem_key or "").upper())


def week_of(when: date, sem_key: str = "26S1") -> Optional[int]:
    sem = get(sem_key)
    return sem.week_of(when) if sem else None


def phase_of(when: date, sem_key: str = "26S1") -> Optional[str]:
    sem = get(sem_key)
    return sem.phase_of(when) if sem else None


# ── "Wk" expressions from a timetable row ───────────────────────────────────

def parse_weeks(expr: str) -> Optional[List[int]]:
    """Expand a timetable week expression into the weeks it covers.

    Accepts the forms NTU actually prints::

        "Wk1-13"                 -> [1..13]
        "Wk2-13"                 -> [2..13]
        "Wk1,3,5,7,9,11,13"      -> [1,3,5,...]
        "Wk1-10"                 -> [1..10]
        ""                       -> None, meaning "every teaching week"
    """
    if not expr:
        return None
    body = expr.upper().replace("TEACHING", "").replace("WK", "").strip()
    if not body:
        return None

    weeks: List[int] = []
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            weeks.extend(range(lo_i, hi_i + 1))
        else:
            try:
                weeks.append(int(part))
            except ValueError:
                continue
    return sorted(set(w for w in weeks if 1 <= w <= TOTAL_TEACHING_WEEKS)) or None


def runs_in_week(expr: str, week: Optional[int]) -> bool:
    """Does a session with this week expression happen in ``week``?

    An unparseable or absent expression means "every week" - better to show a class
    that might not run than to hide one that does.
    """
    weeks = parse_weeks(expr)
    if weeks is None or week is None:
        return True
    return week in weeks
