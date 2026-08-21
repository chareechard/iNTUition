"""Push everything in the local download folder to Google Drive, then reclaim the disk.

    python -m intuition.drive_push --download_to NTU

Mirrors the selected local folder structure as-is, so a file under the staging root\nlands under the configured iNTUition folder in Drive.

Safe to re-run and safe to schedule: uploads are verified before the local file is
removed, and anything already recorded in the ledger is left alone.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

from intuition import drive
from intuition.ledger import STORAGE_DIR, Ledger


def _mtime_stamp(path: str) -> str:
    """Local modification time as a Blackboard-shaped UTC timestamp.

    Walking the disk gives us no Blackboard metadata, but the ledger needs *some*
    stamp: recorded as None, a later scan can never prove the archived file went
    stale, so Blackboard updates would silently never come back down. The local
    mtime is the right proxy - it is exactly what ``sync.classify`` compares the
    remote stamp against while the file is still on disk.
    """
    return (
        datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def collect_files(download_root: str) -> List[Dict]:
    """Every real file under the download root, excluding tool bookkeeping."""
    found: List[Dict] = []
    for dirpath, dirnames, filenames in os.walk(download_root):
        # Do not upload internal state directories.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            # Dummy markers for declined videos are local-only bookkeeping.
            if filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            found.append(
                {
                    "path": full,
                    "rel_path": os.path.relpath(full, download_root),
                    "size": os.path.getsize(full),
                    "modified": _mtime_stamp(full),
                }
            )
    return sorted(found, key=lambda e: e["rel_path"])


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024.0
    return str(size)


def run_check(drive_folder: str) -> int:
    """Walk the setup one stage at a time and stop at the first thing that is missing.

    The final stage makes real API calls - creating and deleting a probe file - because
    everything short of that can pass while the actual upload path is still broken.
    """
    print("Drive setup check\n" + "-" * 50)

    print("1. client libraries      ", end="")
    try:
        drive._require_libs()
        print("OK")
    except drive.DriveError as e:
        print("MISSING\n\n{}".format(e))
        return 1

    print("2. OAuth client file     ", end="")
    info = drive.inspect_client_secret()
    if not info["ok"]:
        print("FAIL\n   {}\n".format(info["problem"]))
        print(drive.SETUP_HELP)
        return 1
    print("OK  (project: {})".format(info.get("project") or "unknown"))

    print("3. account authorisation ", end="", flush=True)
    if not drive.token_present():
        print("needed - opening your browser for consent...")
    try:
        service = drive.build_service()
    except Exception as e:  # noqa: BLE001 - report any auth failure plainly
        print("   FAILED: {}".format(e))
        return 1
    if drive.token_present():
        print("   token cached at {}".format(drive.TOKEN_PATH))

    print("4. live API probe        ", end="", flush=True)
    try:
        mirror = drive.DriveMirror(service, root_folder=drive_folder)
        folder_id = mirror.ensure_path(["_setup_probe"])
        probe = (
            service.files()
            .create(
                body={"name": "intuition_probe.txt", "parents": [folder_id]},
                fields="id",
            )
            .execute()
        )
        # Clean up both the probe file and the probe folder.
        service.files().delete(fileId=probe["id"]).execute()
        service.files().delete(fileId=folder_id).execute()
    except Exception as e:  # noqa: BLE001
        print("FAILED: {}".format(e))
        return 1
    print("OK  (created and removed a probe in Drive/{}/)".format(drive_folder))

    print("-" * 50)
    print("Ready. Drive root folder: {}/".format(drive_folder))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Move downloaded iNTUition materials into Google Drive"
    )
    parser.add_argument("--download_to", default="NTU", help="Local folder to push from")
    parser.add_argument(
        "--drive_folder",
        default=drive.DEFAULT_ROOT_FOLDER,
        help="Destination root folder in Drive (default: %(default)s)",
    )
    parser.add_argument(
        "--keep_local",
        action="store_true",
        help="Copy instead of move: do not delete local files after upload",
    )
    parser.add_argument(
        "--dry_run", action="store_true", help="List what would be pushed and exit"
    )
    parser.add_argument("--setup", action="store_true", help="Print credential setup steps")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify Drive setup end to end (runs the consent flow if needed)",
    )
    args = parser.parse_args()

    if args.setup:
        drive.ensure_config_dir()
        print(drive.SETUP_HELP)
        return 0

    if args.check:
        return run_check(args.drive_folder)

    root = os.path.abspath(args.download_to)
    if not os.path.isdir(root):
        print("No such folder: {}".format(root))
        return 1

    ledger = Ledger(root)
    files = collect_files(root)
    if not files:
        print("Nothing to push in {}".format(root))
        return 0

    total = sum(f["size"] for f in files)
    print("{} file(s), {} to push from {}".format(len(files), human(total), root))
    print("Destination: Drive/{}/".format(args.drive_folder))
    if args.keep_local:
        print("Mode: copy (local files kept)")
    else:
        print("Mode: move (local files removed after verified upload)")

    if args.dry_run:
        for f in files:
            print("  {}  ({})".format(f["rel_path"], human(f["size"])))
        return 0

    if not drive.credentials_present():
        print("\n" + drive.SETUP_HELP)
        return 1

    try:
        service = drive.build_service()
    except drive.DriveError as e:
        print("\n{}".format(e))
        return 1

    mirror = drive.DriveMirror(service, root_folder=args.drive_folder)

    # This is "safe to schedule" (see module docstring) precisely because of this
    # lock: without it, a scheduled run overlapping the dashboard's own push (or two
    # scheduled runs overlapping each other) can each miss the other's brand new
    # upload and duplicate it - Drive's create-vs-update check is only eventually
    # consistent. See drive.push_lock.
    try:
        with drive.push_lock():
            pushed = failed = 0
            for index, entry in enumerate(files, start=1):
                label = "[{}/{}] {}".format(index, len(files), entry["rel_path"])
                sys.stdout.write(label + " ... ")
                sys.stdout.flush()
                try:
                    drive.push_file(
                        mirror, entry, root, ledger, move=not args.keep_local
                    )
                    pushed += 1
                    print("ok")
                except Exception as e:  # noqa: BLE001 - one bad file must not end the run
                    failed += 1
                    print("FAILED: {}".format(e))
                finally:
                    # Persist after each file so an interrupted run does not lose its record.
                    ledger.save()
    except drive.PushLockError as e:
        print("\n{}".format(e))
        return 1

    print("\nPushed {}, failed {}. Ledger holds {} entries.".format(
        pushed, failed, len(ledger)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
