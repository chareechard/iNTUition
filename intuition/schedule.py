"""Class schedule store for the Temporal Protocol panel.

Where the data comes from
-------------------------
Not from Blackboard: NTU keeps 29 per-course calendars there and publishes **no**
meeting times into any of them (verified live - zero items across a whole semester).
And not from STARS: ``AUS_STARS_PLANNER.planner`` is a form POST target behind separate
NTU domain credentials, and is closed outside registration windows.

So the schedule is imported once and cached. Two input shapes are accepted, both of
which a student can produce without any credentials being handed to this tool:

* the timetable text copied out of STARS / the NTU class schedule page
* an ``.ics`` calendar export

Stored beside the ledger so it travels with the download folder.
"""
import json
import os
import re
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

STORAGE_DIR = ".intuition"
SCHEDULE_FILENAME = "schedule.json"

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
DAY_ALIASES = {
    "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU",
    "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
    "M": "MON", "T": "TUE", "W": "WED", "TH": "THU", "F": "FRI",
}

# NTU rows look like:  SC2005  10160  LEC/STUDIO  LE1  MON  0930-1030  LT19  Wk1-13
_COURSE = re.compile(r"\b([A-Z]{2,4}\d{4})\b")
_TIME = re.compile(r"\b(\d{4})\s*[-–]\s*(\d{4})\b")
_INDEX = re.compile(r"\b(\d{5})\b")
_WEEKS = re.compile(r"\b(?:teaching\s*)?wk\s*([0-9,\-\s]+)", re.I)
_TYPES = ("LEC", "TUT", "LAB", "SEM", "STUDIO", "DES", "PRJ", "EXAM", "ONLINE")


def schedule_path(download_root: str) -> str:
    return os.path.join(download_root, STORAGE_DIR, SCHEDULE_FILENAME)


def _norm_day(token: str) -> Optional[str]:
    t = token.strip().upper().rstrip(".")
    if t in DAYS:
        return t
    return DAY_ALIASES.get(t)


def _hhmm(raw: str) -> str:
    return "{}:{}".format(raw[:2], raw[2:])


def parse_ntu_timetable(text: str) -> List[Dict]:
    """Parse timetable rows copied out of STARS or the class schedule page.

    Deliberately tolerant: the copy is whitespace- or tab-separated depending on where
    it came from, columns move around between NTU's own pages, and rows for the same
    course often omit the repeated course code. Anchors are the things that are
    unambiguous - a course code, a HHMM-HHMM range, and a day token - so a row is
    accepted whenever a day and a time can both be found.
    """
    sessions: List[Dict] = []
    current_course = ""
    current_index = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m_course = _COURSE.search(line)
        if m_course:
            current_course = m_course.group(1)
        m_index = _INDEX.search(line)
        if m_index:
            current_index = m_index.group(1)

        m_time = _TIME.search(line)
        if not m_time:
            continue

        tokens = re.split(r"[\t]+|\s{2,}|\s", line)
        day = next((d for d in (_norm_day(t) for t in tokens) if d), None)
        if not day:
            continue

        upper = line.upper()
        kind = next((t for t in _TYPES if t in upper), "")

        m_weeks = _WEEKS.search(line)
        weeks = ("Wk" + m_weeks.group(1).strip()) if m_weeks else ""

        # Venue is the column immediately after the time, in every NTU layout seen:
        #     ... DAY  HHMM-HHMM  VENUE  REMARK
        # Scanning backwards instead would pick up whatever trailing remark the row
        # carries - "Teaching Wk1-13" yields "Teaching", "Makeup Wk7" yields "Makeup".
        cand = [t.strip() for t in re.split(r"[\t]+|\s{2,}|\s", line) if t.strip()]
        venue = ""
        for i, tok in enumerate(cand):
            if not _TIME.search(tok):
                continue
            for nxt in cand[i + 1:]:
                if _WEEKS.match(nxt) or nxt.upper() in ("TEACHING", "MAKEUP", "WK"):
                    break
                venue = nxt
                break
            break

        sessions.append({
            "course": current_course,
            "index": current_index,
            "type": kind,
            "day": day,
            "start": _hhmm(m_time.group(1)),
            "end": _hhmm(m_time.group(2)),
            "venue": venue,
            "weeks": weeks,
        })

    return sessions


def parse_ics(text: str) -> List[Dict]:
    """Minimal iCalendar reader - enough for a timetable export, no dependency."""
    sessions: List[Dict] = []
    block: Dict[str, str] = {}
    in_event = False

    # Unfold RFC 5545 continuation lines before parsing.
    unfolded: List[str] = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    for line in unfolded:
        s = line.strip()
        if s == "BEGIN:VEVENT":
            in_event, block = True, {}
            continue
        if s == "END:VEVENT":
            in_event = False
            started = block.get("DTSTART")
            ended = block.get("DTEND")
            if started:
                try:
                    dt = datetime.strptime(started[:15], "%Y%m%dT%H%M%S")
                except ValueError:
                    block = {}
                    continue
                try:
                    dt_end = datetime.strptime((ended or "")[:15], "%Y%m%dT%H%M%S")
                except ValueError:
                    dt_end = dt + timedelta(hours=1)
                summary = block.get("SUMMARY", "")
                m = _COURSE.search(summary.upper())
                upper = summary.upper()
                sessions.append({
                    "course": m.group(1) if m else summary[:12],
                    "index": "",
                    "type": next((t for t in _TYPES if t in upper), ""),
                    "day": DAYS[dt.weekday()],
                    "start": dt.strftime("%H:%M"),
                    "end": dt_end.strftime("%H:%M"),
                    "venue": block.get("LOCATION", ""),
                    "weeks": "",
                })
            block = {}
            continue
        if in_event and ":" in s:
            key, value = s.split(":", 1)
            block[key.split(";")[0].upper()] = value

    return sessions


# ── STARS "Course(s) Registered" printout ───────────────────────────────────
# That page is a time x day grid, not one row per class, and each cell reads like:
#     SC2005 LEC/STU SCL2 LT2A 1230to1320;
#     MH2500 TUT SPMS1 LHS-TR+53 0930to1020-Wk2-13;
# The day is the column, which is lost the moment the PDF is flattened to text - but
# every cell carries its own "HHMMtoHHMM" so the cells can be recovered individually,
# and the column is recovered from the x position of the text on the page.
_CELL = re.compile(
    r"([A-Z]{2,4}\d{4})\s+"                      # course
    r"(LEC/STU|LEC|TUT|LAB|SEM|DES|PRJ)\s+"      # component
    r"(\S+)\s+"                                  # group, e.g. SCL2 / T087 / LE1
    r"([A-Za-z0-9+\-]+)\s+"                      # venue, e.g. LT2A / LHS-TR+53
    r"(\d{4})to(\d{4})"                          # time
    r"(?:\s*-\s*Wk([0-9,\-]+))?",                # optional week expression
    re.I,
)

# "Academic Year 2026,Semester 1" -> 26S1. Taking the semester from the document
# rather than from today's date is what lets the dashboard notice that a stored
# schedule belongs to a semester that has since ended.
_SEMESTER_HEADING = re.compile(
    r"Academic\s+Year\s*(\d{4})\s*[,/-]?\s*Semester\s*([12])", re.I)

_REGISTERED = re.compile(
    r"(\d{5})\s+([A-Z]{2,4}\d{4})\s+(.+?)\s+(\d)\s+Registered\s+"
    r"(Not Applicable|\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{4}to\d{4})", re.I | re.S)


