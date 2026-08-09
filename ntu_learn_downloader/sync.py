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
"""
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ntu_learn_downloader.utils import dummy_file_exists, sanitise_filename

NEW = "new"
UPDATED = "updated"
CURRENT = "current"
ARCHIVED = "archived"
IGNORED = "ignored"
UNKNOWN = "unknown"

DOWNLOADABLE = (NEW, UPDATED, UNKNOWN)
# Statuses meaning "the bytes are on this machine right now", i.e. pushable to Drive.
PUSHABLE = (CURRENT, UPDATED)


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
        if remote is not None and archived_stamp is not None and remote > archived_stamp:
            return UPDATED
        if remote is not None and archived_stamp is None:
            # Archived before timestamps were recorded; cannot prove it is stale.
            return ARCHIVED
        return ARCHIVED

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

    ``ledger`` is an optional :class:`ntu_learn_downloader.ledger.Ledger`; pass it so
    files already moved to Drive are reported as ``archived`` instead of ``new``.
    """
    plan: List[Dict] = []

    def ledger_entry(full_path: str) -> Optional[Dict]:
        if ledger is None:
            return None
        return ledger.get(os.path.relpath(full_path, download_root))

    def walk(node: Dict, current_path: str):
        node_type = node.get("type")

        if node_type == "folder":
            folder_path = os.path.join(
                current_path, sanitise_filename(node.get("name", "")), ""
            )
            for child in node.get("children") or []:
                walk(child, folder_path)
            return

        if node_type == "file":
            if ignore_files:
                return
            filename = node.get("filename")
            if filename:
                safe_name = sanitise_filename(filename)
                full_path = os.path.join(current_path, safe_name)
                entry = ledger_entry(full_path)
                status = classify(full_path, node.get("modified"), entry)
            else:
                # Legacy scraper: the real filename only appears in the redirect.
                safe_name = sanitise_filename(node.get("name", ""))
                full_path = os.path.join(current_path, safe_name)
                entry = ledger_entry(full_path)
                status = UNKNOWN
            plan.append(
                {
                    "type": "file",
                    "name": node.get("name"),
                    "filename": filename,
                    "path": full_path,
                    "rel_path": os.path.relpath(full_path, download_root),
                    "folder": os.path.relpath(current_path, download_root),
                    "status": status,
                    "modified": node.get("modified"),
                    "predownload_link": node.get("predownload_link"),
                    "drive_id": (entry or {}).get("drive_id"),
                }
            )
            return

        if node_type == "recorded_lecture":
            if ignore_recorded_lectures:
                return
            video_name = sanitise_filename(node.get("name", "") + ".mp4")
            full_path = os.path.join(current_path, video_name)
            entry = ledger_entry(full_path)
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
                    "rel_path": os.path.relpath(full_path, download_root),
                    "folder": os.path.relpath(current_path, download_root),
                    "status": status,
                    "modified": node.get("modified"),
                    "predownload_link": node.get("predownload_link"),
                    "drive_id": (entry or {}).get("drive_id"),
                }
            )

    walk(tree, download_root)
    return plan


def summarize(plan: List[Dict]) -> Dict[str, int]:
    counts = {NEW: 0, UPDATED: 0, CURRENT: 0, ARCHIVED: 0, IGNORED: 0, UNKNOWN: 0}
    for entry in plan:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts
