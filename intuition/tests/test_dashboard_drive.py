import io
import os
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard, drive


class _FakeNotebook:
    """No-op stand-in for intuition.notes.Notebook - do_drive_list's auto-tag
    pass only needs .get()/.save(), and these fixtures carry no solution-like
    filenames, so .save() is never expected to be called."""

    def get(self, _document_id):
        return None

    def save(self, *args, **kwargs):
        raise AssertionError("no solution-like file in this fixture should be tagged")


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
        notebook=_FakeNotebook(),
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
        notebook=_FakeNotebook(),
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


def test_drive_pull_refreshes_stale_inventory_before_resolving_destination():
    """A stale cache must not turn a nested module file into a root-level pull."""
    item = {
        "id": "mh2500-hand01",
        "name": "Hand01.pdf",
        "rel_path": "26S1-MH2500-PROBABILITY/Tutorials/Hand01.pdf",
        "mime_type": "application/pdf",
        "size": 123,
        "modified": "2026-08-24T00:00:00Z",
    }
    pulled = []
    notes = []
    state = SimpleNamespace(
        drive_folder=drive.DEFAULT_ROOT_FOLDER,
        drive_files=[],  # the browser cache is stale/missing this item
        pulling=True,
        pull_progress={},
        download_root="NTU",
        lock=threading.Lock(),
        note=notes.append,
        refresh_media=lambda: None,
    )

    def fake_pull(_service, selected, _root, progress=None):
        pulled.append(selected["rel_path"])
        return "NTU/26S1-MH2500-PROBABILITY/Tutorials/Hand01.pdf"

    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "list_files_from_roots", return_value=([item], {})), \
         patch.object(drive, "pull_file", side_effect=fake_pull):
        dashboard.do_drive_pull(state, [item["id"]])

    assert pulled == [item["rel_path"]]
    assert state.drive_files == [item]
    assert state.pulling is False


def test_drive_pull_rejects_ids_outside_index_instead_of_dumping_at_root():
    """Unknown metadata is unsafe: a filename is not a valid destination path."""
    notes = []
    state = SimpleNamespace(
        drive_folder=drive.DEFAULT_ROOT_FOLDER,
        drive_files=[],
        pulling=True,
        pull_progress={},
        download_root="NTU",
        lock=threading.Lock(),
        note=notes.append,
        refresh_media=lambda: None,
    )
    with patch.object(drive, "build_service", return_value=object()), \
         patch.object(drive, "list_files_from_roots", return_value=([], {})), \
         patch.object(drive, "pull_file") as pull:
        dashboard.do_drive_pull(state, ["outside-configured-roots"])

    pull.assert_not_called()
    assert any("skipped to protect folder structure" in message for message in notes)
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


def _drive_content_handler(state, path, headers=None):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.state = state
    handler.path = path
    handler.headers = headers if headers is not None else {}
    handler.requestline = "GET {} HTTP/1.1".format(path)
    handler.request_version = "HTTP/1.1"
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.close_connection = False
    return handler


def test_drive_content_renders_docx_as_html_for_the_iframe():
    """A .docx has no native browser renderer and the material drawer loads
    /api/drive/content into an <iframe>; the handler converts it to HTML."""
    with TemporaryDirectory() as tmp:
        docx_path = os.path.join(tmp, "Tut6.docx")
        document = (
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>Solve for x.</w:t></w:r></w:p>'
            '</w:body></w:document>')
        import zipfile
        with zipfile.ZipFile(docx_path, "w") as archive:
            archive.writestr("word/document.xml", document)

        item = {"id": "abc123", "mime_type": drive.DOCX_MIME,
                "rel_path": "SC2001/Tutorial/Tut6.docx", "size": 42}
        state = SimpleNamespace(drive_files=[item], lock=threading.Lock(),
                                 note=lambda _message: None)
        handler = _drive_content_handler(state, "/api/drive/content?id=abc123")

        with patch.object(drive, "build_service", return_value=object()), \
             patch.object(drive, "pull_file", return_value=docx_path):
            handler.do_GET()

    header_block, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
    assert b" 200 " in header_block.split(b"\r\n", 1)[0]
    assert b"Content-Type: text/html; charset=utf-8" in header_block
    assert b"<p>Solve for x.</p>" in body


def test_drive_content_authorizes_via_query_secret_for_iframe_loads():
    """The material drawer loads /api/drive/content as an <iframe> src, which
    carries neither the X-iNTUition-Session header nor (in the packaged WebView2
    shell) the SameSite cookie. openMaterial() appends the secret as ?s=; the
    handler must accept it there or every preview 401s."""
    with TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, "tut1.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 body")

        item = {"id": "abc123", "mime_type": "application/pdf",
                "rel_path": "SC2001/Tutorial/tut1.pdf", "size": 12}
        state = SimpleNamespace(drive_files=[item], lock=threading.Lock(),
                                 note=lambda _message: None,
                                 local_secret="s3cr3t")
        handler = _drive_content_handler(
            state, "/api/drive/content?id=abc123&s=s3cr3t")

        with patch.object(drive, "build_service", return_value=object()), \
             patch.object(drive, "pull_file", return_value=pdf_path):
            handler.do_GET()

    header_block = handler.wfile.getvalue().partition(b"\r\n\r\n")[0]
    assert b" 200 " in header_block.split(b"\r\n", 1)[0]
    assert b"Content-Type: application/pdf" in header_block


def test_drive_content_unauthorized_serves_html_not_json():
    """A missing/wrong secret must still render legibly in the drawer, not as
    Chrome's pretty-printed JSON viewer."""
    item = {"id": "abc123", "mime_type": "application/pdf",
            "rel_path": "SC2001/Tutorial/tut1.pdf", "size": 12}
    state = SimpleNamespace(drive_files=[item], lock=threading.Lock(),
                             note=lambda _message: None, local_secret="s3cr3t")
    handler = _drive_content_handler(state, "/api/drive/content?id=abc123")
    handler.do_GET()

    response = handler.wfile.getvalue()
    header_block, _, body = response.partition(b"\r\n\r\n")
    assert b" 401 " in header_block.split(b"\r\n", 1)[0]
    assert b"Content-Type: text/html; charset=utf-8" in header_block
    assert b"application/json" not in header_block


def test_drive_content_preview_failure_serves_html_not_json():
    """/api/drive/content is loaded straight into the material drawer's <iframe>,
    so a JSON error body renders as Chrome's pretty-printed JSON viewer inside the
    drawer. A failed Drive pull should instead serve a plain styled HTML page the
    drawer can show as a legible reason."""
    item = {"id": "abc123", "mime_type": "application/pdf",
            "rel_path": "SC2001/Tutorial/tut1.pdf", "size": 1024}
    notes = []
    state = SimpleNamespace(drive_files=[item], lock=threading.Lock(),
                             note=notes.append)

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
         patch.object(drive, "pull_file", side_effect=RuntimeError("boom")):
        handler.do_GET()

    response = handler.wfile.getvalue()
    header_block, _, body = response.partition(b"\r\n\r\n")
    assert b" 500 " in header_block.split(b"\r\n", 1)[0]
    assert b"Content-Type: text/html; charset=utf-8" in header_block
    assert b"application/json" not in header_block
    assert b"Google Drive" in body
    assert notes and "Drive preview failed" in notes[0]
