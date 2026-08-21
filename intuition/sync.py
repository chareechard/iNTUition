"""Compare a Blackboard content tree against what is already on disk.

This is what turns the downloader into a sync tool: instead of blindly walking the tree
and skipping files that happen to exist, it produces an explicit plan saying which items
are new, which changed on Blackboard since they were downloaded, and which are already
up to date.

Statuses
--------
``new``       - not present locally and not in the Drive ledger
``updated``   - already held, but Blackboard reports a newer ``modified`` timestamp
``current``   - present locally and not known to have changed
``archived``  - no longer on disk, but the ledger confirms it is in Drive and unchanged
``ignored``   - a video the user previously declined (dummy marker file present)
``unknown``   - cannot be resolved without a network call (legacy scraper items whose
                filename only appears in the download redirect)

``archived`` is what keeps the move pipeline from looping: once a file is uploaded and
the local copy deleted, the ledger is the only evidence it was ever fetched.

Logical vs on-disk paths
------------------------
Each entry carries two paths, and the distinction matters:

``rel_path``  the *logical* location - the Blackboard folder titles joined as-is. This
              is the file's identity and the ledger key. It depends only on the course
              tree, so it survives the download root being moved or renamed.
``path``      where the bytes actually go, which is ``rel_path`` shortened as needed to
              keep the absolute path inside Windows' limits.

Keying the ledger on the on-disk path was a bug: the shortening rules measure the
*absolute* path, so relocating the download root silently changed the key of every
deep file, orphaning its ledger entry and re-downloading material already in Drive.
"""
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from intuition.utils import bounded_filename, dummy_file_exists, sanitise_filename

NEW = "new"
UPDATED = "updated"
CURRENT = "current"
ARCHIVED = "archived"
IGNORED = "ignored"
UNKNOWN = "unknown"

DOWNLOADABLE = (NEW, UPDATED, UNKNOWN)
# Statuses meaning "the bytes are on this machine right now", i.e. pushable to Drive.
PUSHABLE = (CURRENT, UPDATED)

# Longest absolute folder path we will create. Leaves room under Windows' 260-char
# MAX_PATH for the filename itself, which bounded_filename trims separately.
MAX_FOLDER_PATH = 185
# How much of an over-long folder title to keep before the disambiguating hash.
_SEGMENT_KEEP = 24
_HASH_LEN = 8


def _branch_hash(logical_rel: str) -> str:
    """Short stable digest of a logical path, used to keep shortened names distinct."""
    return hashlib.sha256(logical_rel.encode("utf-8")).hexdigest()[:_HASH_LEN]


# The REST attachment download URL, and the bbcswebdav links used by files embedded
# in an Ultra document body. Both carry ids that survive the item being moved.
_REST_ATTACHMENT = re.compile(
    r"/courses/[^/]+/contents/([^/]+)/attachments/([^/]+)/download"
)
_WEBDAV_XID = re.compile(r"/xid-([\w.-]+)")


def source_id(predownload_link: Optional[str]) -> str:
    """The Blackboard resource identity behind a planned file, or "" if unknown.

    Paths are not identity: when a lecturer inserts a folder level, every file below
    it moves and a path-keyed ledger calls the whole subtree new. The content and
    attachment ids do not move with it, so they are what the ledger should remember.

    Returns "" for the legacy scraper's links, which carry no stable id; those fall
    back to path matching as before.
    """
    link = predownload_link or ""
    match = _REST_ATTACHMENT.search(link)
    if match:
        return "bb:{}/{}".format(*match.groups())
    match = _WEBDAV_XID.search(link)
    if match:
        return "xid:{}".format(match.group(1))
    return ""


def parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse Blackboard's ``2026-08-06T12:49:13.403Z`` timestamps."""
    if not value:
        return None
    try:
        # Python < 3.11 does not accept the trailing "Z" in fromisoformat.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_mtime(path: str) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return None


def classify(
    path: str, remote_modified: Optional[str], ledger_entry: Optional[Dict] = None
) -> str:
    """Decide the status of a single target path.

    ``ledger_entry`` is the Drive archival record for this path, if any. It is only
    consulted when the local file is gone - that is the case the move pipeline creates.
    """
    remote = parse_iso8601(remote_modified)

    if not os.path.exists(path):
        if ledger_entry is None:
            return NEW
        # Uploaded to Drive and the local copy reclaimed. Re-fetch only if Blackboard
        # has changed the file since we archived it.
        archived_stamp = parse_iso8601(ledger_entry.get("remote_modified"))
        if archived_stamp is None:
            # Archived before timestamps were recorded; cannot prove it is stale.
            return ARCHIVED
        return UPDATED if remote is not None and remote > archived_stamp else ARCHIVED

    if remote is None:
        # No timestamp to compare against; presence is all we know.
        return CURRENT

    local = _local_mtime(path)
    if local is None:
        return CURRENT
    return UPDATED if remote > local else CURRENT


