"""Find and clean up files duplicated under the Drive push root.

    python -m intuition.drive_dedupe --download_to NTU

Two pushes racing the same not-yet-indexed Drive folder - the dashboard's own push
and a scheduled ``drive_push`` run, say - can each miss the other's freshly created
file and both upload, leaving two Drive files for one logical path. ``drive.push_lock``
stops that happening again; this is how the fallout from before that fix gets found
and cleaned up.

Reports what it finds and changes nothing unless ``--apply`` is passed. Even then,
duplicates are moved to Drive's own Trash, never hard-deleted - a mistaken run is
recoverable from Drive itself. Duplicate folders (same name, same parent) are
reported but never auto-resolved: merging their contents needs a human decision that
a script cannot make safely.
"""
import argparse
import os
import sys

from intuition import drive
from intuition.ledger import Ledger


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024.0
    return str(size)


def main():
    parser = argparse.ArgumentParser(
        description="Find (and optionally trash) files duplicated under the Drive push root"
    )
    parser.add_argument(
        "--download_to", default="NTU",
        help="Local sync folder, read only for the ledger's keeper preference (default: %(default)s)")
    parser.add_argument(
        "--drive_folder", default=drive.DEFAULT_ROOT_FOLDER,
        help="Drive root folder to scan (default: %(default)s)")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually trash the duplicates found (default: report only, changes nothing)")
    args = parser.parse_args()

    if not drive.credentials_present():
        print(drive.SETUP_HELP)
        return 1

    try:
        service = drive.build_service()
    except drive.DriveError as e:
        print(e)
        return 1

    root = os.path.abspath(args.download_to)
    ledger = Ledger(root) if os.path.isdir(root) else None

    # A renamed app leaves old material un-migrated under its old root (see
    # LEGACY_ROOT_FOLDERS) - the dashboard's own listing already merges it in, so a
    # duplicate scan has to look there too or it will miss exactly the duplicates
    # that merge produces.
    legacy_roots = (tuple(r for r in drive.LEGACY_ROOT_FOLDERS if r != args.drive_folder)
                    if args.drive_folder == drive.DEFAULT_ROOT_FOLDER else ())
    scanned = ", ".join(("Drive/{}/".format(r) for r in (args.drive_folder,) + legacy_roots))
    print("Scanning {} for duplicates...".format(scanned))
    clusters = drive.find_duplicate_files(
        service, args.drive_folder, legacy_roots=legacy_roots, ledger=ledger)
    if not clusters:
        print("No duplicates found.")
        return 0

    file_clusters = [c for c in clusters if c["type"] == "file"]
    folder_clusters = [c for c in clusters if c["type"] == "folder"]

    total_trash = sum(len(c["trash"]) for c in file_clusters)
    reclaimed = sum(sum(c["sizes"]) - max(c["sizes"]) for c in file_clusters)
    if file_clusters:
        print("\n{} duplicated file path(s), {} extra cop{} to remove ({} reclaimed):".format(
            len(file_clusters), total_trash, "y" if total_trash == 1 else "ies",
            human(reclaimed)))
        for c in file_clusters:
            print("  {}  (keeping {}, trashing {})".format(
                c["rel_path"], c["keep"], ", ".join(c["trash"])))

    if folder_clusters:
        print("\n{} duplicated folder name(s) - not auto-resolved, review manually:".format(
            len(folder_clusters)))
        for c in folder_clusters:
            print("  {}  ({})".format(c["rel_path"], ", ".join(c["ids"])))

    if not file_clusters:
        return 0

    if not args.apply:
        print("\nDry run - nothing changed. Re-run with --apply to trash the "
              "{} duplicate(s) above.".format(total_trash))
        return 0

    all_ids = [file_id for c in file_clusters for file_id in c["trash"]]
    trashed = drive.trash_files(service, all_ids)
    print("\nTrashed {} file(s). Recoverable from Drive's own Trash.".format(len(trashed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
