"""Persistent, course-aggregated iNTUition announcements."""
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

from intuition import rest
from intuition import academic_calendar
from intuition import ai_provider
from intuition.persistence import atomic_json_dump

STORAGE_DIR = ".intuition"
FILENAME = "announcements.json"
SUMMARY_VERSION = 2
RETENTION_DAYS = 7
# Bump when the deterministic dedupe/scrub logic changes so a persisted feed is
# re-cleaned on the next load instead of carrying stale merges forward.
CLEAN_VERSION = 1
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

# ── Data cleaning ──────────────────────────────────────────────────────────────
# Blackboard bodies arrive wrapped in mail-merge chrome and the same post is
# often cross-listed into several course shells (each shell copy gets its own
# id). Everything below scrubs the noise and collapses those duplicates so the
# ticker shows one clean line per real announcement.
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_BOILERPLATE_LINE = re.compile(
    r"^\s*(?:"
    r"do not reply to this (?:e-?mail|message|announcement)"
    r"|please do not reply(?: to this (?:e-?mail|message))?"
    r"|this (?:is|was) an? (?:automated|automatic|system[- ]generated) "
    r"(?:e-?mail|message|notification|announcement).*"
    r"|you are receiving this (?:e-?mail|message|because).*"
    r"|(?:sent|posted|delivered) (?:from|via|by|using) (?:blackboard|ntulearn|"
    r"the ntulearn.*|black\s*board learn).*"
    r"|posted on\s*:.*"
    r"|<!--.*-->"
    r"|[-=_*]{4,}"
    r")\s*$", re.IGNORECASE)
_SIGNOFF_TAIL = re.compile(
    r"\n[ \t]*(?:best|kind|warm|many)?\s*(?:regards|wishes)\b[\s\S]{0,120}\Z"
    r"|\n[ \t]*(?:cheers|sincerely|thank you|thanks|yours (?:sincerely|faithfully|truly))"
    r"[ ,!.]*(?:\n[\s\S]{0,80})?\Z",
    re.IGNORECASE)
_DEAD_LINK_SCHEMES = ("mailto:", "javascript:", "tel:", "#")
_ANNOUNCEMENT_PAGE = re.compile(
    r"announcement_manager\.jsp|/webapps/blackboard/execute/announcement"
    r"|/ultra/courses/[^/]+/announcements", re.IGNORECASE)

_CODE_TOKEN = re.compile(r"\b(?:AY\d{4}|\d{2}S\d|[A-Z]{2,4}\d{4}[A-Z]?)\b")
_TITLE_PREFIX = re.compile(
    r"^\s*(?:re|fw|fwd)\s*:\s*|^\s*[\[(]?(?:" + _CODE_TOKEN.pattern + r")[\])]?[\s:/,\-]*",
    re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-z]+")
_STOPWORDS = frozenset(
    "the a an of to for on in at is are be this that your you our we will with and or "
    "please dear all students student hi hello good morning afternoon evening".split())


def _scrub_text(text: str) -> str:
    """Strip mail chrome, sign-offs and whitespace noise from an announcement body
    without dropping any of the substance (dates, venues, links stay)."""
    text = _ZERO_WIDTH.sub("", str(text or "")).replace("\xa0", " ")
    kept = []
    for line in text.split("\n"):
        line = line.rstrip()
        if _BOILERPLATE_LINE.match(line):
            continue
        kept.append(line)
    text = "\n".join(kept)
    match = _SIGNOFF_TAIL.search(text)
    if match:
        trimmed = text[:match.start()].rstrip()
        removed = len(text) - match.start()
        # Accept the trim when it leaves real content behind, or when the tail it
        # removes is itself just a short "Regards, <name>" - never when a terse
        # announcement is *entirely* a sign-off ("Thanks, see you Monday").
        if len(trimmed.strip()) >= 40 or (trimmed.strip() and removed <= 60):
            text = trimmed
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _scrub_links(links: List[Dict]) -> List[Dict]:
    """De-duplicate by URL, drop dead schemes and self-referential LMS links, and
    prefer a human title over a bare-URL title for the same target."""
    best: Dict[str, Dict] = {}
    for link in links:
        url = str(link.get("url") or "").strip()
        if not url or url.lower().startswith(_DEAD_LINK_SCHEMES):
            continue
        if _ANNOUNCEMENT_PAGE.search(url):
            continue
        title = str(link.get("title") or "").strip() or url
        current = best.get(url)
        if current is None or (current["title"] == url and title != url):
            best[url] = {"title": title, "url": url}
    return list(best.values())


def _norm_words(text: str) -> List[str]:
    return [w for w in _NON_WORD.sub(" ", str(text or "").casefold()).split() if w]


def content_key(item: Dict) -> str:
    """A signature that is identical for one announcement cross-listed into
    several course shells: normalised body (the strongest signal), normalised
    course-code-stripped title, and the release day."""
    title_words = [w for w in _norm_words(_TITLE_PREFIX.sub("", item.get("title") or ""))]
    body_words = _norm_words(item.get("body"))
    released = _released(item)
    day = released.date().isoformat() if released else ""
    basis = " ".join(body_words) or " ".join(title_words)
    return hashlib.sha1(
        ("{}|{}|{}".format(basis, " ".join(title_words), day)).encode("utf-8")
    ).hexdigest()


def _raw_hash(item: Dict) -> str:
    return hashlib.sha1(
        ("{}\x1f{}".format(item.get("title") or "", item.get("body") or "")).encode("utf-8")
    ).hexdigest()


def _similarity(a: Dict, b: Dict) -> float:
    """Jaccard overlap of the significant words in two announcements (title+body).
    Used only to decide which items are worth an AI same-or-not judgement."""
    def bag(item):
        return {w for w in _norm_words("{} {}".format(item.get("title", ""), item.get("body", "")))
                if w not in _STOPWORDS and len(w) > 2}
    x, y = bag(a), bag(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _merge_group(group: List[Dict], read: Set[str]) -> Dict:
    """Fold a set of duplicate shell copies into one primary item. Stable choice
    of primary (earliest release, then lowest id) so the surviving id does not
    change between syncs. Mutates ``read`` to follow the survivor."""
    group = sorted(group, key=lambda x: (str(x.get("created") or ""), str(x.get("id") or "")))
    primary = dict(group[0])
    primary["links"] = list(primary.get("links") or [])  # own copy - do not mutate the source
    ids = {str(x.get("id") or "") for x in group}

    seen_courses = {primary.get("course")}
    extra_courses = list(primary.get("cross_posted") or [])
    known_urls = {link["url"] for link in primary["links"]}
    for dup in group[1:]:
        name = dup.get("course")
        if name and name not in seen_courses:
            seen_courses.add(name)
            extra_courses.append(name)
        for link in dup.get("links", []):
            if link["url"] not in known_urls:
                known_urls.add(link["url"])
                primary["links"].append(link)
    if extra_courses:
        primary["cross_posted"] = sorted(set(extra_courses))
    primary["duplicate_ids"] = sorted(ids - {str(primary.get("id") or "")})

    if ids & read:
        read.difference_update(ids)
        read.add(str(primary.get("id") or ""))
    return primary


def dedupe(items: List[Dict], read: Optional[Set[str]] = None) -> Tuple[List[Dict], int]:
    """Collapse exact cross-posts (same ``content_key``). Order-preserving on the
    surviving items; returns ``(items, collapsed_count)``."""
    read = read if read is not None else set()
    groups: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for item in items:
        key = content_key(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    out, collapsed = [], 0
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(group[0])
        else:
            collapsed += len(group) - 1
            out.append(_merge_group(group, read))
    return out, collapsed


# Only items this close, but not already identical, are worth an AI judgement.
AI_DUPE_SIMILARITY = 0.72


def _ambiguous_clusters(items: List[Dict]) -> List[List[Dict]]:
    """Transitively group items that are textually close (>= AI_DUPE_SIMILARITY)
    yet have different content keys - the deterministic pass has already handled
    everything else. Each returned cluster has 2+ members."""
    keyed = [(content_key(item), item) for item in items]
    parent = list(range(len(keyed)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a in range(len(keyed)):
        for b in range(a + 1, len(keyed)):
            if keyed[a][0] == keyed[b][0]:
                continue
            if _similarity(keyed[a][1], keyed[b][1]) >= AI_DUPE_SIMILARITY:
                parent[find(a)] = find(b)
    buckets: Dict[int, List[Dict]] = {}
    for i, (_key, item) in enumerate(keyed):
        buckets.setdefault(find(i), []).append(item)
    return [members for members in buckets.values() if len(members) > 1]


def _cluster_fingerprint(cluster: List[Dict]) -> str:
    """Stable id for a cluster: its members' ids plus their raw-content hashes, so
    an edited announcement invalidates the cached verdict but a re-sync does not."""
    parts = sorted("{}:{}".format(item.get("id"), _raw_hash(item)) for item in cluster)
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _parse_id_groups(text, known_ids: Set[str]) -> Optional[List[List[str]]]:
    """Decode the AI's ``[["id","id"], ...]`` reply, keeping only supplied ids."""
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start = text.find("[")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    groups = []
    for row in parsed:
        if not isinstance(row, list):
            continue
        members = sorted({str(x) for x in row if str(x) in known_ids})
        if len(members) > 1:
            groups.append(members)
    return groups


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
    return (_timestamp(str(item.get("created") or ""))
            or _timestamp(str(item.get("starts") or ""))
            or _timestamp(str(item.get("modified") or "")))


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
    text = _scrub_text(soup.get_text("\n", strip=True))
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
        "course": course["name"], "title": " ".join(str(
            item.get("title") or "Announcement").split()),
        "body": text, "links": _scrub_links(links), "created": item.get("created") or "",
        "modified": item.get("modified") or "", "starts": duration.get("start"),
        "ends": duration.get("end"),
    }


class Feed:
    def __init__(self, root: str):
        self.root = root
        self.path = _path(root)
        self._lock = threading.RLock()
        self.items: List[Dict] = []
        # Personal announcements the student adds themselves. Kept apart from the
        # synced LMS feed so a Blackboard sync never merges, retention-prunes, or
        # uploads them - they are local reminders, not course posts.
        self.local: List[Dict] = []
        self.read: Set[str] = set()
        self.synced_at = ""
        self.summary: Optional[Dict] = None
        # Cached "is this the same announcement" verdicts from the AI pass, keyed
        # by the fingerprint of the ambiguous cluster it judged, so a steady feed
        # never re-pays for the call. See ``resolve_duplicates_with_ai``.
        self.dupe_cache: Dict[str, List[List[str]]] = {}
        self.clean_version = 0
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items") or []
            self.local = data.get("local") or []
            self.read = set(data.get("read") or [])
            self.synced_at = data.get("synced_at") or ""
            self.summary = data.get("summary")
            self.dupe_cache = data.get("dupe_cache") or {}
            self.clean_version = data.get("clean_version") or 0
        except (OSError, ValueError, TypeError):
            self.items, self.local, self.read = [], [], set()
            self.synced_at, self.summary = "", None
            self.dupe_cache, self.clean_version = {}, 0
        # Self-heal a persisted feed offline: collapse any cross-posts left by an
        # older build so the ticker is clean before the first Blackboard sync.
        # Kept in memory only; the next save persists it.
        deduped, collapsed = dedupe(self.items, self.read)
        if collapsed or self.clean_version != CLEAN_VERSION:
            self.items = deduped

    def save(self):
        with self._lock:
            self.clean_version = CLEAN_VERSION
            atomic_json_dump(
                self.path,
                {"items": self.items, "local": self.local, "read": sorted(self.read),
                 "synced_at": self.synced_at, "summary": self.summary,
                 "dupe_cache": self.dupe_cache, "clean_version": CLEAN_VERSION},
                indent=2,
            )

    def sync(self, token: str, courses: List[Dict]) -> List[str]:
        fetched: List[Dict] = []
        errors: List[str] = []
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
        # Collapse cross-posts (the same announcement in several course shells)
        # every sync - Blackboard re-serves every shell copy each time, so this
        # cannot be a one-off migration.
        items, _collapsed = dedupe(list(merged.values()), self.read)
        items = self._apply_dupe_cache(items)
        items = _recent(items)
        items.sort(key=lambda x: x.get("modified") or x.get("created") or "", reverse=True)
        self.items = items
        self.synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Personal announcements are not in ``items``; keep their read flags too.
        live = {item["id"] for item in items} | {item["id"] for item in self.local}
        self.read.intersection_update(live)
        if self.summary and (self.summary.get("feed_ids") != sorted(live)
                             or self.summary.get("version") != SUMMARY_VERSION):
            self.summary = None
        self.save()
        return errors

    def _apply_dupe_cache(self, items: List[Dict]) -> List[Dict]:
        """Re-apply AI same-announcement verdicts that ``resolve_duplicates_with_ai``
        already paid for, so a steady feed stays collapsed without another call."""
        if not self.dupe_cache:
            return items
        wanted = {frozenset(group) for groups in self.dupe_cache.values()
                  for group in groups if len(group) > 1}
        if not wanted:
            return items
        by_id = {str(item.get("id") or ""): item for item in items}
        consumed: Set[str] = set()
        out: List[Dict] = []
        for item in items:
            item_id = str(item.get("id") or "")
            if item_id in consumed:
                continue
            group_ids = next((g for g in wanted if item_id in g
                              and len(g & by_id.keys()) > 1), None)
            if group_ids:
                members = [by_id[i] for i in group_ids if i in by_id]
                consumed.update(i for i in group_ids if i in by_id)
                out.append(_merge_group(members, self.read))
            else:
                out.append(item)
        return out

    def purge_duplicates(self) -> int:
        """On-demand: collapse cross-posts in the persisted feed now, without a
        Blackboard token. Returns how many shell copies were folded away."""
        with self._lock:
            deduped, collapsed = dedupe(self.items, self.read)
            deduped = self._apply_dupe_cache(deduped)
            if collapsed or len(deduped) != len(self.items):
                self.items = deduped
                live = ({item["id"] for item in self.items}
                        | {item["id"] for item in self.local})
                self.read.intersection_update(live)
                self.summary = None
                self.save()
            return collapsed

    def resolve_duplicates_with_ai(self, preferred=None) -> int:
        """Fold in near-duplicates the deterministic pass cannot see - the same
        announcement re-posted with light edits or into a differently-named shell.

        Cheap by construction: it only calls the model when two or more items are
        textually close but not identical, and it caches the verdict by the
        cluster's fingerprint so a stable feed never pays twice. Best-effort - any
        failure leaves the deterministic feed untouched.
        """
        with self._lock:
            items = list(self.items)
        clusters = _ambiguous_clusters(items)
        if not clusters:
            return 0
        pending = [(fp, cluster) for fp, cluster in
                   ((_cluster_fingerprint(c), c) for c in clusters)
                   if fp not in self.dupe_cache]
        if pending and not ai_provider.status(preferred).get("ready"):
            pending = []  # nothing new we can judge; fall through to re-apply cache
        cache_changed = False
        if pending:
            blocks = []
            for _fp, cluster in pending:
                for item in cluster:
                    blocks.append("ID: {}\nCOURSE: {}\nTITLE: {}\nBODY:\n{}".format(
                        item.get("id"), item.get("course"), item.get("title"),
                        (item.get("body") or "")[:1200]))
            system = (
                "You de-duplicate a university student's announcement feed. Group "
                "announcements that are, for this student, the SAME notice: the "
                "identical message cross-listed into another course shell, or "
                "re-posted with only cosmetic edits. ALSO group near-identical "
                "notices addressed to different tutorial/lab/seminar groups of the "
                "same course (e.g. 'no SCSF tutorial' and 'no SCSE tutorial' for the "
                "same reason) - the student is in at most one group, so the rest are "
                "noise. Do NOT group announcements that merely share a topic, a "
                "course, or a date, or that carry different instructions/dates/venues. "
                "Return a JSON array of arrays of ids, no fences, e.g. "
                "[[\"_1_1\",\"_2_1\"]]. Return [] if every announcement is distinct. "
                "Never include an id that was not supplied.")
            known_ids = {str(item.get("id") or "")
                         for _fp, cluster in pending for item in cluster}
            try:
                result = ai_provider.complete_tier(
                    "bulk", "\n\n--- ANNOUNCEMENT ---\n".join(blocks), system=system,
                    preferred=preferred, max_tokens=500, download_root=self.root)
                verdict = _parse_id_groups(result.get("text"), known_ids)
            except Exception:
                verdict = None
            if verdict is not None:
                # File the verdict per cluster so an unrelated feed change does not
                # invalidate a judgement that is still valid.
                for fingerprint, cluster in pending:
                    cluster_ids = {str(i.get("id") or "") for i in cluster}
                    self.dupe_cache[fingerprint] = [
                        sorted(g) for g in verdict
                        if len(set(g) & cluster_ids) > 1]
                cache_changed = True
        with self._lock:
            pruned = self._prune_dupe_cache()
            before = len(self.items)
            self.items = self._apply_dupe_cache(self.items)
            collapsed = before - len(self.items)
            if collapsed:
                live = ({item["id"] for item in self.items}
                        | {item["id"] for item in self.local})
                self.read.intersection_update(live)
                self.summary = None
            if collapsed or cache_changed or pruned:
                self.save()
        return collapsed

    def _prune_dupe_cache(self) -> bool:
        """Drop cached verdicts whose announcements have all aged out. Returns
        whether anything was removed."""
        live = {str(item.get("id") or "") for item in self.items}
        live |= {i for item in self.items for i in (item.get("duplicate_ids") or [])}
        kept = {key: groups for key, groups in self.dupe_cache.items()
                if any(set(group) & live for group in groups)}
        removed = len(kept) != len(self.dupe_cache)
        self.dupe_cache = kept
        return removed

    def add_local(self, text: str, priority: str = "info") -> Dict:
        """Record a student's own announcement (a reminder), newest first."""
        with self._lock:
            text = " ".join(str(text or "").split())[:240]
            if not text:
                raise ValueError("announcement text is required")
            if priority not in ("info", "warning", "urgent"):
                priority = "info"
            item = {
                "id": "local-" + uuid.uuid4().hex, "course": "Personal",
                "course_id": "", "title": text, "body": "", "links": [],
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "modified": "", "starts": None, "ends": None, "priority": priority,
            }
            self.local.insert(0, item)
            self.save()
            return item

    def remove_local(self, item_id: str) -> bool:
        """Drop a personal announcement. Never touches the synced LMS feed."""
        with self._lock:
            kept = [item for item in self.local if item["id"] != item_id]
            if len(kept) == len(self.local):
                return False
            self.local = kept
            self.read.discard(item_id)
            self.save()
            return True

    def mark_read(self, item_id: str, value=True) -> bool:
        with self._lock:
            known = ({item["id"] for item in self.items}
                     | {item["id"] for item in self.local})
            if item_id not in known:
                return False
            if value:
                self.read.add(item_id)
            else:
                self.read.discard(item_id)
            self.save()
            return True

    def snapshot(self) -> Dict:
        with self._lock:
            local = [dict(item, read=item["id"] in self.read, local=True)
                     for item in self.local]
            synced = [dict(item, read=item["id"] in self.read, local=False)
                      for item in self.items]
            items = local + synced
            return {"items": items, "total": len(items),
                    "unread": sum(not item["read"] for item in items),
                    # Synced only - "Personal" is not a real course filter.
                    "courses": sorted({item["course"] for item in synced}),
                    "synced_at": self.synced_at, "summary": self.summary}

    def summarize(self, preferred=None) -> Dict:
        # ``self.items`` only: personal announcements are the student's own
        # reminders and are deliberately kept out of the AI TL;DR.
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
        summary = dict(result, version=SUMMARY_VERSION,
                       source_ids=sorted(item["id"] for item in source),
                       feed_ids=sorted(item["id"] for item in self.items),
                       generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.summary = summary
        self.save()
        return summary

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