def build_plan(
    tree: Dict,
    download_root: str,
    ignore_files: bool = False,
    ignore_recorded_lectures: bool = True,
    ledger=None,
) -> List[Dict]:
    """Walk a serialized course tree and return a flat list of planned items.

    Each entry: {name, filename, path, rel_path, folder, status, type,
                 predownload_link, modified, drive_id}

    ``ledger`` is an optional :class:`intuition.ledger.Ledger`; pass it so
    files already moved to Drive are reported as ``archived`` instead of ``new``.
    """
    plan: List[Dict] = []

    def ledger_entry(logical_rel: str, legacy_path, source: str) -> Optional[Dict]:
        """Find this file's archival record, re-keying it onto the current identity.

        Three ways in, most durable first: the Blackboard resource id, the stable
        logical path, then the on-disk path *as the superseded code laid it out*.
        Each fallback exists for records written by an older version, and a hit
        upgrades the record in place - otherwise this scan would re-download files
        that are already sitting in Drive.

        ``legacy_path`` is a callable, evaluated only when the earlier lookups miss.
        It reproduces the old key only while the download root is where it was when
        the file was archived - which is what a first scan after upgrading is.
        """
        if ledger is None:
            return None

        # 1. Resource id: survives the file being moved to another folder.
        found = ledger.find_by_source(source)
        if found is not None:
            key, entry = found
            if key != logical_rel:
                ledger.migrate(key, logical_rel)
            return entry

        # 2. Stable logical path, for records written before ids were recorded.
        entry = ledger.get(logical_rel)
        if entry is not None:
            ledger.attach_source(logical_rel, source)
            return entry

        try:
            legacy_path = legacy_path()
        except OSError:
            # The superseded naming could refuse a pathological path outright. There
            # is simply no old key to look up then; do not let it abort the scan.
            return None
        legacy_key = os.path.relpath(legacy_path, download_root).replace("\\", "/")
        if legacy_key == logical_rel:
            return None
        entry = ledger.get(legacy_key)
        if entry is not None:
            ledger.migrate(legacy_key, logical_rel)
            ledger.attach_source(logical_rel, source)
        return entry

    def legacy_folder(parent: str, segment: str) -> str:
        """Where the superseded code would have put this folder.

        It dropped an over-long segment entirely and hoisted the children into the
        parent. Kept only to recognise ledger keys written that way.
        """
        candidate = os.path.join(parent, segment, "")
        return parent if len(os.path.abspath(candidate)) > MAX_FOLDER_PATH else candidate

    def disk_folder(parent_disk: str, segment: str, logical_rel: str):
        """Choose an on-disk folder for one logical segment, bounded for Windows.

        Ultra courses repeat folder titles six or more levels deep, so the natural
        path can blow past MAX_PATH. Previously the segment was dropped entirely and
        its children were hoisted into the parent - which merged sibling branches,
        so two distinct files could land on one path and overwrite each other. Now
        the segment is shortened instead, with a hash of the logical path keeping
        distinct branches in distinct directories.
        """
        candidate = os.path.join(parent_disk, segment)
        if len(os.path.abspath(candidate)) <= MAX_FOLDER_PATH:
            return candidate, True
        # "-" not "~": sanitise_filename strips a tilde, which silently glued the
        # hash onto the title ("Chapter 8cdfec43c") and made the name unreadable.
        short = "{}-{}".format(
            segment[:_SEGMENT_KEEP].rstrip(" .-_"), _branch_hash(logical_rel)
        )
        shortened = os.path.join(parent_disk, short)
        if len(os.path.abspath(shortened)) <= MAX_FOLDER_PATH:
            return shortened, False
        # Not even the shortened name fits. Keep the file in the parent; the branch
        # hash goes into the filename instead so uniqueness is still guaranteed.
        return parent_disk, False

    def disk_name(disk_dir: str, raw_name: str, logical_rel: str, faithful: bool) -> str:
        """Pick the on-disk filename, disambiguating when the folder was shortened."""
        name = sanitise_filename(raw_name)
        if not faithful:
            stem, extension = os.path.splitext(name)
            name = "{}-{}{}".format(
                stem.rstrip(" .-_"), _branch_hash(logical_rel), extension
            )
        return bounded_filename(disk_dir, name)

    def walk(node: Dict, current_path: str, logical: str, faithful: bool,
             legacy: str):
        node_type = node.get("type")

        if node_type == "folder":
            segment = sanitise_filename(node.get("name", ""))
            child_logical = "/".join(p for p in (logical, segment) if p)
            folder_path, intact = disk_folder(current_path, segment, child_logical)
            child_legacy = legacy_folder(legacy, segment)
            for child in node.get("children") or []:
                walk(child, folder_path, child_logical, faithful and intact,
                     child_legacy)
            return

        if node_type == "file":
            if ignore_files:
                return
            filename = node.get("filename")
            # Legacy scraper: the real filename only appears in the redirect.
            raw_name = filename or node.get("name", "")
            logical_rel = "/".join(p for p in (logical, sanitise_filename(raw_name)) if p)
            safe_name = disk_name(current_path, raw_name, logical_rel, faithful)
            full_path = os.path.join(current_path, safe_name)
            if not os.path.exists(full_path):
                candidate_rel = os.path.join(download_root, logical_rel)
                candidate_unhashed = os.path.join(
                    current_path, bounded_filename(current_path, sanitise_filename(raw_name))
                )
                if os.path.isfile(candidate_rel):
                    full_path = candidate_rel
                elif os.path.isfile(candidate_unhashed):
                    full_path = candidate_unhashed

            link = node.get("predownload_link")
            source = source_id(link)
            entry = ledger_entry(
                logical_rel,
                lambda: os.path.join(legacy, bounded_filename(legacy, raw_name)),
                source,
            )
            status = (
                classify(full_path, node.get("modified"), entry) if filename else UNKNOWN
            )
            plan.append(
                {
                    "type": "file",
                    "name": node.get("name"),
                    "filename": filename,
                    "path": full_path,
                    "rel_path": logical_rel,
                    "folder": logical,
                    "status": status,
                    "modified": node.get("modified"),
                    "predownload_link": link,
                    "source_id": source,
                    "drive_id": (entry or {}).get("drive_id"),
                }
            )
            return

        if node_type == "recorded_lecture":
            if ignore_recorded_lectures:
                return
            title = node.get("name", "") + ".mp4"
            logical_rel = "/".join(
                p for p in (logical, sanitise_filename(title)) if p
            )
            video_name = disk_name(current_path, title, logical_rel, faithful)
            full_path = os.path.join(current_path, video_name)
            source = source_id(node.get("predownload_link"))
            entry = ledger_entry(
                logical_rel,
                lambda: os.path.join(legacy, sanitise_filename(title)),
                source,
            )
            if dummy_file_exists(current_path, video_name):
                status = IGNORED
            else:
                status = classify(full_path, node.get("modified"), entry)
            plan.append(
                {
                    "type": "recorded_lecture",
                    "name": node.get("name"),
                    "filename": video_name,
                    "path": full_path,
                    "rel_path": logical_rel,
                    "folder": logical,
                    "status": status,
                    "modified": node.get("modified"),
                    "predownload_link": node.get("predownload_link"),
                    "source_id": source,
                    "drive_id": (entry or {}).get("drive_id"),
                }
            )

    walk(tree, download_root, "", True, download_root)
    return plan


