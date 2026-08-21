"""Work out which NTU semester a course belongs to, and which one is current today.

Why not just match a prefix
---------------------------
Course names use several formats, so the parser accepts both compact semester prefixes and verbose academic-year labels. It also leaves names without a semester marker untouched rather than silently dropping a real course. Examples in tests cover compact, verbose, trailing-semester, and unlabelled forms.

Academic year mapping
---------------------
NTU's AY runs August to July. Semester 1 is roughly Aug-Dec, semester 2 Jan-May, with a
special term over Jun-Jul. So the year label always names the *starting* year:

    An August-to-December teaching period maps to semester 1 of that academic year.
    Jan 2027 - Jul 2027  ->  26S2
    Aug 2027             ->  27S1
"""
import re
from datetime import date
from typing import List, Optional, Tuple

# Month in which semester 1 of a new academic year begins.
S1_START_MONTH = 8

Semester = Tuple[int, int]  # (two-digit AY start year, semester number)

# 26S1 / 25S2 - the dominant form. Anchored so it cannot match inside a course code.
_COMPACT = re.compile(r"(?<![0-9A-Za-z])(\d{2})S([12])(?![0-9A-Za-z])")
# AY2026-2027, Semester 1, ...
_VERBOSE = re.compile(r"AY\s*(\d{4})\s*[-/]\s*\d{2,4}\s*,?\s*Semester\s*([12])", re.I)
# ... AY2025/26 SEM 2
_TRAILING = re.compile(r"AY\s*(\d{4})\s*[-/]\s*\d{2,4}\s*SEM(?:ESTER)?\s*([12])", re.I)


def current_semester(today: Optional[date] = None) -> Semester:
    """The semester in progress on ``today``, as (yy, sem).

    Jun-Jul is the special term; it belongs to the academic year that is still ending,
    so it keeps reporting that year's semester 2 until the new S1 begins in August.
    """
    today = today or date.today()
    if today.month >= S1_START_MONTH:
        return (today.year % 100, 1)
    return ((today.year - 1) % 100, 2)


def format_semester(sem: Semester) -> str:
    return "{:02d}S{}".format(sem[0], sem[1])


def parse_course_semester(name: str) -> Optional[Semester]:
    """Extract the semester a course name refers to, or None if it states none."""
    if not name:
        return None

    # Verbose and trailing forms carry a full 4-digit year, so they are unambiguous;
    # check them before the compact form, whose 2-digit year could also appear inside
    # a longer date string.
    for pattern in (_VERBOSE, _TRAILING):
        m = pattern.search(name)
        if m:
            return (int(m.group(1)) % 100, int(m.group(2)))

    m = _COMPACT.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def is_current(name: str, today: Optional[date] = None) -> bool:
    """True if this course is labelled with the semester currently in progress."""
    parsed = parse_course_semester(name)
    return parsed is not None and parsed == current_semester(today)


def split_by_currency(names: List[str], today: Optional[date] = None):
    """Partition course names into (current, other, undated).

    ``undated`` is kept separate rather than lumped into ``other`` because those are
    courses whose semester genuinely cannot be determined - admin and compliance
    modules, mostly - and the caller may reasonably want to see or keep them.
    """
    now = current_semester(today)
    current, other, undated = [], [], []
    for n in names:
        parsed = parse_course_semester(n)
        if parsed is None:
            undated.append(n)
        elif parsed == now:
            current.append(n)
        else:
            other.append(n)
    return current, other, undated
