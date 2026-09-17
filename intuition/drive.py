"""Google Drive destination for the move pipeline.

Mirrors the local download tree into Drive, verifies each upload, then deletes the local
copy and records the result in the ledger.

Drive has no real paths - only files with parent ids - so every folder level has to be
looked up or created and then cached, otherwise a few hundred files would cost a few
hundred redundant queries.

Credentials
-----------
Google requires an OAuth client that belongs to *you*; there is no way to ship one. See
``SETUP_HELP`` (printed by the CLI) for the console steps. The consent screen opens in
your browser and Google hands the token straight back to the local flow - the tool never
sees your Google password.
"""
import base64
import contextlib
import html
import math
import os
import posixpath
import re
import socket
import tempfile
import time
import zipfile
from collections import Counter
from xml.etree import ElementTree
from typing import Callable, Dict, List, Optional, Tuple

from intuition import utils
from intuition.ledger import Ledger


def _prefer_ipv4_dns():
    """Stop Google calls from hanging on a broken IPv6 route.

    Observed on at least one deployment: DNS still returns AAAA records for
    oauth2.googleapis.com / www.googleapis.com, httplib2 and requests try
    those first, and the connect() hangs until its ~120s timeout instead of
    falling back to the (working) IPv4 address - every "Drive inventory
    failed: ... connection attempt failed" error traced back to this. Every
    Google call in this module resolves through socket.getaddrinfo, so
    filtering AAAA out once, here, fixes it everywhere. Falls back to the
    unfiltered result when a host genuinely has no A record, so this is a
    no-op on an IPv6-only network.
    """
    if getattr(socket.getaddrinfo, "_ipv4_preferred", False):
        return  # already patched - avoid re-wrapping on repeated imports
    original = socket.getaddrinfo

    def getaddrinfo_ipv4_first(host, *args, **kwargs):
        results = original(host, *args, **kwargs)
        ipv4 = [r for r in results if r[0] == socket.AF_INET]
        return ipv4 or results

    getaddrinfo_ipv4_first._ipv4_preferred = True
    socket.getaddrinfo = getaddrinfo_ipv4_first


_prefer_ipv4_dns()

# drive.file alone only ever sees files this app created or that were opened with it
# through a picker - a student's own pre-existing Docs are invisible to it no matter
# where they sit. drive.readonly adds read (list/get/export) access across the whole
# Drive so "Search my Drive" can find and pull them too; write access for the app's
# own mirrored tree still comes from drive.file.
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".intuition")
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, "google_client_secret.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "google_token.json")
PUSH_LOCK_PATH = os.path.join(CONFIG_DIR, "push.lock")
# Comfortably longer than any real push run, so a lock left behind by a crashed or
# killed process (no ordinary exit path skips the `finally` in push_lock()) is
# eventually reclaimed instead of wedging every push forever.
PUSH_LOCK_STALE_SECONDS = 2 * 60 * 60

DEFAULT_ROOT_FOLDER = "iNTUition"
# The application was renamed after existing users had already archived course
# material below Drive/NTULearn.  Keep that archive discoverable while new pushes
# use the branded root; Drive file IDs remain stable, so merged listings can be
# deduplicated safely.
LEGACY_ROOT_FOLDERS = ("NTULearn",)
SEARCH_LIMIT = 6
MAX_DRIVE_LIST_PAGES = 500
MAX_DRIVE_LIST_ITEMS = 250_000
SEARCH_CONCEPTS = {
    "lecture": {"lecture", "lectures", "slides", "slide", "video", "videos"},
    "tutorial": {"tutorial", "tutorials", "tut", "tuts"},
    "assignment": {"assignment", "assignments", "assessment", "assessments"},
    "note": {"note", "notes"},
    "lab": {"lab", "labs", "laboratory"},
    "exam": {"exam", "exams", "examination", "quiz", "quizzes", "test", "tests"},
    "solution": {"solution", "solutions", "answer", "answers"},
    "recording": {"recording", "recordings", "webcast", "webcasts"},
    "syllabus": {"syllabus", "outline", "overview"},
    "textbook": {"textbook", "textbooks", "book", "books"},
}


def _normalize_num(word: str) -> str:
    if word.isdigit():
        return str(int(word))
    return word


def _canonical_word(word: str) -> str:
    word = _normalize_num(word)
    for concept, variants in SEARCH_CONCEPTS.items():
        if word in variants:
            return concept
    if len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


def _tokenize(text: str) -> List[str]:
    raw_tokens = re.findall(r"[a-z]+\d+[a-z]*|[a-z]+|\d+", (text or "").lower())
    tokens = []
    for token in raw_tokens:
        tokens.append(token)
        m_course = re.fullmatch(r"([a-z]{2,4})(\d{3,5}[a-z]?)", token)
        if m_course:
            tokens.append(m_course.group(1))
            tokens.append(m_course.group(2))
        else:
            m_comp = re.fullmatch(r"([a-z]+)(\d+)([a-z]*)", token)
            if m_comp:
                tokens.append(m_comp.group(1))
                tokens.append(str(int(m_comp.group(2))))
                if m_comp.group(3):
                    tokens.append(m_comp.group(3))
        if token.isdigit():
            tokens.append(str(int(token)))
    return tokens


def _normalize_query(query: str) -> str:
    q = re.sub(r"\b([a-z]{2,4})\s+(\d{3,5}[a-z]?)\b", r"\1\2", (query or ""), flags=re.I)
    q = re.sub(r"\b(tut|tutorial|lec|lecture|wk|week|lab|ch|chapter|assignment|hw)\s*(\d+)\b", r"\1 \2", q, flags=re.I)
    return q


def _one_edit_apart(left: str, right: str) -> bool:
    """Cheap typo tolerance for one insertion, deletion, substitution or swap."""
    if left == right or min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        return (len(differences) == 1 or
                len(differences) == 2 and differences[1] == differences[0] + 1 and
                left[differences[0]] == right[differences[1]] and
                left[differences[1]] == right[differences[0]])
    short, long = (left, right) if len(left) < len(right) else (right, left)
    i = j = edits = 0
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1; j += 1
        else:
            edits += 1; j += 1
            if edits > 1:
                return False
    return True


def _search_terms(text: str) -> List[str]:
    """Normalise paths into useful lexical concepts without an embedding service."""
    words = _tokenize(text)
    terms = list(words)
    terms.extend(words[i] + " " + words[i + 1] for i in range(len(words) - 1))
    return terms