def _is_ordered_subset(parts: List[str], whole: List[str]) -> bool:
    """True if ``parts`` appears within ``whole`` in order (gaps allowed)."""
    remaining = iter(whole)
    return all(part in remaining for part in parts)


def recover_restructured(plan: List[Dict], ledger, download_root: str) -> List[str]:
    """Re-key archived files that a course reorganisation left looking new.

    Records written before resource ids existed can only be matched by path, so
    inserting a folder level in Learn orphans everything below it: the files sit in
    Drive, but the next scan calls them ``new`` and downloads and re-uploads the lot.

    A record is only claimed when its path is an in-order subset of the candidate's
    path and exactly one unclaimed record matches that filename - an inserted folder
    level, not a guess. Anything ambiguous is left alone to be re-downloaded, which
    is wasteful but never wrong.

    Pass the whole plan, across every course, so a file cannot be matched to a
    record that another course's entry already accounts for. Returns the re-keyed
    paths; the caller decides whether to persist and how to report.
    """
    if ledger is None:
        return []

    claimed = {e["rel_path"] for e in plan if e.get("status") != NEW}
    unclaimed: Dict[str, List[str]] = {}
    for key in list(ledger.entries):
        if key in claimed:
            continue
        unclaimed.setdefault(os.path.basename(key), []).append(key)

    recovered: List[str] = []
    for entry in plan:
        if entry.get("status") != NEW:
            continue
        logical = entry["rel_path"]
        candidates = unclaimed.get(os.path.basename(logical))
        if not candidates:
            continue
        matches = [
            key for key in candidates
            if _is_ordered_subset(key.split("/"), logical.split("/"))
        ]
        if len(matches) != 1:
            continue

        old_key = matches[0]
        ledger.migrate(old_key, logical)
        ledger.attach_source(logical, entry.get("source_id") or "")
        candidates.remove(old_key)

        record = ledger.get(logical) or {}
        entry["status"] = classify(entry["path"], entry.get("modified"), record)
        entry["drive_id"] = record.get("drive_id")
        recovered.append(logical)

    return recovered


def summarize(plan: List[Dict]) -> Dict[str, int]:
    counts = {NEW: 0, UPDATED: 0, CURRENT: 0, ARCHIVED: 0, IGNORED: 0, UNKNOWN: 0}
    for entry in plan:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts
