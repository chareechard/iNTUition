"""Transcribe staged lecture media, and backfill videos already moved to Drive.

Two modes:

    # Everything staged locally that has no transcript yet
    python -m ntu_learn_downloader.transcribe_run --download_to NTU

    # Videos already relayed to Drive: pull back, transcribe, upload the transcript
    python -m ntu_learn_downloader.transcribe_run --backfill

Run the first before pushing, so the video and its transcript travel together. Move
mode deletes the local video after upload, so a transcript generated later would have
nothing to work from - that is what --backfill exists to repair.
"""
import argparse
import os
import sys
import tempfile
import time
from typing import Dict, List

from ntu_learn_downloader import drive, transcribe
from ntu_learn_downloader.ledger import Ledger


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


def _drive_videos(svc, root_folder: str) -> List[Dict]:
    """Every media file under the Drive root, with the folder it lives in."""
    top = svc.files().list(
        q="name='{}' and 'root' in parents and trashed=false and mimeType='{}'".format(
            root_folder, drive.FOLDER_MIME),
        fields="files(id,name)").execute().get("files", [])
    if not top:
        return []

    found: List[Dict] = []

    def walk(fid, path):
        for f in svc.files().list(
                q="'{}' in parents and trashed=false".format(fid),
                fields="files(id,name,mimeType,size)", pageSize=500).execute().get("files", []):
            if f["mimeType"] == drive.FOLDER_MIME:
                walk(f["id"], path + "/" + f["name"])
            elif transcribe.is_media(f["name"]):
                found.append({"id": f["id"], "name": f["name"], "parent": fid,
                              "path": path, "size": int(f.get("size") or 0)})

    walk(top[0]["id"], "")
    return found


def _sibling_names(svc, parent_id: str) -> set:
    return {
        f["name"] for f in svc.files().list(
            q="'{}' in parents and trashed=false".format(parent_id),
            fields="files(name)", pageSize=500).execute().get("files", [])
    }


def backfill(root_folder: str, model: str, compute_type: str, threads: int,
             dry_run: bool = False) -> int:
    """Transcribe videos that are already in Drive and no longer held locally."""
    from googleapiclient.http import MediaIoBaseDownload

    svc = drive.build_service()
    videos = _drive_videos(svc, root_folder)
    if not videos:
        print("No media found in Drive/{}/".format(root_folder))
        return 0

    todo = []
    for v in videos:
        stem = os.path.splitext(v["name"])[0]
        siblings = _sibling_names(svc, v["parent"])
        if stem + ".vtt" in siblings and stem + ".txt" in siblings:
            continue
        todo.append(v)

    print("{} media file(s) in Drive, {} without a transcript".format(
        len(videos), len(todo)))
    for v in todo:
        print("  {}{}  ({:.1f} MB)".format(v["path"], "/" + v["name"], v["size"] / 1048576))
    if dry_run or not todo:
        return 0

    engine = transcribe.Transcriber(model, compute_type, cpu_threads=threads)
    ok = failed = 0

    with tempfile.TemporaryDirectory(prefix="intuition_backfill_") as tmp:
        for i, v in enumerate(todo, 1):
            print("\n[{}/{}] {}".format(i, len(todo), v["name"]), flush=True)
            local = os.path.join(tmp, v["name"])
            try:
                print("      downloading...", flush=True)
                with open(local, "wb") as fh:
                    dl = MediaIoBaseDownload(
                        fh, svc.files().get_media(fileId=v["id"]),
                        chunksize=8 * 1024 * 1024)
                    done = False
                    while not done:
                        _status, done = dl.next_chunk()

                t0 = time.time()
                paths = engine.transcribe(local, progress=_ticker())
                print("      transcribed in {}".format(human_mins(time.time() - t0)),
                      flush=True)

                mirror = drive.DriveMirror(svc, root_folder=root_folder)
                for kind in ("vtt", "txt"):
                    mirror.upload(paths[kind], v["parent"])
                    print("      uploaded {}".format(os.path.basename(paths[kind])),
                          flush=True)
                ok += 1
            except Exception as e:  # noqa: BLE001 - one bad video must not end the run
                print("      FAILED: {}".format(e), flush=True)
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
        description="Generate Whisper transcripts for NTULearn lecture media")
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