def semantic_search(files: List[Dict], query: str, limit: int = SEARCH_LIMIT) -> List[Dict]:
    """Rank Drive paths with a compact TF-IDF index plus prefix/fuzzy concept matches.

    This intentionally indexes metadata only: it avoids downloading file bodies and
    keeps search deterministic and cheap enough for the local dashboard.
    """
    if not files:
        return []
    query = _normalize_query(query)
    raw_query_words = _tokenize(query)
    if not raw_query_words:
        return []
    query_words = [_canonical_word(word) for word in raw_query_words]
    course_terms = {term for term in query_words if re.fullmatch(r"[a-z]{2,4}\d{3,5}[a-z]?", term)}
    documents, filenames = [], []
    for item in files:
        raw_words = _tokenize(item.get("rel_path") or item.get("name") or "")
        documents.append([_canonical_word(word) for word in raw_words])
        name_words = _tokenize(item.get("name") or os.path.basename(item.get("rel_path") or ""))
        filenames.append([_canonical_word(word) for word in name_words])
    frequencies = Counter(term for words in documents for term in set(words))
    total = len(documents)
    ranked = []
    for item, words, name_words in zip(files, documents, filenames):
        counts = Counter(words)
        score = 0.0
        matched_words = set()
        for wanted in query_words:
            for term, frequency in counts.items():
                # Prefixes are accepted only as deliberate short abbreviations such
                # as "lec". Never reverse-match a longer query to a short path token.
                similarity = (1.0 if term == wanted else
                              0.68 if 3 <= len(wanted) <= 4 and term.startswith(wanted) else
                              0.56 if _one_edit_apart(wanted, term) else 0.0)
                if similarity:
                    field_boost = 2.4 if term in name_words else 1.0
                    score += field_boost * similarity * (1.0 + math.log(frequency)) * (
                        math.log((total + 1) / (frequencies[term] + 1)) + 1.0)
                    matched_words.add(wanted)
        # Course codes are identifiers, not fuzzy concepts: never leak results from a
        # different module. Other multi-word searches use AND semantics to keep the
        # result set compact and predictable.
        if course_terms and not course_terms.issubset(set(words)):
            continue
        if not set(query_words).issubset(matched_words):
            continue
        # Numbers describe a local structure (week 3, tutorial 2), so check proximity
        # in the token sequence instead of strict adjacent pair matching.
        if any(word.isdigit() for word in query_words):
            pair_ok = True
            for i in range(len(query_words) - 1):
                left, right = query_words[i], query_words[i + 1]
                if right.isdigit():
                    found = False
                    for idx_r, w_r in enumerate(words):
                        if w_r == right:
                            for idx_l in range(max(0, idx_r - 4), idx_r + 1):
                                if words[idx_l] == left:
                                    found = True
                                    break
                        if found:
                            break
                    if not found:
                        pair_ok = False
                        break
            if not pair_ok:
                continue
        phrase = " ".join(query_words)
        if phrase and phrase in " ".join(words):
            score *= 1.35
        mime = item.get("mime_type") or ""
        extension = os.path.splitext(item.get("name") or item.get("rel_path") or "")[1].lower()
        leaf_context = "/".join((item.get("rel_path") or "").split("/")[-2:]).lower()
        if "lecture" in query_words and re.search(r"\b(tutorial|tut\d*)\b", leaf_context):
            score *= 0.15
        if "tutorial" in query_words and "lecture" in leaf_context:
            score *= 0.15
        if mime.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".gif"}:
            score *= 0.2
        elif extension in {".java", ".py", ".txt"}:
            score *= 0.72
        if extension == ".pdf":
            score *= 1.08
        if score:
            ranked.append((score, item.get("modified") or "", item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    results, seen = [], set()
    for score, _, item in ranked:
        # PDF/PPTX twins and repeated exports otherwise consume most of a small result
        # set. Keep only the strongest representation of the same logical path.
        identity = os.path.splitext((item.get("rel_path") or "").lower())[0]
        if identity in seen:
            continue
        seen.add(identity)
        results.append(dict(item, search_score=round(score, 3)))
        if len(results) >= max(1, min(limit, SEARCH_LIMIT)):
            break
    return results


def _drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def native_search(service, files: List[Dict], query: str,
                  limit: int = SEARCH_LIMIT) -> List[Dict]:
    """Use Drive's content index, then scope/rerank matches through our iNTUition tree."""
    query = _normalize_query(query)
    words = _tokenize(query)
    if not words or not files:
        return []
    course_terms = {word for word in words if re.fullmatch(r"[a-z]{2,4}\d{3,5}[a-z]?", word)}
    content_words = [word for word in words if word not in course_terms]
    inventory = {item["id"]: item for item in files}
    if course_terms:
        inventory = {item_id: item for item_id, item in inventory.items()
                     if course_terms.issubset(set(_tokenize(item.get("rel_path") or item.get("name") or "")))}
    if not inventory:
        return semantic_search(files, query, limit=limit)
    structured_indexes = set()
    for index, word in enumerate(words[:-1]):
        if word in {"week", "wk", "tutorial", "tut", "lecture", "lec"} and words[index + 1].isdigit():
            number = words[index + 1]
            pattern = re.compile(r"(?:^|[^a-z0-9])(?:{})(?:\s*|[-_])0*{}(?:[^0-9]|$)".format(
                "week|wk" if word in {"week", "wk"} else
                "tutorial|tut" if word in {"tutorial", "tut"} else "lecture|lec",
                re.escape(number)))
            inventory = {item_id: item for item_id, item in inventory.items()
                         if pattern.search((item.get("rel_path") or "").lower())}
            structured_indexes.update({index, index + 1})
    if not inventory:
        return semantic_search(files, query, limit=limit)
    content_words = [word for index, word in enumerate(words)
                     if word not in course_terms and index not in structured_indexes]
    # A course-only query is metadata intent, so the local tree is authoritative.
    if not content_words:
        return semantic_search(list(inventory.values()), query, limit=limit)

    clauses = []
    for word in content_words:
        value = _drive_query_value(word)
        clauses.append("(name contains '{}' or fullText contains '{}')".format(value, value))
    # Also offer Drive its exact-phrase form. Its managed content index can match text
    # inside supported documents that is absent from our local path metadata.
    phrase = _drive_query_value(" ".join(content_words))
    token_query = " and ".join(clauses)
    search_query = "({})".format(token_query)
    if len(content_words) > 1:
        search_query = "({} or fullText contains '\"{}\"')".format(token_query, phrase)
    try:
        response = service.files().list(
            q="trashed = false and " + search_query,
            fields="files(id,name,mimeType,size,modifiedTime)", pageSize=100,
            orderBy="modifiedTime desc", spaces="drive",
        ).execute()
        native = [inventory[item["id"]] for item in response.get("files", [])
                  if item.get("id") in inventory]
    except Exception:
        native = []
    # Metadata matches are highly interpretable, so place them first.
    metadata = semantic_search(list(inventory.values()), query, limit=limit)
    seen = {item["id"] for item in metadata}
    output = list(metadata)
    for item in native:
        if item["id"] not in seen:
            output.append(dict(item, search_source="Google Drive content index"))
            seen.add(item["id"])
        if len(output) >= limit:
            break
    return output


def search_my_drive(service, query: str, limit: int = 25) -> List[Dict]:
    """Search the whole Drive, not just the app's mirrored root - the only way to
    find files a student created directly in Drive rather than through this app
    (see the drive.readonly half of SCOPES).

    Files found this way have no ancestry in the app's own folder tree, so there is
    no real rel_path to derive the way list_files() does. A synthetic one is built
    instead - "My Drive/<parent folder>/<name>" - which is enough for the existing
    tree browser (tree_level splits purely on rel_path) and for matches_course()
    elsewhere, as long as the real Drive folder name carries the course code, which
    course folders typically do.
    """
    q = "trashed = false and (name contains '{v}' or fullText contains '{v}')".format(
        v=_drive_query_value(query))
    response = service.files().list(
        q=q, fields="files(id,name,mimeType,size,modifiedTime,parents)",
        pageSize=max(1, limit), orderBy="modifiedTime desc", spaces="drive",
    ).execute()

    parent_names: Dict[str, str] = {}

    def _parent_name(parent_id: str) -> Optional[str]:
        if parent_id not in parent_names:
            try:
                meta = service.files().get(fileId=parent_id, fields="name").execute()
                parent_names[parent_id] = meta.get("name") or ""
            except Exception:
                parent_names[parent_id] = ""
        return parent_names[parent_id] or None

    found: List[Dict] = []
    for item in response.get("files", [])[:limit]:
        parents = item.get("parents") or []
        parent_name = _parent_name(parents[0]) if parents else None
        rel_path = "My Drive/{}/{}".format(parent_name, item["name"]) if parent_name \
            else "My Drive/{}".format(item["name"])
        found.append({
            "id": item["id"], "name": item["name"], "rel_path": rel_path,
            "mime_type": item.get("mimeType") or "application/octet-stream",
            "size": int(item.get("size") or 0),
            "modified": item.get("modifiedTime"),
            "external": True,
        })
    return found


def tree_level(files: List[Dict], prefix: str = "") -> Dict:
    """Group the flat ``list_files()`` inventory into one level of a folder tree.

    ``rel_path`` is the only structure Drive metadata carries here (folders
    themselves are discarded in ``list_files`` - only their names survive as
    path segments), so a course-code tree browser is built by splitting on
    "/" one level at a time rather than indexing folders separately. This
    keeps every level a single pass over the already-cached inventory, with
    no extra Drive requests or state to keep in sync.
    """
    prefix = (prefix or "").strip("/")
    depth = len(prefix.split("/")) if prefix else 0
    folders: Dict[str, Dict] = {}
    entries = []
    for item in files:
        rel = item.get("rel_path") or ""
        if not rel:
            continue
        if prefix:
            if not rel.startswith(prefix + "/"):
                continue
            parts = rel.split("/")[depth:]
        else:
            parts = rel.split("/")
        if len(parts) > 1:
            name = parts[0]
            bucket = folders.setdefault(
                name, {"name": name, "count": 0, "size": 0, "modified": ""})
            bucket["count"] += 1
            bucket["size"] += int(item.get("size") or 0)
            modified = item.get("modified") or ""
            if modified > bucket["modified"]:
                bucket["modified"] = modified
        elif parts and parts[0]:
            entries.append(item)
    return {
        "path": prefix,
        "folders": sorted(folders.values(), key=lambda f: f["name"].lower()),
        "files": sorted(entries, key=lambda f: (f.get("name") or "").lower()),
    }


_SLIDE_NUM_RE = re.compile(r"slide(\d+)\.xml$")


def extract_learning_pages(path: str, limit: int = 40000) -> List[Tuple[int, str]]:
    """``[(page_number, text)]`` for paginated formats; ``[(1, text)]`` for flat ones.

    Page numbers are what a citation can point back to. PDFs paginate naturally.
    PPTX slides are separate XML parts under ``ppt/slides/`` and sort into slide
    order (numerically, not lexicographically - slide10 sorts after slide9, not
    before slide2). DOCX and plain text carry no reliable page boundary in the
    source, so they report everything as page 1 - honest under-precision rather
    than a fabricated split.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension == ".pdf":
        try:
            from pypdf import PdfReader
            pages = [(i + 1, page.extract_text() or "")
                     for i, page in enumerate(PdfReader(path).pages)]
        except Exception as exc:
            raise DriveError("PDF text extraction is unavailable: {}".format(exc))
    elif extension == ".pptx":
        try:
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist()
                         if name.startswith("ppt/slides/") and name.endswith(".xml")
                         and _SLIDE_NUM_RE.search(name)]
                def slide_number(name: str) -> int:
                    match = _SLIDE_NUM_RE.search(name)
                    if match is None:
                        return 0
                    return int(match.group(1))

                names.sort(key=slide_number)
                pages = []
                for name in names:
                    match = _SLIDE_NUM_RE.search(name)
                    if match is None:
                        continue
                    root = ElementTree.fromstring(archive.read(name))
                    text = " ".join(node.text for node in root.iter()
                                    if node.text and node.text.strip())
                    pages.append((int(match.group(1)), text))
        except Exception as exc:
            raise DriveError("Office document text extraction failed: {}".format(exc))
    elif extension == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                names = sorted(name for name in archive.namelist()
                               if name.startswith("word/") and name.endswith(".xml"))
                chunks = []
                for name in names:
                    root = ElementTree.fromstring(archive.read(name))
                    chunks.append(" ".join(node.text for node in root.iter()
                                           if node.text and node.text.strip()))
                pages = [(1, "\n".join(chunks))]
        except Exception as exc:
            raise DriveError("Office document text extraction failed: {}".format(exc))
    elif extension in {".txt", ".md", ".csv", ".vtt", ".srt", ".java", ".py"}:
        with open(path, encoding="utf-8", errors="replace") as stream:
            pages = [(1, stream.read(limit + 1))]
    else:
        raise DriveError("FRIDAY supports PDF, DOCX, PPTX and text materials")

    cleaned = [(num, re.sub(r"[ \t]+", " ", text).strip()) for num, text in pages]
    if not any(text for _num, text in cleaned):
        raise DriveError("No readable text was found in this material")

    # Bound the total by dropping whole trailing pages once the budget runs out,
    # rather than truncating every page a little - a citation to an included page
    # is then never pointing at text that got cut off mid-sentence.
    out: List[Tuple[int, str]] = []
    used = 0
    for num, text in cleaned:
        if not text:
            continue
        remaining = limit - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        out.append((num, text))
        used += len(text)
    return out


def extract_learning_text(path: str, limit: int = 40000) -> str:
    """Extract bounded study text from common course-material formats."""
    return "\n".join(text for _page, text in extract_learning_pages(path, limit))


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCREL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_DOCX_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "tif": "image/tiff",
    "tiff": "image/tiff", "svg": "image/svg+xml",
}
# emf/wmf are Windows metafiles no browser renders inline - skipped, not embedded.
_DOCX_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_DOCX_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ color-scheme: light dark; }}
html, body {{ margin: 0; }}
body {{
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #1a1c22; background: #fff;
  padding: 40px clamp(16px, 5vw, 64px);
}}
main {{ max-width: 46rem; margin: 0 auto; }}
h1, h2, h3, h4, h5, h6 {{ line-height: 1.25; margin: 1.6em 0 .5em; }}
h1 {{ font-size: 1.8em; }} h2 {{ font-size: 1.45em; }} h3 {{ font-size: 1.2em; }}
p {{ margin: 0 0 .9em; }}
ul, ol {{ margin: 0 0 .9em; padding-left: 1.6em; }}
li {{ margin: .2em 0; }}
a {{ color: #2563eb; }}
img {{ max-width: 100%; height: auto; }}
table {{ border-collapse: collapse; margin: 0 0 1.1em; width: 100%; }}
td, th {{ border: 1px solid #cdd2dc; padding: 6px 10px; vertical-align: top; }}
@media (prefers-color-scheme: dark) {{
  body {{ color: #d5d8e0; background: #14161c; }}
  a {{ color: #6ea8fe; }}
  td, th {{ border-color: #3a3f4b; }}
}}
</style></head><body><main>{body}</main></body></html>"""


def _wtag(tag: str) -> str:
    return "{{{}}}{}".format(_WORD_NS, tag)


def _docrel(attr: str) -> str:
    return "{{{}}}{}".format(_DOCREL_NS, attr)


def _toggle_on(node) -> bool:
    """A Word toggle property (``<w:b/>``) is on unless it carries ``w:val`` off."""
    if node is None:
        return False
    return (node.get(_wtag("val")) or "true").lower() not in ("0", "false", "off")


_HEADING_RE = re.compile(r"heading\s*([1-9])")


class _DocxRenderer:
    """Walk ``word/document.xml`` into semantic HTML for inline preview.

    Word's body is a flat stream of paragraphs and tables; the browser can't
    show a .docx natively and the material drawer loads /api/drive/content
    straight into an <iframe>, so this turns the stream into headings,
    bold/italic runs, lists, tables and inline images - enough for the tutorial
    sheets and notes that arrive as .docx. Deliberately dependency-free, in the
    same house style as ``extract_learning_pages``.
    """

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.rels = self._load_rels()
        self.numbering = self._load_numbering()
        self._list_open: Optional[str] = None
        self.has_content = False

    def _load_rels(self) -> Dict[str, str]:
        try:
            root = ElementTree.fromstring(
                self.archive.read("word/_rels/document.xml.rels"))
        except (KeyError, ElementTree.ParseError):
            return {}
        rels: Dict[str, str] = {}
        for node in root:
            rid = node.get("Id")
            if rid:
                rels[rid] = node.get("Target", "")
        return rels

    def _load_numbering(self) -> Dict[str, str]:
        """``numId`` -> ``"ol"`` / ``"ul"`` for the top list level."""
        try:
            root = ElementTree.fromstring(self.archive.read("word/numbering.xml"))
        except (KeyError, ElementTree.ParseError):
            return {}
        abstract: Dict[str, str] = {}
        for anum in root.findall(_wtag("abstractNum")):
            abstract_id = anum.get(_wtag("abstractNumId"))
            if not abstract_id:
                continue
            fmt = None
            for lvl in anum.findall(_wtag("lvl")):
                if lvl.get(_wtag("ilvl")) == "0":
                    node = lvl.find(_wtag("numFmt"))
                    fmt = node.get(_wtag("val")) if node is not None else None
                    break
            abstract[abstract_id] = (
                "ul" if fmt in (None, "bullet", "none") else "ol")
        numbering: Dict[str, str] = {}
        for num in root.findall(_wtag("num")):
            num_id = num.get(_wtag("numId"))
            ref = num.find(_wtag("abstractNumId"))
            ref_val = ref.get(_wtag("val")) if ref is not None else None
            if num_id and ref_val:
                numbering[num_id] = abstract.get(ref_val, "ul")
        return numbering

    def render(self) -> str:
        document = ElementTree.fromstring(self.archive.read("word/document.xml"))
        body = document.find(_wtag("body"))
        if body is None:
            return ""
        parts: List[str] = []
        for child in body:
            if child.tag == _wtag("p"):
                parts.append(self._paragraph(child))
            elif child.tag == _wtag("tbl"):
                self._close_list(parts)
                parts.append(self._table(child))
        self._close_list(parts)
        return "".join(parts)

    def _close_list(self, out: List[str]) -> None:
        if self._list_open:
            out.append("</{}>".format(self._list_open))
            self._list_open = None

    def _paragraph(self, para) -> str:
        ppr = para.find(_wtag("pPr"))
        style, list_kind = "", None
        if ppr is not None:
            pstyle = ppr.find(_wtag("pStyle"))
            if pstyle is not None:
                style = (pstyle.get(_wtag("val")) or "").lower()
            numpr = ppr.find(_wtag("numPr"))
            if numpr is not None:
                num_id = numpr.find(_wtag("numId"))
                if num_id is not None:
                    list_kind = self.numbering.get(
                        num_id.get(_wtag("val")), "ul")

        inner = self._runs(para).strip()
        out: List[str] = []
        if list_kind:
            if self._list_open != list_kind:
                self._close_list(out)
                out.append("<{}>".format(list_kind))
                self._list_open = list_kind
            out.append("<li>{}</li>".format(inner or "&nbsp;"))
            self.has_content = True
            return "".join(out)

        self._close_list(out)
        if not inner:
            return "".join(out)  # blank line - spacing already comes from margins
        heading = _HEADING_RE.match(style)
        if heading:
            tag = "h{}".format(heading.group(1))
        elif style == "title":
            tag = "h1"
        else:
            tag = "p"
        out.append("<{0}>{1}</{0}>".format(tag, inner))
        self.has_content = True
        return "".join(out)

    def _runs(self, parent) -> str:
        parts: List[str] = []
        for child in parent:
            if child.tag == _wtag("r"):
                parts.append(self._run(child))
            elif child.tag == _wtag("hyperlink"):
                inner = self._runs(child)
                target = self.rels.get(child.get(_docrel("id")), "")
                if inner and target:
                    parts.append('<a href="{}" target="_blank" rel="noopener">'
                                 "{}</a>".format(html.escape(target, quote=True),
                                                 inner))
                else:
                    parts.append(inner)
            elif child.tag in (_wtag("ins"), _wtag("smartTag")):
                parts.append(self._runs(child))
        return "".join(parts)

    def _run(self, run) -> str:
        rpr = run.find(_wtag("rPr"))
        pieces: List[str] = []
        for child in run:
            if child.tag == _wtag("t"):
                pieces.append(html.escape(child.text or ""))
            elif child.tag == _wtag("tab"):
                pieces.append(" ")
            elif child.tag in (_wtag("br"), _wtag("cr")):
                pieces.append("<br>")
            elif child.tag == _wtag("drawing") or child.tag == _wtag("pict"):
                pieces.append(self._image(child))
        text = "".join(pieces)
        if not text or rpr is None:
            return text
        if _toggle_on(rpr.find(_wtag("b"))):
            text = "<strong>{}</strong>".format(text)
        if _toggle_on(rpr.find(_wtag("i"))):
            text = "<em>{}</em>".format(text)
        if rpr.find(_wtag("u")) is not None:
            text = "<u>{}</u>".format(text)
        if _toggle_on(rpr.find(_wtag("strike"))):
            text = "<s>{}</s>".format(text)
        vert = rpr.find(_wtag("vertAlign"))
        if vert is not None:
            val = vert.get(_wtag("val"))
            if val == "superscript":
                text = "<sup>{}</sup>".format(text)
            elif val == "subscript":
                text = "<sub>{}</sub>".format(text)
        return text

    def _image(self, node) -> str:
        blip = next((el for el in node.iter() if el.tag.endswith("}blip")), None)
        rel_id = None
        if blip is not None:
            rel_id = blip.get(_docrel("embed")) or blip.get(_docrel("link"))
        if rel_id is None:
            imagedata = next((el for el in node.iter()
                              if el.tag.endswith("}imagedata")), None)
            if imagedata is not None:
                rel_id = imagedata.get(_docrel("id"))
        if not rel_id:
            return ""
        target = self.rels.get(rel_id, "")
        if not target:
            return ""
        ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        mime = _DOCX_IMAGE_MIME.get(ext)
        if not mime:
            return ""
        arcname = posixpath.normpath(posixpath.join("word", target))
        try:
            data = self.archive.read(arcname)
        except KeyError:
            return ""
        if len(data) > _DOCX_MAX_IMAGE_BYTES:
            return ""
        return '<img src="data:{};base64,{}" alt="">'.format(
            mime, base64.b64encode(data).decode("ascii"))

    def _table(self, tbl) -> str:
        self.has_content = True
        saved, self._list_open = self._list_open, None
        rows: List[str] = []
        for tr in tbl.findall(_wtag("tr")):
            cells: List[str] = []
            for tc in tr.findall(_wtag("tc")):
                body: List[str] = []
                for child in tc:
                    if child.tag == _wtag("p"):
                        body.append(self._paragraph(child))
                    elif child.tag == _wtag("tbl"):
                        body.append(self._table(child))
                self._close_list(body)
                cells.append("<td>{}</td>".format("".join(body) or "&nbsp;"))
            rows.append("<tr>{}</tr>".format("".join(cells)))
        self._list_open = saved
        return "<table>{}</table>".format("".join(rows))


def docx_to_html(path: str) -> str:
    """A standalone, styled HTML page rendering ``path`` (.docx) for inline preview.

    Raises ``DriveError`` when the archive can't be parsed or holds no content.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            renderer = _DocxRenderer(archive)
            fragment = renderer.render()
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise DriveError("This .docx could not be parsed for preview: {}".format(exc))
    if not renderer.has_content:
        raise DriveError("No readable content was found in this .docx")
    return _DOCX_PAGE_TEMPLATE.format(body=fragment)


# PNG and JPEG are the two raster formats pdflatex's graphicx driver embeds directly,
# with no external convert step - the only two worth recognising here.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
FIGURE_MIN_BYTES = 8000     # below this, an "image" is almost always a bullet/icon/logo
FIGURE_MAX_COUNT = 6        # a summary is a page or two, not a slide deck of screenshots


def _sniff_image_ext(data: bytes) -> Optional[str]:
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data.startswith(_JPEG_MAGIC):
        return "jpg"
    return None


def extract_learning_figures(
    path: str, limit: int = FIGURE_MAX_COUNT, min_bytes: int = FIGURE_MIN_BYTES
) -> List[Tuple[int, bytes, str]]:
    """Up to ``limit`` embedded images from a PDF, largest first, each as
    ``(page, data, ext)`` - Compendium's raw material for real figures rather than
    the TikZ diagrams it can also draw itself.

    PDF only for now: pypdf's ``page.images`` gives page-accurate embedded images for
    free (already a dependency, see extract_learning_pages); PPTX images live in a
    shared ``ppt/media/`` with the page link only in each slide's relationship XML,
    which is real extra work saved for when a PPTX figure turns out to matter.

    Best-effort by design - a corrupt embedded image stream, or no images at all, is
    not a reason to fail a summary that only needed the text. Anything that goes
    wrong here is swallowed and reported as "no figures available" rather than
    surfaced as a DriveError, unlike extract_learning_pages which fails loudly
    because losing all the text really would make the summary worthless.
    """
    if os.path.splitext(path)[1].lower() != ".pdf":
        return []
    try:
        from pypdf import PdfReader
        candidates: List[Tuple[int, int, bytes, str]] = []  # (page, size, data, ext)
        for page_no, page in enumerate(PdfReader(path).pages, start=1):
            try:
                images = list(page.images)
            except Exception:
                continue
            for image in images:
                try:
                    data = bytes(image.data)
                except Exception:
                    continue
                if len(data) < min_bytes:
                    continue
                ext = _sniff_image_ext(data)
                if not ext:
                    continue
                candidates.append((page_no, len(data), data, ext))
        candidates.sort(key=lambda c: c[1], reverse=True)
        kept = candidates[:max(0, int(limit))]
        kept.sort(key=lambda c: c[0])  # back to page order for a readable prompt
        return [(page_no, data, ext) for page_no, _size, data, ext in kept]
    except Exception:
        return []


# A summary is a page or two of prose - a 40-page deck rasterized whole would swamp
# the prompt for marginal return past this point, and each page's own text is still
# in the corpus regardless. 1400px keeps a rendered slide legible (labels, small
# print) without ballooning the base64 payload per image.
MAX_VISION_PAGES = 40
VISION_MAX_DIMENSION_PX = 1400


def rasterize_pages(path: str, dpi: int = 110, max_pages: int = MAX_VISION_PAGES,
                    max_dimension_px: int = VISION_MAX_DIMENSION_PX
                    ) -> List[Tuple[int, bytes]]:
    """Up to ``max_pages`` full-page renders of a PDF, each as ``(page, png_bytes)``.

    Text extraction alone is blind to anything baked into a slide as a picture - a
    diagram's own labels, an image-only slide, a callout box - which is fully visible
    to a reader but invisible to ``extract_learning_pages``. This is Compendium's
    other eye: the actual rendered page, handed to the model alongside the extracted
    text rather than instead of it.

    PDF only, same reasoning as ``extract_learning_figures`` - PPTX/DOCX keep their
    existing text-only treatment rather than adding a conversion step for a format
    this hasn't been asked to handle yet.

    Best-effort by design, matching ``extract_learning_figures``: a missing PyMuPDF
    install, a corrupt PDF, or a render failure degrades to "no page images" rather
    than failing a summary that only strictly needed the text.
    """
    if os.path.splitext(path)[1].lower() != ".pdf":
        return []
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []
    try:
        out: List[Tuple[int, bytes]] = []
        with fitz.open(path) as doc:
            scale = dpi / 72.0
            matrix = fitz.Matrix(scale, scale)
            for page_no, page in enumerate(doc, start=1):
                if page_no > max_pages:
                    break
                pixmap = page.get_pixmap(matrix=matrix)
                if max(pixmap.width, pixmap.height) > max_dimension_px:
                    shrink = max_dimension_px / max(pixmap.width, pixmap.height)
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale * shrink,
                                                                scale * shrink))
                out.append((page_no, pixmap.tobytes("png")))
        return out
    except Exception:
        return []


class DriveError(Exception):
    pass


SETUP_HELP = """\
Google Drive needs an OAuth client that belongs to your own Google account:

  1. https://console.cloud.google.com/  ->  create (or pick) a project
  2. APIs & Services -> Library -> enable "Google Drive API"
  3. APIs & Services -> OAuth consent screen -> External -> add your own
     Google address under "Test users"
  4. APIs & Services -> Credentials -> Create credentials
     -> OAuth client ID -> Desktop app -> Download JSON
  5. Save that file as:
     {path}

The first push opens a browser for consent; the token is then cached at
{token} and reused.\
""".format(
    path=CLIENT_SECRET_PATH, token=TOKEN_PATH
)


def _require_libs():
    """Import the Google client lazily so the rest of the tool runs without it."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.http import MediaFileUpload  # noqa: F401
    except ImportError as e:
        raise DriveError(
            "Google Drive support needs extra packages:\n"
            "    pip install google-api-python-client google-auth-oauthlib\n"
            "({})".format(e)
        )


def ensure_config_dir() -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    return CONFIG_DIR


class PushLockError(DriveError):
    pass


@contextlib.contextmanager
def push_lock():
    """Cross-process guard around a Drive push.

    ``DriveMirror.upload`` decides create-vs-update with a ``files.list`` query,
    which is only eventually consistent - two pushes running at once (the
    dashboard's own push and a scheduled ``drive_push`` run, say) can each check the
    same not-yet-indexed path, both find nothing, and both ``create``. That is how a
    file ends up duplicated in Drive; see ``find_duplicate_files`` for the cleanup.
    An in-process flag (``State.pushing``) cannot prevent this - it says nothing
    about a *different* process - so the lock lives on disk instead, where every
    pusher (dashboard or CLI) can see it.

    A file that still exists after a crash would otherwise wedge every future push
    forever, so a lock older than ``PUSH_LOCK_STALE_SECONDS`` is treated as
    abandoned and reclaimed rather than honoured.
    """
    ensure_config_dir()
    deadline = time.monotonic() + 5  # a few seconds covers a benign handoff
    while True:
        try:
            fd = os.open(PUSH_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(PUSH_LOCK_PATH)
            except OSError:
                age = None  # removed between the failed open and this stat
            if age is not None and age > PUSH_LOCK_STALE_SECONDS:
                try:
                    os.remove(PUSH_LOCK_PATH)
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise PushLockError(
                    "Another push is already running (lock held at {}). Wait for it "
                    "to finish, or delete that file if you're sure nothing is "
                    "actually pushing.".format(PUSH_LOCK_PATH))
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.remove(PUSH_LOCK_PATH)
        except OSError:
            pass


def credentials_present() -> bool:
    return os.path.exists(CLIENT_SECRET_PATH)


def _classify_client_secret(data) -> Dict:
    """Shared verdict for a parsed OAuth client JSON.

    The two mistakes that actually happen are downloading a *service account* key or a
    *Web application* client instead of a Desktop app client. Both fail later with
    unhelpful errors, so name the problem here.
    """
    if not isinstance(data, dict):
        return {"ok": False, "problem": "Expected a JSON object."}
    if data.get("type") == "service_account":
        return {
            "ok": False,
            "problem": "This is a service-account key, not an OAuth client. A service "
                       "account has its own empty Drive, not yours. Create an OAuth "
                       "client ID of type 'Desktop app' instead.",
        }
    if "web" in data:
        return {
            "ok": False,
            "problem": "This is a 'Web application' client. The desktop consent flow "
                       "needs an OAuth client of type 'Desktop app'.",
        }
    if "installed" not in data:
        return {
            "ok": False,
            "problem": "Unrecognised client file: expected an 'installed' section "
                       "(Desktop app client).",
        }
    installed = data["installed"]
    return {"ok": True, "client_id": installed.get("client_id", ""),
            "project": installed.get("project_id")}


def inspect_client_secret() -> Dict:
    """Validate the OAuth client JSON on disk before we try to use it."""
    import json

    if not credentials_present():
        return {"ok": False, "problem": "No file at {}".format(CLIENT_SECRET_PATH)}
    try:
        with open(CLIENT_SECRET_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        return {"ok": False, "problem": "Not valid JSON ({})".format(e)}
    return _classify_client_secret(data)


def save_client_secret(raw: str) -> Dict:
    """Validate an uploaded OAuth client JSON and, only if it is a Desktop-app
    client, store it at CLIENT_SECRET_PATH. Returns the same verdict shape as
    :func:`inspect_client_secret`; the file is left untouched on any failure."""
    import json

    try:
        data = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "problem": "Not valid JSON ({})".format(e)}
    verdict = _classify_client_secret(data)
    if not verdict.get("ok"):
        return verdict

    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".client-secret-", suffix=".tmp", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CLIENT_SECRET_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    try:
        os.chmod(CLIENT_SECRET_PATH, 0o600)
    except OSError:
        pass
    return verdict


def token_present() -> bool:
    return os.path.exists(TOKEN_PATH)


def link_interactively(open_browser: bool = True, timeout_seconds: int = 300):
    """Run the Google consent flow now and cache the resulting token.

    ``get_credentials`` only reaches the browser flow when no usable token is
    cached, which is the wrong behaviour for the dashboard's explicit "Connect
    Google Drive" button (and for reconnecting after a revoke). This always runs
    a fresh consent.
    """
    _require_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not credentials_present():
        raise DriveError("Missing OAuth client secret.\n\n" + SETUP_HELP)
    verdict = inspect_client_secret()
    if not verdict.get("ok"):
        raise DriveError(verdict["problem"])

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    try:
        creds = flow.run_local_server(
            port=0, open_browser=open_browser, timeout_seconds=timeout_seconds,
            authorization_prompt_message=(
                "Opening your browser to authorise Google Drive access. "
                "If it does not open, visit:\n{url}"),
            success_message=(
                "iNTUition is connected to Google Drive. You can close this tab."))
    except TypeError:
        # Older google-auth-oauthlib without timeout_seconds.
        creds = flow.run_local_server(port=0, open_browser=open_browser)
    _save_token(creds)
    return creds


def disconnect() -> None:
    """Forget the cached Drive token (the OAuth client JSON is kept)."""
    _clear_token()


def get_credentials(interactive: bool = True):
    """Load cached credentials, refreshing or running the consent flow as needed."""
    _require_libs()
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except ValueError:
            creds = None

    if creds and not set(SCOPES).issubset(set(creds.scopes or [])):
        # A token cached under an older, narrower SCOPES list (e.g. drive.file only)
        # can't be silently upgraded by refreshing - a refresh token only ever
        # carries the scopes it was originally consented to. Drop it so the flow
        # below runs a fresh consent instead of reusing an insufficient grant.
        _clear_token()
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Google rejected the refresh token outright - most often a "Testing"
            # publish-status OAuth consent screen, whose refresh tokens expire after
            # 7 days regardless of how recently this file was rewritten, or access
            # revoked from myaccount.google.com/permissions. It will not start
            # working again on its own, so drop it: leaving a dead token in place
            # would keep token_present() reporting Drive as linked while every real
            # call kept failing behind it.
            _clear_token()
            if not interactive:
                raise DriveError(
                    "Drive access has expired or been revoked ({}). Run a push "
                    "from a terminal to sign in again - and if this keeps "
                    "happening, publish the OAuth consent screen in Google Cloud "
                    "Console out of Testing mode, which caps refresh tokens at 7 "
                    "days.".format(exc))
            # fall through to the interactive consent flow below
        else:
            _save_token(creds)
            return creds

    if not interactive:
        raise DriveError("No usable Drive token; run a push from a terminal to consent.")

    if not credentials_present():
        raise DriveError(
            "Missing OAuth client secret.\n\n" + SETUP_HELP
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".drive-token-", suffix=".tmp", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, TOKEN_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def _clear_token():
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass


def build_service(interactive: bool = True):
    _require_libs()
    from googleapiclient.discovery import build

    creds = get_credentials(interactive=interactive)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _walk_tree(service) -> Dict[str, List[Dict]]:
    """Fetch every non-trashed item once, grouped by parent id.

    One large-paged listing plus a local traversal, shared by ``list_files`` (files
    only) and ``find_duplicate_files`` (which needs the folders too) - the account-
    wide fetch is the expensive part, and both callers walk the same parent links
    from a different root or with a different filter.
    """
    items, token, seen_tokens = [], None, set()
    for _page in range(MAX_DRIVE_LIST_PAGES):
        if token in seen_tokens:
            raise DriveError("Drive returned a repeating page token")
        if token:
            seen_tokens.add(token)
        response = service.files().list(
            q="trashed = false",
            fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,parents)",
            pageSize=1000, pageToken=token, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        page_items = response.get("files", [])
        if not isinstance(page_items, list) or any(
            not isinstance(item, dict) or not item.get("id") or not item.get("name")
            for item in page_items
        ):
            raise DriveError("Drive returned malformed inventory metadata")
        items.extend(page_items)
        if len(items) > MAX_DRIVE_LIST_ITEMS:
            raise DriveError(
                "Drive inventory exceeds the {} item safety limit".format(
                    MAX_DRIVE_LIST_ITEMS))
        token = response.get("nextPageToken")
        if not token:
            break
    else:
        raise DriveError("Drive inventory exceeded the page safety limit")
    children: Dict[str, List[Dict]] = {}
    for item in items:
        for parent_id in item.get("parents") or []:
            children.setdefault(parent_id, []).append(item)
    return children


class DriveMirror:
    """Mirrors a local directory tree into Drive under a single root folder."""

    def __init__(self, service, root_folder: str = DEFAULT_ROOT_FOLDER):
        self.service = service
        self.root_folder = root_folder
        # (parent_id, name) -> folder_id. Folder lookups dominate the request count
        # without this.
        self._folders: Dict[Tuple[str, str], str] = {}
        self._root_id: Optional[str] = None

    # -- folders ----------------------------------------------------------------

    def _find_child(self, parent_id: str, name: str, folder: bool) -> Optional[str]:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = "name = '{}' and '{}' in parents and trashed = false".format(safe, parent_id)
        q += " and mimeType {} '{}'".format("=" if folder else "!=", FOLDER_MIME)
        resp = (
            self.service.files()
            .list(q=q, fields="files(id, name, size)", pageSize=10,
                  orderBy="modifiedTime desc",
                  supportsAllDrives=True, includeItemsFromAllDrives=True)
            .execute()
        )
        files = resp.get("files", [])
        # If a duplicate already exists (see find_duplicate_files), update lands on
        # the most recently modified copy rather than whichever one Drive's search
        # index happens to return first.
        return files[0]["id"] if files else None

    def _create_folder(self, parent_id: str, name: str) -> str:
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = (
            self.service.files()
            .create(body=meta, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def ensure_folder(self, parent_id: str, name: str) -> str:
        cached = self._folders.get((parent_id, name))
        if cached:
            return cached
        folder_id = self._find_child(parent_id, name, folder=True)
        if folder_id is None:
            folder_id = self._create_folder(parent_id, name)
        self._folders[(parent_id, name)] = folder_id
        return folder_id

    def root_id(self) -> str:
        if self._root_id is None:
            self._root_id = self.ensure_folder("root", self.root_folder)
        return self._root_id

    def ensure_path(self, parts: List[str]) -> str:
        """Resolve (creating as needed) a chain of folders under the root."""
        parent = self.root_id()
        for part in parts:
            if not part or part in (".", ".."):
                continue
            parent = self.ensure_folder(parent, part)
        return parent

    def list_files(self) -> List[Dict]:
        """Build one metadata tree, then return files below the configured root.

        The old walker issued a Drive request for every folder. Fetching metadata in
        large pages and traversing parent links locally makes deep course trees cheap.
        """
        root_id = self.root_id()
        children = _walk_tree(self.service)

        found: List[Dict] = []
        pending: List[Tuple[str, List[str]]] = [(root_id, [])]
        visited: set[str] = set()
        while pending:
            parent_id, parts = pending.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            for item in children.get(parent_id, []):
                item_parts = parts + [item["name"]]
                if item.get("mimeType") == FOLDER_MIME:
                    pending.append((item["id"], item_parts))
                else:
                    found.append({
                        "id": item["id"], "name": item["name"],
                        "rel_path": "/".join(item_parts),
                        "mime_type": item.get("mimeType") or "application/octet-stream",
                        "size": int(item.get("size") or 0),
                        "modified": item.get("modifiedTime"),
                    })
        return sorted(found, key=lambda item: item["rel_path"].lower())

    def upload(self, local_path: str, parent_id: str, name: Optional[str] = None,
               progress: Optional[Callable[[float], None]] = None) -> Dict:
        """Upload (or replace) one file and return the resulting Drive metadata.

        ``name`` overrides the Drive-visible filename; callers with a logical name
        that differs from the on-disk one (shortened to fit Windows' MAX_PATH) pass
        it explicitly so Drive stores the real title, not the truncated one.
        """
        _require_libs()
        from googleapiclient.http import MediaFileUpload
        name = name or os.path.basename(local_path)
        existing_id = self._find_child(parent_id, name, folder=False)
        media = MediaFileUpload(local_path, resumable=True, chunksize=8 * 1024 * 1024)
        if existing_id:
            request = self.service.files().update(
                fileId=existing_id, media_body=media, fields="id, name, size",
                supportsAllDrives=True)
        else:
            request = self.service.files().create(
                body={"name": name, "parents": [parent_id]}, media_body=media,
                fields="id, name, size", supportsAllDrives=True)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress:
                progress(status.progress())
        return response


def list_files_from_roots(service, roots: List[str]) -> Tuple[List[Dict], Dict[str, int]]:
    """List files below several Drive roots while preserving each logical path.

    A Drive file ID is not enough to reconstruct the local destination: the path is
    carried by the folder ancestry.  Keep this inventory operation in one place so
    callers that have a stale cache can refresh it instead of falling back to a
    filename-only path (which silently dumps a file at the download root).
    """
    by_id: Dict[str, Dict] = {}
    counts: Dict[str, int] = {}
    for root in dict.fromkeys(roots):
        listed = DriveMirror(service, root_folder=root).list_files()
        counts[root] = len(listed)
        for item in listed:
            by_id.setdefault(item["id"], item)
    return sorted(by_id.values(), key=lambda item: item["rel_path"].lower()), counts


def _pick_keeper(candidates: List[Dict], rel_path: str, ledger: Optional[Ledger]) -> Dict:
    """Choose which copy of a duplicated file survives.

    The ledger's recorded drive_id is the strongest signal - it is what the rest of
    the pipeline (pull, notes, chat memory) already points at for this path, so
    trashing it would orphan those references even though the file itself is fine.
    Falls back to the most recently modified copy when the ledger has nothing (or
    disagrees, e.g. a pre-ledger record) to go on.
    """
    if ledger is not None:
        known_id = (ledger.get(rel_path) or {}).get("drive_id")
        for candidate in candidates:
            if candidate["id"] == known_id:
                return candidate
    return max(candidates, key=lambda c: c.get("modifiedTime") or "")


def _scan_root(service, root_folder: str) -> Tuple[List[Dict], List[Dict]]:
    """One root's files (each tagged with its rel_path) and its own within-root
    folder-name collisions - a sibling folder sharing a name under the very same
    parent, which is a bug there in a way it is not across two different roots.
    """
    mirror = DriveMirror(service, root_folder=root_folder)
    root_id = mirror.root_id()
    children = _walk_tree(service)

    files: List[Dict] = []
    folder_clusters: List[Dict] = []
    pending: List[Tuple[str, List[str]]] = [(root_id, [])]
    visited: set[str] = set()
    while pending:
        parent_id, parts = pending.pop()
        if parent_id in visited:
            continue
        visited.add(parent_id)
        by_name: Dict[str, List[Dict]] = {}
        for item in children.get(parent_id, []):
            by_name.setdefault(item["name"], []).append(item)
            if item.get("mimeType") == FOLDER_MIME:
                pending.append((item["id"], parts + [item["name"]]))
        for name, group in by_name.items():
            rel_path = "/".join(parts + [name])
            folders_only = [g for g in group if g.get("mimeType") == FOLDER_MIME]
            files.extend(dict(g, rel_path=rel_path)
                        for g in group if g.get("mimeType") != FOLDER_MIME)
            if len(folders_only) > 1:
                folder_clusters.append({
                    "rel_path": rel_path, "type": "folder", "root": root_folder,
                    "ids": [f["id"] for f in folders_only],
                })
    return files, folder_clusters


def find_duplicate_files(service, root_folder: str, legacy_roots: Tuple[str, ...] = (),
                         ledger: Optional[Ledger] = None) -> List[Dict]:
    """Find files that share a logical path, and folders that share a name under
    the same parent.

    Two pushes racing the same not-yet-indexed Drive folder produce the first kind:
    ``DriveMirror.upload``'s existence check (``files.list`` with a ``q`` filter) is
    only eventually consistent, so two near-simultaneous uploads for the same
    logical path can each miss the other's just-created file and both ``create``
    instead of one ``create`` and one ``update`` - see ``push_lock`` for the fix that
    stops that happening again.

    ``legacy_roots`` covers the other source: a rename leaves the pipeline pushing
    to a new root while old material sits, un-migrated, under the old one (see
    ``LEGACY_ROOT_FOLDERS``). ``do_drive_list`` already merges these roots into one
    listing for display, which is exactly why the same course material re-pushed
    after the rename reads as a duplicate there - this compares across all of them
    the same way, by rel_path rather than by shared Drive parent, since the two
    copies live in genuinely different folder trees.

    Folder collisions are reported but never auto-resolved: merging their contents
    needs a human decision (which children conflict, which one is truly newer),
    where a duplicate *file* has an unambiguous, reversible answer (keep one, trash
    the rest - see ``trash_files``).
    """
    all_files, clusters = [], []
    for root in (root_folder,) + tuple(legacy_roots):
        files, folder_clusters = _scan_root(service, root)
        all_files.extend(files)
        clusters.extend(folder_clusters)

    by_path: Dict[str, List[Dict]] = {}
    for item in all_files:
        by_path.setdefault(item["rel_path"], []).append(item)
    for rel_path, group in by_path.items():
        if len(group) > 1:
            keep = _pick_keeper(group, rel_path, ledger)
            clusters.append({
                "rel_path": rel_path, "type": "file",
                "keep": keep["id"],
                "trash": [f["id"] for f in group if f["id"] != keep["id"]],
                "sizes": [int(f.get("size") or 0) for f in group],
            })
    return sorted(clusters, key=lambda c: c["rel_path"].lower())


def trash_files(service, file_ids: List[str]) -> List[str]:
    """Move files to Drive's own trash - recoverable there, never a hard delete."""
    trashed = []
    for file_id in file_ids:
        service.files().update(fileId=file_id, body={"trashed": True},
                               supportsAllDrives=True).execute()
        trashed.append(file_id)
    return trashed


def pull_file(service, item: Dict, download_root: str,
              progress: Optional[Callable[[float], None]] = None) -> str:
    """Download one listed Drive item below download_root, replacing atomically."""
    _require_libs()
    from googleapiclient.http import MediaIoBaseDownload

    root = os.path.abspath(download_root)
    rel_path = item["rel_path"]
    export = GOOGLE_EXPORTS.get(item.get("mime_type") or "")
    if export and not rel_path.lower().endswith(export[1]):
        rel_path += export[1]
    normalized = os.path.normpath(rel_path.replace("/", os.sep))
    logical_target = os.path.realpath(os.path.join(root, normalized))
    root_real = os.path.realpath(root)
    try:
        inside_root = os.path.commonpath([root_real, logical_target]) == root_real
    except ValueError:
        inside_root = False
    if not inside_root or normalized in (".", ".."):
        raise DriveError("Unsafe Drive path: {}".format(item["rel_path"]))
    # A Drive-mirrored rel_path carries no length limit, unlike the on-disk paths
    # sync.py produces when files are first pulled off Blackboard - a deeply nested
    # course archive (a folder-per-year exam bank) restores past Windows' 260-char
    # MAX_PATH and fails with ENOENT before this shortens it the same way sync.py
    # already shortens the original download.
    target = utils.shorten_path_for_disk(root, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".{}-".format(os.path.basename(target)),
        suffix=".pulling",
        dir=os.path.dirname(target),
    )
    os.close(fd)
    try:
        if export:
            request = service.files().export_media(fileId=item["id"], mimeType=export[0])
        else:
            request = service.files().get_media(fileId=item["id"], supportsAllDrives=True)
        with open(temporary, "wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status and progress:
                    progress(status.progress())
        expected = int(item.get("size") or 0)
        if expected and not export and os.path.getsize(temporary) != expected:
            raise DriveError("Size mismatch while pulling {}".format(item["rel_path"]))
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)

def push_file(
    mirror: DriveMirror,
    entry: Dict,
    download_root: str,
    ledger: Ledger,
    move: bool = True,
    progress: Optional[Callable[[float], None]] = None,
) -> Dict:
    """Upload one planned entry, verify it, then reclaim the local copy.

    The local file is only deleted after Drive echoes back a size matching the bytes on
    disk. A mismatch leaves the file alone and raises.
    """
    local_path = entry["path"]
    if not os.path.isfile(local_path):
        raise DriveError("Not on disk: {}".format(local_path))

    local_size = os.path.getsize(local_path)
    disk_rel = os.path.relpath(local_path, download_root)
    # The Drive tree mirrors the entry's logical path, not the on-disk one: the disk
    # path is shortened and hashed to fit Windows' MAX_PATH (sync.py), but Drive has
    # no such limit, and a shortened name is unsearchable - a course title truncated
    # to "Optimization Meth-a3f92b1c" no longer contains the words a student would
    # actually search for. The ledger is keyed the same way, for the same reason: it
    # survives the download root moving.
    ledger_key = entry.get("rel_path") or disk_rel
    logical_dir, logical_name = os.path.split(ledger_key.replace("\\", "/"))
    parts = logical_dir.split("/")
    parent_id = mirror.ensure_path([p for p in parts if p])

    result = mirror.upload(local_path, parent_id, name=logical_name, progress=progress)

    remote_size = int(result.get("size") or 0)
    # Google Docs-converted files report no size; only enforce when Drive gives one.
    if remote_size and remote_size != local_size:
        raise DriveError(
            "Size mismatch for {}: local {} vs Drive {} - keeping local copy".format(
                disk_rel, local_size, remote_size
            )
        )

    ledger.record(
        ledger_key,
        drive_id=result["id"],
        remote_modified=entry.get("modified"),
        size=local_size,
        folder_id=parent_id,
        source_id=entry.get("source_id") or "",
    )

    if move:
        # Make the archive record durable before deleting the only local copy.
        ledger.save()
        os.remove(local_path)
        _prune_empty_dirs(os.path.dirname(local_path), download_root)

    return {
        "rel_path": ledger_key,
        "drive_id": result["id"],
        "size": local_size,
        "moved": move,
    }


def _prune_empty_dirs(directory: str, stop_at: str):
    """Walk upward removing directories left empty by the move, never past the root."""
    stop_at = os.path.realpath(stop_at)
    directory = os.path.realpath(directory)
    while directory != stop_at and os.path.commonpath((stop_at, directory)) == stop_at:
        try:
            if os.listdir(directory):
                return
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)
