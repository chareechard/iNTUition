"""Small, auditable NTU faculty catalogue used by Research suggestions.

The data is intentionally local and provenance-tagged from official NTU faculty
pages. Matching is
keyword-based so an AI response can never invent a supervisor or a research area.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "faculty.json")
_STOPWORDS = {
    "a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with",
    "using", "use", "based", "study", "towards", "developing", "designing", "analysis", "analyses", "analytical", "computation", "computational", "computer", "computing", "design", "development", "engineering", "framework", "information", "intelligence", "method", "methods", "modelling", "modeling", "science", "scientific", "systems", "system", "testing", "theory", "theoretical",
    "model", "models", "system", "systems", "data", "research", "project",
}


def _load() -> Dict:
    try:
        with open(_DATA_PATH, encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {"version": 0, "updated": None, "sources": {}, "faculty": []}
    return data if isinstance(data, dict) else {"faculty": []}


_CATALOGUE: Dict[str, Any] = {}
_CATALOGUE_SIGNATURE = None


def _catalogue() -> Dict:
    """Return the current catalogue, reloading it when the JSON changes.

    The dashboard is a long-lived process and the catalogue may be added or
    updated while it is already running. Avoid freezing an empty catalogue at
    import time, which would make every topic look unmatched until restart.
    """
    global _CATALOGUE, _CATALOGUE_SIGNATURE
    try:
        stat = os.stat(_DATA_PATH)
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if signature != _CATALOGUE_SIGNATURE:
        _CATALOGUE = _load()
        _CATALOGUE_SIGNATURE = signature
    return _CATALOGUE


def metadata() -> Dict:
    catalogue = _catalogue()
    return {
        "version": catalogue.get("version", 0),
        "updated": catalogue.get("updated"),
        "count": len(catalogue.get("faculty") or []),
        "rawRecordCount": catalogue.get("raw_record_count"),
        "sourceCounts": catalogue.get("source_counts") or {},
        "scope": catalogue.get("scope", ""),
    }


def _normalise(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _tokens(text: object) -> set:
    return {word for word in _normalise(text).split()
            if len(word) > 2 and word not in _STOPWORDS}


def _schools(record: Dict) -> List[str]:
    values = record.get("schools") or [record.get("school", "")]
    return list(dict.fromkeys(str(value).strip().upper()
                              for value in values if str(value).strip()))


def _source_url(record: Dict) -> str:
    catalogue = _catalogue()
    if record.get("profile_url"):
        return record["profile_url"]
    sources = catalogue.get("sources") or {}
    schools = _schools(record)
    if "SPMS" in schools and "CCDS" not in schools:
        return sources.get("spms", "")
    return sources.get("ccds", "")

def _score(query: str, record: Dict):
    query_norm = _normalise(query)
    query_tokens = _tokens(query)
    score = 0.0
    reasons = []
    for tag in record.get("tags") or []:
        tag_norm = _normalise(tag)
        tag_tokens = _tokens(tag)
        if not tag_norm or not tag_tokens:
            continue
        phrase_hit = re.search(
            r"(?<![a-z0-9])" + re.escape(tag_norm) + r"(?![a-z0-9])",
            query_norm)
        if phrase_hit:
            score += 7.0 + min(len(tag_tokens), 4)
            reasons.append(tag)
            continue
        overlap = query_tokens.intersection(tag_tokens)
        if overlap:
            # A lone generic word from a multi-word official interest is not
            # enough to call someone a fit. Exact phrases remain strong.
            score += min(2.5, len(overlap) * 1.25)
            reasons.extend(sorted(overlap))
    # Direct wording in an official interest field is a stronger signal than
    # partial overlap with the catalogue's keyword taxonomy.
    for interest in record.get("research_interests") or []:
        interest_norm = _normalise(interest)
        if interest_norm and len(_tokens(interest)) > 1 and interest_norm in query_norm:
            score += 4.0
            reasons.append(interest)
    deduped = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return score, deduped[:4]

def _public(record: Dict, score: Optional[float] = None,
            reasons: Optional[List[str]] = None, weak: bool = False) -> Dict:
    schools = _schools(record)
    result = {
        "id": record.get("id", ""),
        "name": record.get("name", ""),
        "title": record.get("title", ""),
        "school": record.get("school") or " / ".join(schools),
        "schools": schools,
        "unit": record.get("unit", ""),
        "email": record.get("email", ""),
        "research_interests": record.get("research_interests") or [],
        "profile_summary": record.get("profile_summary", ""),
        "appointments": record.get("appointment_text", ""),
        "source_section": record.get("source_section", ""),
        "profile_url": record.get("profile_url", ""),
        "source_url": record.get("source_url") or _source_url(record),
    }
    if score is not None:
        result["score"] = round(score, 1)
    if reasons:
        result["matched_terms"] = reasons
    if weak:
        # A nearest-neighbour lead assigned to keep the topic->faculty mapping
        # total, not a catalogue match that cleared MIN_MATCH_SCORE. The UI
        # labels it so the student treats it as a starting point, not a fit.
        result["weak"] = True
    return result

MIN_MATCH_SCORE = 6.0  # require an exact/strong official-interest signal before displaying a lead


def match_topic(title: str, topic: str, keywords: str = "",
                limit: int = 3) -> List[Dict]:
    """Return up to limit faculty whose catalogue tags fit this idea."""
    catalogue = _catalogue()
    query = " ".join(part for part in (title, topic, keywords) if part)
    ranked = []
    for record in catalogue.get("faculty") or []:
        score, reasons = _score(query, record)
        if score >= MIN_MATCH_SCORE:
            ranked.append((score, record, reasons))
    ranked.sort(key=lambda row: (-row[0], row[1].get("school", ""),
                                 row[1].get("name", "")))
    return [_public(record, score, reasons)
            for score, record, reasons in ranked[:max(1, min(limit, 5))]]


def _rank_catalogue(query: str) -> List:
    """Every faculty record scored against one query, strongest first."""
    ranked = [(score, record, reasons)
              for record in _catalogue().get("faculty") or []
              for score, reasons in [_score(query, record)]]
    ranked.sort(key=lambda row: (-row[0], row[1].get("school", ""),
                                 row[1].get("name", "")))
    return ranked


def match_suggestions(suggestions: List[Dict], keywords: str = "",
                      limit: int = 3) -> List[List[Dict]]:
    """Map each suggestion to a faculty lead list, in the suggestions' own order.

    The mapping is total: every suggestion gets at least one faculty member.
    Strong catalogue matches (``score >= MIN_MATCH_SCORE``) are listed best-first
    exactly as ``match_topic`` would; a suggestion with no strong match still
    receives its single nearest faculty member (flagged ``weak``) so nothing is
    left unmapped.

    The primary (first) lead is kept injective - distinct across suggestions -
    wherever the catalogue is large enough, so N suggestions surface N different
    supervisors instead of the same name repeated. Only when there are more
    suggestions than faculty, or a topic has no scored candidate left, does a
    primary repeat.
    """
    suggestions = list(suggestions or [])
    if not suggestions:
        return []
    if not (_catalogue().get("faculty") or []):
        return [[] for _ in suggestions]

    ranked = [_rank_catalogue(
                  " ".join(part for part in (item.get("title", ""),
                                             item.get("topic", ""), keywords)
                           if part))
              for item in suggestions]

    n = len(suggestions)
    primary: List[Optional[tuple]] = [None] * n
    used_ids = set()

    # Greedy one-to-one assignment: settle the strongest (topic, faculty) pair
    # first, then the next strongest whose topic and faculty are both still free.
    pairs = sorted(
        ((score, i, record, reasons)
         for i, rows in enumerate(ranked)
         for score, record, reasons in rows if score > 0),
        key=lambda p: -p[0])
    for score, i, record, reasons in pairs:
        if primary[i] is None and record.get("id") not in used_ids:
            primary[i] = (score, record, reasons, score < MIN_MATCH_SCORE)
            used_ids.add(record.get("id"))

    # Totality: a topic with nothing scored (or nothing left unused) still gets
    # a lead - its nearest unused record, or the overall nearest if the
    # catalogue is exhausted.
    for i in range(n):
        if primary[i] is not None:
            continue
        rows = ranked[i]
        pick = next((row for row in rows if row[1].get("id") not in used_ids),
                    rows[0] if rows else None)
        if pick is not None:
            score, record, reasons = pick
            primary[i] = (score, record, reasons, score < MIN_MATCH_SCORE)
            used_ids.add(record.get("id"))

    span = max(1, min(limit, 5))
    out: List[List[Dict]] = []
    for i in range(n):
        rows: List[Dict] = []
        seen = set()
        if primary[i] is not None:
            score, record, reasons, weak = primary[i]
            rows.append(_public(record, score, reasons, weak=weak))
            seen.add(record.get("id"))
        for score, record, reasons in ranked[i]:
            if len(rows) >= span:
                break
            if score >= MIN_MATCH_SCORE and record.get("id") not in seen:
                rows.append(_public(record, score, reasons))
                seen.add(record.get("id"))
        out.append(rows)
    return out


def get(faculty_id: str) -> Optional[Dict]:
    """Look up one faculty member by id, for the Research tab's professor picker.

    Returns the same public shape as everything else here so the UI never has
    to special-case a directly-selected professor versus a matched one.
    """
    faculty_id = (faculty_id or "").strip()
    if not faculty_id:
        return None
    for record in _catalogue().get("faculty") or []:
        if record.get("id") == faculty_id:
            return _public(record)
    return None


def directory(query: str = "", school: str = "") -> List[Dict]:
    catalogue = _catalogue()
    query_tokens = _tokens(query)
    school = (school or "").strip().upper()
    records = []
    for record in catalogue.get("faculty") or []:
        if school and school not in _schools(record):
            continue
        if query_tokens and not query_tokens.intersection(_tokens(" ".join(
                [record.get("name", ""), record.get("unit", ""),
                 " ".join(record.get("research_interests") or []),
                 " ".join(record.get("tags") or [])]))):
            continue
        records.append(_public(record))
    return records
