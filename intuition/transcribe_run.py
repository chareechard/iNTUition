"""Transcribe staged lecture media, and backfill videos already moved to Drive.

Two modes:

    # Everything staged locally that has no transcript yet
    python -m intuition.transcribe_run --download_to NTU

    # Videos already relayed to Drive: pull back, transcribe, upload the transcript
    python -m intuition.transcribe_run --backfill

Run the first before pushing, so the video and its transcript travel together. Move
mode deletes the local video after upload, so a transcript generated later would have
nothing to work from - that is what --backfill exists to repair.
"""
import argparse
import os
import sys
import tempfile
import time

from intuition import drive, transcribe
from intuition.ledger import Ledger


def human_mins(seconds: float) -> str:
    return "{:.1f} min".format(seconds / 60) if seconds >= 60 else "{:.0f}s".format(seconds)


def transcribe_staged(root: str, model: str, compute_type: str, threads: int,
                      redo: bool = False) -> int:
    media = transcribe.find_media(root, skip_transcribed=not redo)
    if not media:
        print("No untranscribed media under {}".format(root))
        return 0

    print("{} file(s) to transcribe with {}".format(len(media), model))
    engine = transcribe.Transcriber(model, compute_type, cpu_threads=threads)
    ok = failed = 0
    for i, path in enumerate(media, 1):
        rel = os.path.relpath(path, root)
        print("[{}/{}] {}".format(i, len(media), rel), flush=True)
        t0 = time.time()
        try:
            engine.transcribe(path, progress=_ticker())
            print("      done in {}".format(human_mins(time.time() - t0)), flush=True)
            ok += 1
        except transcribe.TranscribeError as e:
            print("      FAILED: {}".format(e), flush=True)
            failed += 1
    print("\nTranscribed {}, failed {}".format(ok, failed))
    return 1 if failed else 0


def _ticker():
    """Print a progress line every 20% rather than per segment."""
    state = {"last": 0.0}

    def report(frac, text):
        if frac - state["last"] >= 0.2:
            state["last"] = frac
            print("      {:3.0f}%  {}".format(frac * 100, text), flush=True)

    return report


def backfill(root_folder: str, model: str, compute_type: str, threads: int,
             dry_run: bool = False) -> int:
    """Transcribe videos that are already in Drive and no longer held locally."""
    from googleapiclient.http import MediaIoBaseDownload

    svc = drive.build_service()
    mirror = drive.DriveMirror(svc, root_folder=root_folder)
    # Same detection transcribe.classify_drive_media() gives the dashboard's
    # Transcription tab, so "found under Drive/<root>/" means the same thing in
    # both places.
    media = transcribe.classify_drive_media(mirror.list_files())
    if not media:
        print("No media found in Drive/{}/".format(root_folder))
        return 0

    todo = [e for e in media if e["status"] == transcribe.MISSING]
    print("{} media file(s) in Drive, {} without a transcript".format(
        len(media), len(todo)))
    for e in todo:
        print("  {}  ({:.1f} MB)".format(e["rel_path"], e["size"] / 1048576))
    if dry_run or not todo:
        return 0

    engine = transcribe.Transcriber(model, compute_type, cpu_threads=threads)
    ok = failed = 0

    with tempfile.TemporaryDirectory(prefix="intuition_backfill_") as tmp:
        for i, e in enumerate(todo, 1):
            print("\n[{}/{}] {}".format(i, len(todo), e["rel_path"]), flush=True)
            local = os.path.join(tmp, e["name"])
            try:
                print("      downloading...", flush=True)
                with open(local, "wb") as fh:
                    dl = MediaIoBaseDownload(
                        fh, svc.files().get_media(fileId=e["drive_id"]),
                        chunksize=8 * 1024 * 1024)
                    done = False
                    while not done:
                        _status, done = dl.next_chunk()

                t0 = time.time()
                paths = engine.transcribe(local, progress=_ticker())
                print("      transcribed in {}".format(human_mins(time.time() - t0)),
                      flush=True)

                parent_id = mirror.ensure_path(
                    [p for p in os.path.dirname(e["rel_path"]).split("/") if p])
                for kind in ("vtt", "txt"):
                    mirror.upload(paths[kind], parent_id)
                    print("      uploaded {}".format(os.path.basename(paths[kind])),
                          flush=True)
                ok += 1
            except Exception as e2:  # noqa: BLE001 - one bad video must not end the run
                print("      FAILED: {}".format(e2), flush=True)
                failed += 1
            finally:
                # Reclaim the temp copy immediately; these are large.
                for p in (local,) + tuple(
                        transcribe.transcript_paths(local).values()):
                    if os.path.exists(p):
                        os.remove(p)

    print("\nBackfilled {}, failed {}".format(ok, failed))
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description="Generate Whisper transcripts for iNTUition lecture media")
    parser.add_argument("--download_to", default="NTU",
                        help="Local staging folder to transcribe")
    parser.add_argument("--model", default=transcribe.DEFAULT_MODEL,
                        help="Whisper model (default: %(default)s)")
    parser.add_argument("--compute_type", default=transcribe.DEFAULT_COMPUTE_TYPE,
                        help="CTranslate2 compute type (default: %(default)s)")
    parser.add_argument("--threads", type=int, default=0,
                        help="CPU threads, 0 lets the backend decide")
    parser.add_argument("--redo", action="store_true",
                        help="Re-transcribe files that already have a transcript")
    parser.add_argument("--backfill", action="store_true",
                        help="Transcribe media already in Drive rather than staged locally")
    parser.add_argument("--drive_folder", default=drive.DEFAULT_ROOT_FOLDER,
                        help="Drive root folder for --backfill")
    parser.add_argument("--dry_run", action="store_true",
                        help="List what would be transcribed and exit")
    parser.add_argument("--list", action="store_true", dest="do_list",
                        help="Show every staged media file and whether a transcript "
                             "already exists, without transcribing anything")
    args = parser.parse_args()

    if args.do_list:
        root = os.path.abspath(args.download_to)
        rows = transcribe.survey(root) if os.path.isdir(root) else []
        if not rows:
            print("No media staged under {}".format(root))
            return 0
        print("%-11s %8s  %s" % ("STATUS", "SIZE", "FILE"))
        for r in rows:
            note = "  <- {}".format(", ".join(r["sources"])) if r["sources"] else ""
            print("%-11s %7.1fMB  %s%s" % (
                r["status"], r["size"] / 1048576, r["rel_path"], note))
        missing = [r for r in rows if r["status"] == transcribe.MISSING]
        print("\n{} of {} need Whisper.".format(len(missing), len(rows)))
        return 0

    if args.backfill:
        return backfill(args.drive_folder, args.model, args.compute_type,
                        args.threads, dry_run=args.dry_run)

    root = os.path.abspath(args.download_to)
    if not os.path.isdir(root):
        print("No such folder: {}".format(root))
        return 1
    if args.dry_run:
        media = transcribe.find_media(root, skip_transcribed=not args.redo)
        print("{} file(s) would be transcribed:".format(len(media)))
        for m in media:
            print("  " + os.path.relpath(m, root))
        return 0
    return transcribe_staged(root, args.model, args.compute_type, args.threads,
                             redo=args.redo)


if __name__ == "__main__":
    sys.exit(main())
