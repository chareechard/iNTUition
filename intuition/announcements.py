"""Persistent, course-aggregated iNTUition announcements."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from bs4 import BeautifulSoup

from intuition import rest
from intuition import academic_calendar
from intuition import ai_provider

STORAGE_DIR = ".intuition"
FILENAME = "announcements.json"
SUMMARY_VERSION = 2
RETENTION_DAYS = 7
URL = "https://ntulearn.ntu.edu.sg/learn/api/public/v1/courses/{course_id}/announcements"
SCHEDULE_ACTION = re.compile(
    r"\b(cancel+ed|postponed|rescheduled|moved|new venue|make.?up)\b|"
    r"\b(class|lecture|tutorial|seminar|lab|venue|time)\b.{0,60}\bchange(?:d)?\b|"
    r"\bchange(?:d)?\b.{0,60}\b(class|lecture|tutorial|seminar|lab|venue|time)\b",
    re.IGNORECASE | re.DOTALL)
SCHEDULE_DATED_TITLE = re.compile(
    r"\b(class|lecture|tutorial|seminar|lab|lesson)s?\b.*\b(today|tomorrow|"
    r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|"
    r"sun(?:day)?|\d{1,2}[:.]\d{2}|\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?))\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b", re.IGNORECASE)
SCHEDULE_PATTERN = re.compile(
    r"\b(?:tutorial|lab|lecture|class)(?:s| session)?\b.{0,100}\b(?:start|first|begin)"
    r".{0,80}(?:\bweek\s*\d+|\b(?:first|second|third|fourth|fifth|sixth|seventh|"
    r"eighth|ninth|tenth|eleventh|twelfth|thirteenth)\s+week\b)|"
    r"\b(?:start|first|begin).{0,80}\b(?:tutorial|lab|lecture|class).{0,100}"
    r"(?:\bweek\s*\d+|\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|"
    r"ninth|tenth|eleventh|twelfth|thirteenth)\s+week\b)", re.IGNORECASE | re.DOTALL)
IMPORTANT_DATE_PATTERN = re.compile(
    r"\b(mid[ -]?term|final(?:\s+exam)?|exam(?:ination)?|quiz|test|"
    r"presentation|demo|oral|viva|defen[cs]e|assignment|homework|coursework|"
    r"project|report|essay|graded)\b", re.IGNORECASE)

SCHEDULE_CHANGE_SCHEMA = json.dumps({
    "type": "array", "items": {"type": "object", "additionalProperties": False,
    "properties": {key: {"type": "string"} for key in
                   ("source_id", "source_title", "course", "date", "action", "type",
                    "old_start", "start", "end", "venue", "weeks", "reason")},
    "required": ["source_id", "source_title", "course", "date", "action", "type",
                 "old_start", "start", "end", "venue", "weeks", "reason"]}})


def explicit_week_patterns(item: Dict, sessions: List[Dict]) -> List[Dict]:
    """Deterministic guardrail for plainly stated recurring-session starts."""
    text = "{}\n{}".format(item.get("title", ""), item.get("body", ""))
    words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
             "eleventh": 11, "twelfth": 12, "thirteenth": 13}
    token = r"(?:\d+|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth)"
    prefix = r"(?:first\s+)?(lab|tutorial|lecture)(?:s|\s+session)?[^.\n]{0,100}?(?:start|begin|will\s+be)[^.\n]{0,60}?"
    patterns = []
    # "during week 6 or 7" / "starts at week 2"
    for match in re.finditer(prefix + r"week\s*(" + token + r")(?:\s+or\s+(" + token + r"))?",
                             text, re.IGNORECASE):
        patterns.append((match, match.group(1), match.group(2), match.group(3)))
    # "starts from either third week or fourth week"
    for match in re.finditer(prefix + r"(?:either\s+)?(" + token + r")\s+week"
                             r"(?:\s+or\s+(" + token + r")\s+week)?",
                             text, re.IGNORECASE):
        patterns.append((match, match.group(1), match.group(2), match.group(3)))
    code_match = re.search(r"[A-Z]{2,4}\d{4}", item.get("course", "").upper())
    code = code_match.group(0) if code_match else ""
    out = []
    for match, raw_kind, first_token, second_token in patterns:
        kind = raw_kind.upper()[:3]  # tutorial -> TUT, lecture -> LEC
        rows = [row for row in sessions
                if (not code or code in str(row.get("course", "")).upper())
                and kind in str(row.get("type", "")).upper()]
        if len(rows) != 1:
            continue
        row = rows[0]
        alternatives = []
        for value in (first_token, second_token):
            if not value:
                continue
            alternatives.append(int(value) if value.isdigit() else words[value.lower()])
        baseline = academic_calendar.parse_weeks(row.get("weeks", ""))
        if baseline:
            compatible = [week for week in alternatives if week in baseline]
            first = min(compatible or alternatives)
            weeks = [week for week in baseline if week >= first]
        else:
            first = min(alternatives)
            weeks = list(range(first, academic_calendar.TOTAL_TEACHING_WEEKS + 1))
        if baseline and weeks == baseline:
            continue
        expression = "Wk" + ",".join(str(week) for week in weeks)
        if expression == row.get("weeks"):
            continue
        out.append({"source_id": item["id"], "source_title": item["title"],
                    "course": code or row.get("course", ""), "date": "",
                    "action": "pattern", "type": row.get("type", kind),
                    "old_start": row.get("start", ""), "start": "", "end": "",
                    "venue": "", "weeks": expression,
                    "reason": match.group(0).strip()})
    return out


def _path(root: str) -> str:
    return os.path.join(root, STORAGE_DIR, FILENAME)


def _timestamp(value: str):
    """Parse Blackboard timestamps as aware UTC datetimes."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _released(item: Dict):
    # ``created`` is the release date. Fall back for older/cached payloads where
    # Blackboard supplied only a modification or availability-start timestamp.
    return (_timestamp(item.get("created")) or _timestamp(item.get("starts"))
            or _timestamp(item.get("modified")))


def _recent(items: List[Dict], now=None) -> List[Dict]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    return [item for item in items
            if _released(item) is None or _released(item) >= cutoff]


def clean(item: Dict, course: Dict) -> Dict:
    soup = BeautifulSoup(item.get("body") or "", "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        links.append({"title": anchor.get_text(" ", strip=True) or anchor["href"],
                      "url": anchor["href"]})
    # Plain URLs are common in announcements without an <a>; linkify those client-side
    # later, but retain the readable body here.
    text = soup.get_text("\n", strip=True)
    known = {link["url"] for link in links}
    for url in re.findall(r"https?://[^\s<>]+", text):
        url = url.rstrip(".,);]")
        if url not in known:
            known.add(url)
            links.append({"title": url, "url": url})
    availability = item.get("availability") or {}
    duration = availability.get("duration") or {}
    return {
        "id": str(item.get("id") or ""), "course_id": course["id"],
        "course": course["name"], "title": item.get("title") or "Announcement",
        "body": text, "links": links, "created": item.get("created") or "",
        "modified": item.get("modified") or "", "starts": duration.get("start"),
        "ends": duration.get("end"),
    }


class Feed:
    def __init__(self, root: str):
        self.root = root
        self.path = _path(root)
        self.items: List[Dict] = []
        self.read = set()
        self.synced_at = ""
        self.summary = None
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items") or []
            self.read = set(data.get("read") or [])
            self.synced_at = data.get("synced_at") or ""
            self.summary = data.get("summary")
        except (OSError, ValueError, TypeError):
            self.items, self.read, self.synced_at, self.summary = [], set(), "", None

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items, "read": sorted(self.read),
                       "synced_at": self.synced_at, "summary": self.summary}, f, indent=2)
        os.replace(tmp, self.path)

    def sync(self, token: str, courses: List[Dict]) -> List[str]:
        fetched, errors = [], []
        for course in courses:
            try:
                rows = rest._get_paged(token, URL.format(course_id=course["id"]),
                                       params={"limit": rest.PAGE_LIMIT})
                fetched.extend(clean(row, course) for row in rows
                               if not row.get("draft") and row.get("id"))
            except Exception as exc:  # one course cannot hide the others
                errors.append("{}: {}".format(course["name"], exc))
        # Merge by id so a temporarily empty/failed Blackboard response cannot erase
        # announcements that are still within their seven-day display window.
        merged = {item["id"]: item for item in self.items if item.get("id")}
        merged.update((item["id"], item) for item in fetched)
        items = _recent(list(merged.values()))
        items.sort(key=lambda x: x.get("modified") or x.get("created") or "", reverse=True)
        self.items = items
        self.synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        live = {item["id"] for item in items}
        self.read.intersection_update(live)
        if self.summary and (self.summary.get("feed_ids") != sorted(live)
                             or self.summary.get("version") != SUMMARY_VERSION):
            self.summary = None
        self.save()
        return errors

    def mark_read(self, item_id: str, value=True) -> bool:
        if item_id not in {item["id"] for item in self.items}:
            return False
        if value:
            self.read.add(item_id)
        else:
            self.read.discard(item_id)
        self.save()
        return True

    def snapshot(self) -> Dict:
        items = [dict(item, read=item["id"] in self.read) for item in self.items]
        return {"items": items, "total": len(items),
                "unread": sum(not item["read"] for item in items),
                "courses": sorted({item["course"] for item in items}),
                "synced_at": self.synced_at, "summary": self.summary}

    def summarize(self, preferred=None) -> Dict:
        source = [item for item in self.items if item["id"] not in self.read] or self.items
        if not source:
            raise ValueError("No announcements to summarize")
        blocks = []
        for item in source[:30]:
            blocks.append("COURSE: {}\nTITLE: {}\nDATE: {}\nBODY:\n{}".format(
                item["course"], item["title"], item.get("modified") or item.get("created"),
                item["body"][:4000]))
        result = ai_provider.complete_tier(
            # High volume, structured, low stakes - the tier docs/ai-infrastructure.md
            # assigns announcements to. Ladders through OmniRoute's measured routes
            # (auto/coding:free, then auto/fast) with a degeneracy guard on every rung,
            # instead of landing on whichever unmeasured model "auto" happens to route
            # to - which is what produced generic, thin summaries before this.
            "bulk",
            "\n\n--- ANNOUNCEMENT ---\n".join(blocks),
            system=("You are the TL;DR module for a university student dashboard. Turn the "
                    "supplied course announcements into useful decision-ready information, not "
                    "a paraphrase of their titles. Use at most 220 words and exactly these "
                    "headings when applicable: ACTION NOW, PREPARE, FYI. Under each heading use "
                    "one-line bullets. Every bullet must begin with the course code and state "
                    "what changed or matters, what the student must do, and the exact deadline, "
                    "date, venue, link purpose, or affected teaching week when supplied. Put "
                    "urgent items first. Merge duplicates and omit greetings and administrative "
                    "filler. Never invent a requirement, date, or recommendation. Omit empty "
                    "headings."),
            # Prefer the dashboard's selected route. In automatic mode the shared
            # adapter tries OmniRoute first and can recover through the logged-in
            # Claude CLI when the local gateway is installed but unhealthy.
            preferred=preferred,
            download_root=self.root)
        self.summary = dict(result, version=SUMMARY_VERSION,
                            source_ids=sorted(item["id"] for item in source),
                            feed_ids=sorted(item["id"] for item in self.items),
                            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.save()
        return self.summary

    def detect_schedule_changes(self, sessions: List[Dict], preferred=None) -> List[Dict]:
        """Extract explicit, dated timetable exceptions from professor announcements."""
        if not self.items or not sessions:
            return []
        candidates = [item for item in self.items
                      if SCHEDULE_ACTION.search("{}\n{}".format(
                          item.get("title", ""), item.get("body", "")))
                      or SCHEDULE_DATED_TITLE.search(item.get("title", ""))
                      or SCHEDULE_PATTERN.search(item.get("body", ""))]
        if not candidates:
            return []
        system = (
            "Identify only explicit professor-announced changes to class meetings. "
            "Return a JSON array only, no fences. Each object must contain: source_id, "
            "source_title, course, date (YYYY-MM-DD or empty), action "
            "(cancel|change|add|pattern), type, old_start, start, end, venue, weeks, "
            "reason. Resolve relative dates from TODAY. Use action pattern when an "
            "announcement changes which teaching weeks a recurring class runs; put the "
            "new expression in weeks (for example Wk7,9,11,13). Reconcile alternatives "
            "such as week 6 or 7 with the student's baseline alternating-week pattern. "
            "For change, old_start identifies the baseline meeting and start is the new "
            "time. Never infer a change from reminders, assessment deadlines, recordings, "
            "or vague wording. If the date or intended class is ambiguous, omit it. "
            "Return [] when there are no certain schedule changes.")
        out, failures = [], []
        handled = set()
        for item in candidates[:12]:
            certain = explicit_week_patterns(item, sessions)
            if certain:
                out.extend(certain)
                handled.add(item["id"])
        # One announcement per call isolates malformed or adversarial content: a single
        # welcome post cannot consume the whole detector's time budget.
        for item in candidates[:12]:
            if item["id"] in handled:
                continue
            code_match = re.search(r"[A-Z]{2,4}\d{4}", item.get("course", "").upper())
            code = code_match.group(0) if code_match else ""
            relevant = [row for row in sessions
                        if not code or code in str(row.get("course", "")).upper()]
            baseline = ["{} {} {} {}-{} {}".format(
                row.get("course", ""), row.get("type", ""), row.get("day", ""),
                row.get("start", ""), row.get("end", ""), row.get("venue", ""))
                for row in relevant]
            prompt = "TODAY: {}\nBASE TIMETABLE:\n{}\n\nID: {}\nCOURSE: {}\nTITLE: {}\nBODY:\n{}".format(
                datetime.now().date().isoformat(), "\n".join(baseline), item["id"],
                item["course"], item["title"], item["body"][:3000])
            try:
                result = ai_provider.complete(
                    prompt, system, preferred=preferred, max_tokens=700,
                    download_root=self.root, timeout=12)
                text = str(result.get("text") or "").strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                                  flags=re.IGNORECASE)
                start = text.find("[")
                if start < 0:
                    raise ValueError("result has no JSON array")
                # Claude CLI can append a short explanation despite being told not to.
                # Decode exactly the first value and ignore prose after it.
                parsed, _end = json.JSONDecoder().raw_decode(text[start:])
                if not isinstance(parsed, list):
                    raise ValueError("result is not a JSON array")
                for row in parsed:
                    if not isinstance(row, dict) or row.get("source_id") != item["id"]:
                        continue
                    row["source_title"] = item["title"]
                    out.append(row)
            except Exception as exc:
                failures.append(str(exc))
        attempted = min(len(candidates), 12) - len(handled)
        if failures and len(failures) == attempted and not out:
            raise ValueError("Every schedule candidate failed: {}".format(failures[0]))
        return out

    def detect_important_dates(self, preferred=None) -> List[Dict]:
        """Extract explicitly dated assessments for the Temporal Protocol."""
        candidates = [item for item in self.items if IMPORTANT_DATE_PATTERN.search(
            "{}\n{}".format(item.get("title", ""), item.get("body", "")))]
        if not candidates:
            return []
        system = (
            "Extract only explicitly announced student assessment dates: midterms, final "
            "exams, quizzes/tests, presentations, demos, oral examinations, and graded "
            "assignment/homework/coursework/project/report/essay due dates. Return a "
            "JSON array only. Each object must contain source_id, source_title, course, "
            "date (YYYY-MM-DD), kind "
            "(midterm|final|quiz|presentation|oral|assignment), start, end, "
            "venue, and details. Resolve relative dates from TODAY. A submission deadline "
            "is not a presentation date unless the announcement explicitly says the "
            "presentation occurs then; classify it as assignment when the submitted work "
            "is graded. Exclude ungraded practice, optional work, and content-release "
            "dates. Omit tentative, ambiguous, or undated items and "
            "never invent a time or venue; use an empty string when absent. Return [].")
        out, failures = [], []
        allowed = {"midterm", "final", "quiz", "presentation", "oral", "assignment"}
        for item in candidates[:20]:
            prompt = "TODAY: {}\nID: {}\nCOURSE: {}\nTITLE: {}\nBODY:\n{}".format(
                datetime.now().date().isoformat(), item["id"], item["course"],
                item["title"], item["body"][:4000])
            try:
                result = ai_provider.complete(prompt, system, preferred=preferred,
                    max_tokens=700, download_root=self.root, timeout=12)
                text = str(result.get("text") or "").strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                                  flags=re.IGNORECASE)
                start = text.find("[")
                if start < 0:
                    raise ValueError("result has no JSON array")
                parsed, _end = json.JSONDecoder().raw_decode(text[start:])
                if not isinstance(parsed, list):
                    raise ValueError("result is not a JSON array")
                for row in parsed:
                    if not isinstance(row, dict) or row.get("source_id") != item["id"]:
                        continue
                    try:
                        datetime.strptime(str(row.get("date") or ""), "%Y-%m-%d")
                    except ValueError:
                        continue
                    kind = str(row.get("kind") or "").lower()
                    if kind not in allowed:
                        continue
                    clean_row = {key: str(row.get(key) or "").strip() for key in
                                 ("source_id", "course", "date", "kind", "start",
                                  "end", "venue", "details")}
                    clean_row["source_title"] = item["title"]
                    out.append(clean_row)
            except Exception as exc:
                failures.append(str(exc))
        if failures and len(failures) == min(len(candidates), 20) and not out:
            raise ValueError("Every important-date candidate failed: {}".format(failures[0]))
        unique = {}
        for row in out:
            unique[(row["course"].upper(), row["date"], row["kind"], row["start"],
                    row["details"])] = row
        return sorted(unique.values(), key=lambda row: (row["date"], row["start"]))