def parse_stars_pdf(path: str) -> Dict:
    """Read a STARS 'Course(s) Registered' PDF into sessions, exams and courses.

    The page is a time x day grid, so the day is carried by the *column*, not by any
    text in the cell. pypdf's layout extraction preserves that grid as character
    offsets, which makes the header row a self-calibrating column ruler - far more
    robust than inferring columns from raw x coordinates, since the header cells on
    this document carry no usable transform at all.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ScheduleError(
            "Reading a STARS PDF needs pypdf:\n    pip install pypdf\n({})".format(e))

    text = ""
    for page in PdfReader(path).pages:
        text += (page.extract_text(extraction_mode="layout") or "") + "\n"
    lines = text.splitlines()

    # Locate the header row and the character offset of each day column.
    header_idx, offsets = None, {}
    for i, line in enumerate(lines):
        upper = line.upper()
        if "TIME" in upper and "MON" in upper and "TUE" in upper:
            header_idx = i
            for day in DAYS:
                pos = upper.find(day)
                if pos >= 0:
                    offsets[day] = pos
            break
    if header_idx is None or len(offsets) < 3:
        raise ScheduleError(
            "This does not look like a STARS 'Course(s) Registered' printout: no "
            "TIME/DAY header row found.")

    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    # Column boundaries are the midpoints between adjacent day headers; the first
    # starts just after the TIME column.
    bounds = []
    for i, (day, pos) in enumerate(ordered):
        lo = (ordered[i - 1][1] + pos) // 2 if i else max(0, pos - 14)
        hi = (ordered[i + 1][1] + pos) // 2 if i + 1 < len(ordered) else 10_000
        bounds.append((day, lo, hi))

    # A cell's text wraps over several lines and always terminates with ";", so
    # accumulate per column until one arrives.
    buffers = {day: "" for day, _, _ in bounds}
    sessions: List[Dict] = []

    for line in lines[header_idx + 1:]:
        if "Academic Year" in line and "Semester" in line:
            break  # the grid has ended; the registered-courses table follows
        for day, lo, hi in bounds:
            chunk = line[lo:hi].strip() if len(line) > lo else ""
            if not chunk:
                continue
            buffers[day] = (buffers[day] + " " + chunk).strip()
            while ";" in buffers[day]:
                cell, _, rest = buffers[day].partition(";")
                buffers[day] = rest.strip()
                m = _CELL.search(cell)
                if m:
                    sessions.append(_session_from_match(m, day))

    # Registered-courses table: index, code, title, AUs, and any exam slot.
    flat = " ".join(text.split())
    courses, exams = [], []
    for m in _REGISTERED.finditer(flat):
        idx, code, title, aus, exam = m.groups()
        courses.append({"index": idx, "course": code.upper(),
                        "title": " ".join(title.split()), "aus": int(aus)})
        if "not applicable" not in exam.lower():
            exams.append({"course": code.upper(), "when": " ".join(exam.split())})

    by_code = {c["course"]: c["index"] for c in courses}
    for s in sessions:
        s["index"] = s["index"] or by_code.get(s["course"], "")

    sem = ""
    m_sem = _SEMESTER_HEADING.search(flat)
    if m_sem:
        sem = "{:02d}S{}".format(int(m_sem.group(1)) % 100, m_sem.group(2))

    return {"sessions": dedupe(sessions), "courses": courses, "exams": exams,
            "semester": sem}


def _session_from_match(m, day: str) -> Dict:
    kind = m.group(2).upper()
    kind = "LEC" if kind.startswith("LEC") else kind
    return {
        "course": m.group(1).upper(),
        "index": "",
        "type": kind,
        "group": m.group(3),
        "day": day,
        "start": _hhmm(m.group(5)),
        "end": _hhmm(m.group(6)),
        "venue": m.group(4),
        "weeks": ("Wk" + m.group(7)) if m.group(7) else "",
    }


class ScheduleError(Exception):
    pass


def parse(text: str) -> List[Dict]:
    """Detect the input shape and parse accordingly."""
    if "BEGIN:VEVENT" in text.upper():
        return parse_ics(text)
    return parse_ntu_timetable(text)


def parse_file(path: str) -> Dict:
    """Parse any supported schedule file into {sessions, exams, courses}."""
    if path.lower().endswith(".pdf"):
        return parse_stars_pdf(path)
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    m = _SEMESTER_HEADING.search(text)
    sem = "{:02d}S{}".format(int(m.group(1)) % 100, m.group(2)) if m else ""
    return {"sessions": parse(text), "exams": [], "courses": [], "semester": sem}


def dedupe(sessions: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for s in sessions:
        key = (s["course"], s["day"], s["start"], s["end"], s.get("venue", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return sorted(out, key=lambda s: (DAYS.index(s["day"]), s["start"]))


class Schedule:
    """The stored week, plus the queries the Temporal Protocol panel needs."""

    def __init__(self, download_root: str):
        self.path = schedule_path(download_root)
        self.sessions: List[Dict] = []
        self.exams: List[Dict] = []
        self.courses: List[Dict] = []
        self.semester: str = ""
        self.imported_at: Optional[str] = None
        self.overrides: List[Dict] = []
        self.important_dates: List[Dict] = []
        self._loaded_mtime_ns = 0
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
            self.sessions = data.get("sessions", [])
            self.exams = data.get("exams", [])
            self.courses = data.get("courses", [])
            self.semester = data.get("semester", "")
            self.imported_at = data.get("imported_at")
            self.overrides = data.get("overrides", [])
            self.important_dates = data.get("important_dates", [])
            self._loaded_mtime_ns = os.stat(self.path).st_mtime_ns
        except (ValueError, OSError):
            self.sessions = []

    def reload_if_changed(self) -> bool:
        """Reload when another process updated the persistent Temporal store."""
        try:
            modified = os.stat(self.path).st_mtime_ns
        except OSError:
            return False
        if modified == self._loaded_mtime_ns:
            return False
        self.load()
        return True

    def save(self):
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        payload = {
            "imported_at": self.imported_at
            or datetime.now().isoformat(timespec="seconds"),
            "sessions": self.sessions,
            "exams": self.exams,
            "courses": self.courses,
            "semester": self.semester,
            "overrides": self.overrides,
            "important_dates": self.important_dates,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, self.path)
        self._loaded_mtime_ns = os.stat(self.path).st_mtime_ns

    def replace(self, sessions: List[Dict], exams=None, courses=None, semester=None):
        self.sessions = dedupe(sessions)
        if exams is not None:
            self.exams = exams
        if courses is not None:
            self.courses = courses
        if semester:
            self.semester = semester
        self.imported_at = datetime.now().isoformat(timespec="seconds")

    def for_day(self, day_name: str, week: Optional[int] = None) -> List[Dict]:
        """Sessions on a day, optionally only those running in ``week``."""
        from intuition import academic_calendar as cal
        rows = [s for s in self.sessions if s["day"] == day_name]
        if week is None:
            return rows
        return [s for s in rows if cal.runs_in_week(s.get("weeks", ""), week)]

    def week(self, teaching_week: Optional[int] = None) -> Dict[str, List[Dict]]:
        return {d: self.for_day(d, teaching_week) for d in DAYS}

    def set_announcement_overrides(self, changes: List[Dict]):
        """Replace announcement-derived date exceptions with a validated set."""
        allowed = {"cancel", "change", "add", "pattern"}
        clean = []
        for change in changes:
            action = str(change.get("action") or "").lower()
            course = str(change.get("course") or "").strip().upper()
            if action not in allowed or not course:
                continue
            if action == "pattern":
                if not str(change.get("weeks") or "").strip():
                    continue
            else:
                try:
                    datetime.strptime(str(change.get("date") or ""), "%Y-%m-%d")
                except ValueError:
                    continue
            row = {k: str(change.get(k) or "").strip() for k in
                   ("date", "course", "action", "type", "old_start", "start",
                    "end", "venue", "weeks", "source_id", "source_title", "reason")}
            clean.append(row)
        self.overrides = clean
        return len(clean)

    def set_announcement_important_dates(self, events: List[Dict]):
        """Merge assessment milestones, letting announcements amend document dates."""
        allowed = {"midterm", "final", "quiz", "presentation", "oral", "assignment"}
        clean = []
        for event in events:
            try:
                datetime.strptime(str(event.get("date") or ""), "%Y-%m-%d")
            except ValueError:
                continue
            kind = str(event.get("kind") or "").lower()
            course = str(event.get("course") or "").strip().upper()
            if kind not in allowed or not course:
                continue
            row = {key: str(event.get(key) or "").strip() for key in
                   ("source_id", "source_title", "date", "course", "kind",
                    "start", "end", "venue", "details")}
            row["course"], row["kind"] = course, kind
            text = "{} {}".format(row["details"], row["source_title"]).lower()
            labels = (
                r"lecture\s+quiz\s*\d+", r"lab\s+quiz\s*\d+",
                r"concept\s+quiz", r"algorithm\s+quiz", r"test\s*\d+",
                r"quiz\s*\d+", r"assignment\s*\d+", r"midterm(?:\s+exam)?",
                r"final(?:\s+exam)?", r"presentation\s*\d*")
            label = next((match.group(0) for pattern in labels
                          for match in [re.search(pattern, text)] if match), row["kind"])
            row["assessment_key"] = "{}:{}".format(
                course, re.sub(r"\s+", "", label))
            row["origin"] = ("drive" if row["source_id"].startswith("drive:")
                             else "announcement")
            clean.append(row)
        # Announcements rotate out of the seven-day feed long before an assessment
        # may occur. Keep future milestones, while a reprocessed source replaces its
        # previous extraction and past dates naturally expire.
        incoming_sources = {row["source_id"] for row in clean if row["source_id"]}
        announcement_keys = {row["assessment_key"] for row in clean
                             if row["origin"] == "announcement"}
        existing_by_key = {row.get("assessment_key"): row
                           for row in self.important_dates
                           if row.get("assessment_key")}
        retained = []
        for row in self.important_dates:
            if row.get("date", "") < date.today().isoformat():
                continue
            if row.get("source_id") in incoming_sources:
                continue
            if row.get("assessment_key") in announcement_keys:
                continue
            retained.append(row)
        retained_keys = {row.get("assessment_key") for row in retained}
        accepted = []
        for row in clean:
            previous = existing_by_key.get(row["assessment_key"])
            if row["origin"] == "drive" and row["assessment_key"] in retained_keys:
                continue
            if row["origin"] == "announcement" and previous:
                row["amended"] = True
                row["amended_from"] = previous.get("source_title", "")
            accepted.append(row)
        merged = retained + accepted
        unique = {}
        for row in merged:
            unique[(row["course"], row["date"], row["kind"], row["start"],
                    row["details"])] = row
        self.important_dates = sorted(
            unique.values(), key=lambda row: (row["date"], row["start"]))
        return len(clean)

    def assessment_timeline(self, semester: str) -> List[Dict]:
        """Return assessments enriched with authoritative academic-calendar labels."""
        from intuition import academic_calendar as cal
        timeline = []
        for event in self.important_dates:
            row = dict(event)
            try:
                when = datetime.strptime(str(row.get("date") or ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            row["teaching_week"] = cal.week_of(when, semester)
            row["phase"] = cal.phase_of(when, semester)
            row["day"] = when.strftime("%a").upper()
            row["timeslot"] = ("{}{}".format(
                row.get("start") or "",
                "-{}".format(row["end"]) if row.get("end") else "")
                or "Time TBA")
            timeline.append(row)
        return sorted(timeline, key=lambda row: (row["date"], row.get("start", "")))

    def dynamic_week(self, monday, teaching_week: Optional[int] = None):
        """Overlay dated professor-announcement exceptions on the recurring week."""
        from intuition import academic_calendar as cal
        effective = [dict(row) for row in self.sessions]
        for change in self.overrides:
            if change.get("action") != "pattern":
                continue
            course = change["course"].upper()
            kind = change.get("type", "").upper()
            old_start = change.get("old_start", "")
            matches = [row for row in effective
                       if course in str(row.get("course", "")).upper()
                       and (not kind or kind in str(row.get("type", "")).upper())
                       and (not old_start or row.get("start") == old_start)]
            if len(matches) == 1:
                matches[0]["weeks"] = change["weeks"]
                matches[0].update(dynamic=True, change=change)
        week = {day: [row for row in effective if row["day"] == day and
                      (teaching_week is None or cal.runs_in_week(
                          row.get("weeks", ""), teaching_week))]
                for day in DAYS}
        for change in self.overrides:
            if change.get("action") == "pattern":
                continue
            try:
                when = datetime.strptime(change["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                continue
            offset = (when - monday).days
            if offset < 0 or offset >= len(DAYS):
                continue
            day = DAYS[offset]
            rows = week[day]
            course = change["course"].upper()
            kind = change.get("type", "").upper()
            old_start = change.get("old_start", "")
            matches = [row for row in rows
                       if course in str(row.get("course", "")).upper()
                       and (not kind or kind in str(row.get("type", "")).upper())
                       and (not old_start or row.get("start") == old_start)]
            action = change["action"]
            if action == "cancel":
                week[day] = [row for row in rows if row not in matches]
            elif action == "change" and len(matches) == 1:
                row = matches[0]
                for key in ("start", "end", "venue"):
                    if change.get(key):
                        row[key] = change[key]
                row.update(dynamic=True, change=change)
            elif action == "add" and change.get("start") and change.get("end"):
                week[day].append({"course": change["course"],
                                  "type": change.get("type", ""), "day": day,
                                  "start": change["start"], "end": change["end"],
                                  "venue": change.get("venue", ""), "weeks": "",
                                  "dynamic": True, "change": change})
            week[day].sort(key=lambda row: row.get("start", ""))
        return week

    def upcoming(self, now: Optional[datetime] = None, limit: int = 5) -> List[Dict]:
        """The next few sessions from ``now``, rolling into following days."""
        now = now or datetime.now()
        out: List[Dict] = []
        for offset in range(0, 8):
            when = now.date() + timedelta(days=offset)
            for s in self.for_day(DAYS[when.weekday()]):
                try:
                    h, m = (int(x) for x in s["start"].split(":"))
                except ValueError:
                    continue
                starts = datetime.combine(when, time(h, m))
                if starts < now:
                    continue
                item = dict(s)
                item["date"] = when.isoformat()
                item["in_minutes"] = int((starts - now).total_seconds() // 60)
                out.append(item)
                if len(out) >= limit:
                    return sorted(out, key=lambda x: x["in_minutes"])
        return sorted(out, key=lambda x: x["in_minutes"])

    def __len__(self) -> int:
        return len(self.sessions)
