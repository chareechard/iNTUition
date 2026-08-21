"""Pick and stage the course materials one research item is allowed to see.

Research is more useful when it knows what the course actually covers - "build a
scheduler simulator" gets a better answer from something that has read the lecture on
scheduling. That means course material leaves the machine, so the selection is narrow
and legible rather than "everything":

* **Scoped to the entry's course tag.** An entry tagged SC2005 can see SC2005 material
  and nothing else. An entry with no tag stages nothing at all.
* **Readable formats only.** Transcripts and documents. Videos and audio are skipped -
  a reader cannot use them and they are enormous.
* **Capped**, by file count and by total bytes, so a research run cannot quietly ship a
  whole semester.
* **Transcripts first.** They are the lecture in words; slides are a distant second.
* **Staged, then deleted.** Copies live in the sandbox for the length of one run.

Files that were moved to Drive are pulled back for the run, since move mode means most
of the term is no longer on disk.
"""
import os
import shutil
from typing import Dict, List, Optional

# What a reader can actually use. Deliberately excludes .mp4/.wav and friends.
TRANSCRIPT_EXT = (".txt", ".vtt", ".srt", ".sbv", ".md")
DOCUMENT_EXT = (".pdf", ".docx", ".pptx", ".doc", ".ppt", ".csv")
READABLE_EXT = TRANSCRIPT_EXT + DOCUMENT_EXT

MAX_FILES = 12
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
STAGE_DIRNAME = "materials"


def _rank(rel_path: str) -> int:
    """Transcripts before documents; within a kind, shorter paths (top level) first."""
    ext = os.path.splitext(rel_path)[1].lower()
    return 0 if ext in TRANSCRIPT_EXT else 1


def matches_course(rel_path: str, course: str) -> bool:
    """Course tags are codes like SC2005; folder names embed them, e.g. 26S1-SC2005-..."""
    course = (course or "").strip().upper()
    if not course:
        return False
    return course in rel_path.upper()


def candidates(course: str, download_root: str, ledger=None) -> List[Dict]:
    """Everything readable for this course, on disk or in Drive, best first.

    A file present both locally and in the ledger is offered once, preferring the local
    copy - no reason to re-download what is already here.
    """
    course = (course or "").strip().upper()
    if not course:
        return []

    found: Dict[str, Dict] = {}

    for dirpath, dirnames, filenames in os.walk(download_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, download_root).replace("\\", "/")
            if not matches_course(rel, course):
                continue
            if os.path.splitext(name)[1].lower() not in READABLE_EXT:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            found[rel] = {"rel": rel, "name": name, "size": size,
                          "local": full, "drive_id": None}

    for rel, record in (getattr(ledger, "entries", None) or {}).items():
        if rel in found or not matches_course(rel, course):
            continue
        if os.path.splitext(rel)[1].lower() not in READABLE_EXT:
            continue
        found[rel] = {"rel": rel, "name": os.path.basename(rel),
                      "size": record.get("size") or 0, "local": None,
                      "drive_id": record.get("drive_id")}

    usable = [f for f in found.values()
              if f["size"] <= MAX_FILE_BYTES and (f["local"] or f["drive_id"])]
    usable.sort(key=lambda f: (_rank(f["rel"]), f["rel"].count("/"), f["rel"].lower()))
    return usable


def select(course: str, download_root: str, ledger=None,
           max_files: int = MAX_FILES,
           max_bytes: int = MAX_TOTAL_BYTES) -> List[Dict]:
    """Apply the caps. Returns at most `max_files` totalling at most `max_bytes`."""
    chosen: List[Dict] = []
    total = 0
    for f in candidates(course, download_root, ledger):
        if len(chosen) >= max_files:
            break
        if total + f["size"] > max_bytes and chosen:
            continue
        chosen.append(f)
        total += f["size"]
    return chosen


def stage_dir(sandbox: str) -> str:
    return os.path.join(sandbox, STAGE_DIRNAME)


def stage(selected: List[Dict], sandbox: str, service=None) -> List[Dict]:
    """Copy or download the selection into <sandbox>/materials/. Returns what landed.

    A file that cannot be fetched is skipped rather than failing the run: partial
    context beats no research, and the finding records what was actually shared.
    """
    dest = stage_dir(sandbox)
    clear(sandbox)
    os.makedirs(dest, exist_ok=True)

    landed: List[Dict] = []
    for f in selected:
        # Flatten: one directory of readable files is easier for a reader to work
        # through than a rebuilt folder tree, and it cannot collide with the sandbox.
        target = os.path.join(dest, _unique(dest, f["name"]))
        try:
            if f.get("local") and os.path.isfile(f["local"]):
                shutil.copy2(f["local"], target)
            elif f.get("drive_id"):
                if service is None:
                    continue
                _download(service, f["drive_id"], target)
            else:
                continue
        except Exception:  # noqa: BLE001 - one unfetchable file must not end the run
            if os.path.exists(target):
                os.remove(target)
            continue
        landed.append({"name": os.path.basename(target), "rel": f["rel"],
                       "size": f["size"],
                       "source": "local" if f.get("local") else "drive"})
    return landed


def _unique(directory: str, name: str) -> str:
    candidate, n = name, 1
    while os.path.exists(os.path.join(directory, candidate)):
        stem, ext = os.path.splitext(name)
        candidate = "{} ({}){}".format(stem, n, ext)
        n += 1
    return candidate


def _download(service, file_id: str, target: str):
    from googleapiclient.http import MediaIoBaseDownload

    with open(target, "wb") as fh:
        dl = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id),
                                 chunksize=4 * 1024 * 1024)
        done = False
        while not done:
            _status, done = dl.next_chunk()


def clear(sandbox: str):
    """Remove staged copies. Called before and after every run."""
    shutil.rmtree(stage_dir(sandbox), ignore_errors=True)


def service_for(download_root: str) -> Optional[object]:
    """A Drive client, or None if Drive was never linked.

    Never interactive: this runs on a dashboard worker thread, where opening a consent
    browser would be both surprising and unanswerable.
    """
    try:
        from intuition import drive
        if not (drive.credentials_present() and drive.token_present()):
            return None
        return drive.build_service(interactive=False)
    except Exception:  # noqa: BLE001 - Drive is optional; research still works without
        return None
