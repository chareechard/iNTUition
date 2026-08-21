import io
import os
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard, drive


def test_drive_inventory_merges_current_and_legacy_roots():
    listings = {
        "iNTUition": [
            {"id": "new", "rel_path": "SC2001/new.pdf"},
            {"id": "shared", "rel_path": "SC2001/shared.pdf"},
        ],
        "NTULearn": [
            {"id": "old", "rel_path": "MH2100/old.pdf"},
            {"id": "shared", "rel_path": "duplicate/shared.pdf"},
        ],
    }

    class Mirror:
        def __init__(self, _service, root_folder):
            self.root_folder = root_folder

        def list_files(self):
            return listings[self.root_folder]

    notes = []
    state = SimpleNamespace(
        drive_folder=drive.DEFAULT_ROOT_FOLDER,
        drive_files=[],
        drive_listing=True,
        lock=threading.Lock(),
        note=notes.append,
    )
    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "DriveMirror", Mirror):
        dashboard.do_drive_list(state)

    assert [item["id"] for item in state.drive_files] == ["old", "new", "shared"]
    assert state.drive_listing is False
    assert notes == ["Drive inventory: 3 file(s) (iNTUition: 2, NTULearn: 2)"]


def test_custom_drive_root_does_not_merge_legacy_archive():
    seen = []

    class Mirror:
        def __init__(self, _service, root_folder):
            seen.append(root_folder)

        def list_files(self):
            return []

    state = SimpleNamespace(
        drive_folder="My Materials",
        drive_files=[],
        drive_listing=True,
        lock=threading.Lock(),
        note=lambda _message: None,
    )
    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "DriveMirror", Mirror):
        dashboard.do_drive_list(state)

    assert seen == ["My Materials"]


def test_drive_pull_reports_an_expired_token_instead_of_failing_silently():
    """do_drive_list and do_push both catch drive.DriveError around build_service()
    and surface it through state.note(); do_drive_pull used to call build_service()
    unguarded, so the same failure (an expired OAuth token, a revoked grant) escaped
    the worker thread with no note logged and no pulling-state reset visible to the
    user - clicking Fetch looked like it silently did nothing.
    """
    notes = []
    state = SimpleNamespace(
        drive_files=[],
        pulling=True,
        pull_progress={},
        lock=threading.Lock(),
        note=notes.append,
    )
    with patch.object(drive, "build_service",
                       side_effect=drive.DriveError("token expired")):
        dashboard.do_drive_pull(state, ["some-id"])

    assert notes == ["Drive unavailable: token expired"]
    assert state.pulling is False


def test_push_reconciles_a_disk_only_file_back_to_its_plan_entry():
    """collect_files() reports the on-disk relpath, which is backslash-separated on
    Windows; state.plan carries sync.py's logical, forward-slash rel_path. A raw
    string comparison between the two never matches on Windows, silently orphaning
    the push target from its plan entry - the status update after a successful push
    never reaches state.plan (it stays "current" forever, even once the bytes are
    long gone), and the orphaned target carries the disk-derived name into Drive
    instead of the entry's real logical one.
    """
    pushed = []

    def fake_push_file(mirror, entry, download_root, ledger, move, progress):
        pushed.append(entry["rel_path"])
        return {"drive_id": "fake-id"}

    with TemporaryDirectory() as root:
        nested = os.path.join(root, "CE2003", "Long Original Folder Title")
        os.makedirs(nested)
        with open(os.path.join(nested, "notes.pdf"), "wb") as f:
            f.write(b"payload")

        # Status is "new", not PUSHABLE: stage 1 skips this entry outright even
        # though the bytes already exist on disk at exactly this path - a real gap
        # between a scan's classification and a retrieve landing the file, which is
        # exactly what stage 2's disk walk exists to reconcile.
        entry = {"rel_path": "CE2003/Long Original Folder Title/notes.pdf",
                 "status": "new", "path": ""}
        state = SimpleNamespace(
            lock=threading.Lock(), plan=[entry], download_root=root,
            push_progress={}, move=True,
            ledger=SimpleNamespace(save=lambda: None),
            drive_folder="NTULearn", note=lambda _message: None,
        )

        with patch.object(drive, "build_service", return_value=object()), \
             patch.object(drive, "DriveMirror", return_value=object()), \
             patch.object(drive, "push_file", fake_push_file):
            dashboard.do_push(state)

    # The disk file was matched back to the existing plan entry rather than pushed
    # as a disconnected orphan under its raw disk-relpath.
    assert pushed == ["CE2003/Long Original Folder Title/notes.pdf"]
    assert entry["status"] == "archived"
    assert entry["drive_id"] == "fake-id"


def test_drive_content_falls_back_to_drives_own_mime_type_for_unknown_extensions():
    """mimetypes.guess_type() doesn't know source-code extensions like .java or .cpp -
    it returns None for them - which used to fall straight through to
    application/octet-stream. The frontend's material preview picks its <iframe> vs.
    "cannot be rendered" branch off Drive's own item.mime_type (text/x-java), so a
    served Content-Type of application/octet-stream disagreed with what the browser
    was told to expect and the file failed to render inline instead of showing as
    text - this hit every .java file in the OOP course (128 of 325 Drive files).
    """
    with TemporaryDirectory() as tmp:
        java_path = os.path.join(tmp, "Hello.java")
        with open(java_path, "w", encoding="utf-8") as f:
            f.write("public class Hello {}")

        item = {"id": "abc123", "mime_type": "text/x-java",
                "rel_path": "SC2002/Hello.java"}
        state = SimpleNamespace(drive_files=[item], lock=threading.Lock(),
                                 note=lambda _message: None)

        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.state = state
        handler.path = "/api/drive/content?id=abc123"
        handler.requestline = "GET {} HTTP/1.1".format(handler.path)
        handler.request_version = "HTTP/1.1"
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.close_connection = False

        with patch.object(drive, "build_service", return_value=object()), \
             patch.object(drive, "pull_file", return_value=java_path):
            handler.do_GET()

    response = handler.wfile.getvalue()
    header_block, _, body = response.partition(b"\r\n\r\n")
    assert b"Content-Type: text/x-java" in header_block
    assert body == b"public class Hello {}"
