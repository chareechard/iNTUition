"""Drive mirror tests against an in-memory stand-in for the Drive v3 service.

The real API cannot be exercised without the user's own OAuth client, so this models
the pieces the mirror actually depends on: folder lookup by name+parent, folder
creation, resumable upload, and the size Drive echoes back.
"""
import os
import time
import unittest
import zipfile
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from intuition import drive
from intuition.ledger import Ledger

FOLDER_MIME = drive.FOLDER_MIME


class FakeRequest:
    def __init__(self, result, chunks=1):
        self._result = result
        self._chunks = chunks
        self._served = 0

    def execute(self):
        return self._result

    def next_chunk(self):
        self._served += 1
        if self._served >= self._chunks:
            return None, self._result
        return FakeStatus(self._served / self._chunks), None


class FakeStatus:
    def __init__(self, fraction):
        self._f = fraction

    def progress(self):
        return self._f


class FakeFiles:
    def __init__(self, store):
        self.store = store          # id -> {name, parents, mimeType, size}
        self.created_folders = []
        self.uploads = []
        self.list_calls = 0
        self._next = 1000

    def _new_id(self):
        self._next += 1
        return "id{}".format(self._next)

    def list(self, q=None, fields=None, pageSize=None, pageToken=None, **kw):
        self.list_calls += 1
        if not q or "name = '" not in q:
            # The raw account-wide listing used by _walk_tree() (list_files(),
            # find_duplicate_files()) - single page, no name/parent filter.
            files = [
                {"id": fid, "name": f["name"], "mimeType": f["mimeType"],
                 "size": f.get("size"), "modifiedTime": f.get("modifiedTime"),
                 "parents": f["parents"]}
                for fid, f in self.store.items()
            ]
            return FakeRequest({"files": files})
        # Parse just enough of the Drive query language for these tests.
        name = q.split("name = '")[1].split("'")[0]
        parent = q.split("' in parents")[0].rsplit("'", 1)[-1]
        want_folder = "mimeType = '{}'".format(FOLDER_MIME) in q
        hits = [
            {"id": fid, "name": f["name"], "size": f.get("size")}
            for fid, f in self.store.items()
            if f["name"] == name
            and parent in f["parents"]
            and ((f["mimeType"] == FOLDER_MIME) == want_folder)
        ]
        return FakeRequest({"files": hits})

    def create(self, body=None, media_body=None, fields=None, **kw):
        fid = self._new_id()
        if media_body is None:
            self.store[fid] = {
                "name": body["name"], "parents": body["parents"], "mimeType": FOLDER_MIME,
            }
            self.created_folders.append((body["name"], body["parents"][0]))
            return FakeRequest({"id": fid})
        size = os.path.getsize(media_body.path)
        self.store[fid] = {
            "name": body["name"], "parents": body["parents"],
            "mimeType": "application/octet-stream", "size": size,
        }
        self.uploads.append((body["name"], body["parents"][0]))
        return FakeRequest({"id": fid, "name": body["name"], "size": size}, chunks=3)

    def update(self, fileId=None, media_body=None, body=None, fields=None, **kw):
        if media_body is None:
            # Metadata-only update, e.g. trash_files()'s {"trashed": True}.
            self.store[fileId].update(body or {})
            return FakeRequest({"id": fileId, "name": self.store[fileId]["name"]})
        size = os.path.getsize(media_body.path)
        self.store[fileId]["size"] = size
        self.uploads.append((self.store[fileId]["name"], "update"))
        return FakeRequest(
            {"id": fileId, "name": self.store[fileId]["name"], "size": size}, chunks=2
        )


class FakeService:
    def __init__(self):
        self._files = FakeFiles({})

    def files(self):
        return self._files


class FakeMedia:
    """Stands in for MediaFileUpload; only `path` is used by the fake service."""

    def __init__(self, path, resumable=False, chunksize=None):
        self.path = path


def make_file(root, rel, content=b"payload"):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(content)
    return full


class TestDriveMirror(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.mirror = drive.DriveMirror(self.service, root_folder="iNTUition")
        # push_file/upload import MediaFileUpload lazily; swap it for the fake.
        self.patches = [
            patch.object(drive, "_require_libs", lambda: None),
            patch.dict(
                "sys.modules",
                {"googleapiclient.http": type("m", (), {"MediaFileUpload": FakeMedia})},
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_ensure_path_creates_each_level_once(self):
        a = self.mirror.ensure_path(["CE2003", "Tutorials"])
        b = self.mirror.ensure_path(["CE2003", "Tutorials"])
        self.assertEqual(a, b)
        names = [n for n, _ in self.service._files.created_folders]
        self.assertEqual(names, ["iNTUition", "CE2003", "Tutorials"])

    def test_folder_cache_avoids_repeat_lookups(self):
        self.mirror.ensure_path(["CE2003", "Tutorials"])
        calls_after_first = self.service._files.list_calls
        for _ in range(5):
            self.mirror.ensure_path(["CE2003", "Tutorials"])
        self.assertEqual(self.service._files.list_calls, calls_after_first)

    def test_sibling_folders_do_not_collide(self):
        one = self.mirror.ensure_path(["CE2003", "Week 1"])
        two = self.mirror.ensure_path(["CE2006", "Week 1"])
        self.assertNotEqual(one, two)

    def test_push_file_uploads_records_and_moves(self):
        with TemporaryDirectory() as root:
            path = make_file(root, os.path.join("CE2003", "Tutorials", "Tut1.pdf"))
            ledger = Ledger(root)
            entry = {"path": path, "modified": "2026-08-06T12:00:00Z"}

            seen = []
            result = drive.push_file(
                self.mirror, entry, root, ledger, move=True,
                progress=lambda f: seen.append(f),
            )

            self.assertTrue(result["moved"])
            self.assertFalse(os.path.exists(path), "local copy should be reclaimed")
            rec = ledger.get(os.path.join("CE2003", "Tutorials", "Tut1.pdf"))
            self.assertEqual(rec["drive_id"], result["drive_id"])
            self.assertEqual(rec["remote_modified"], "2026-08-06T12:00:00Z")
            self.assertTrue(seen, "resumable progress should be reported")

    def test_push_file_keys_the_ledger_on_the_logical_path(self):
        """The plan's rel_path is the key, so the record survives the root moving.

        Drive itself also gets the logical name, not the shortened on-disk one: Drive
        has no MAX_PATH to work around, and a name truncated for Windows is a name a
        student's search can no longer match against.
        """
        with TemporaryDirectory() as root:
            path = make_file(root, os.path.join("CE2003", "Shortened~ab12cd34", "a.pdf"))
            ledger = Ledger(root)
            entry = {
                "path": path,
                "rel_path": "CE2003/A Very Long Original Folder Title/a.pdf",
                "modified": "2026-08-06T12:00:00Z",
            }

            drive.push_file(self.mirror, entry, root, ledger, move=False)

            self.assertIsNotNone(
                ledger.get("CE2003/A Very Long Original Folder Title/a.pdf")
            )
            self.assertIsNone(ledger.get(os.path.join("CE2003", "Shortened~ab12cd34",
                                                      "a.pdf")))
            folder_names = [n for n, _ in self.service._files.created_folders]
            self.assertIn("A Very Long Original Folder Title", folder_names)
            self.assertNotIn("Shortened~ab12cd34", folder_names)
            uploaded_names = [n for n, _ in self.service._files.uploads]
            self.assertEqual(uploaded_names, ["a.pdf"])

    def test_push_file_uses_the_logical_filename_when_the_disk_name_was_shortened(self):
        """bounded_filename() truncates+hashes long attachment names, not just folders."""
        with TemporaryDirectory() as root:
            path = make_file(root, os.path.join("CE2003", "Long-attachm-9f8e7d6c.pdf"))
            ledger = Ledger(root)
            entry = {
                "path": path,
                "rel_path": "CE2003/Long attachment name from the original Learn item.pdf",
                "modified": "2026-08-06T12:00:00Z",
            }

            drive.push_file(self.mirror, entry, root, ledger, move=False)

            uploaded_names = [n for n, _ in self.service._files.uploads]
            self.assertEqual(
                uploaded_names,
                ["Long attachment name from the original Learn item.pdf"])

    def test_push_file_falls_back_to_the_disk_path_without_a_rel_path(self):
        """The CLI push walks the disk and has no plan entry to supply one."""
        with TemporaryDirectory() as root:
            path = make_file(root, os.path.join("CE2003", "a.pdf"))
            ledger = Ledger(root)
            drive.push_file(self.mirror, {"path": path}, root, ledger, move=False)
            self.assertIsNotNone(ledger.get("CE2003/a.pdf"))

    def test_push_file_keeps_local_when_not_moving(self):
        with TemporaryDirectory() as root:
            path = make_file(root, "CE2003/a.pdf")
            ledger = Ledger(root)
            drive.push_file(self.mirror, {"path": path}, root, ledger, move=False)
            self.assertTrue(os.path.exists(path))

    def test_size_mismatch_keeps_local_and_raises(self):
        with TemporaryDirectory() as root:
            path = make_file(root, "CE2003/a.pdf", b"12345")
            ledger = Ledger(root)

            # Drive reports a different size than what is on disk.
            real_upload = self.mirror.upload
            self.mirror.upload = lambda *a, **k: dict(real_upload(*a, **k), size=999)

            with self.assertRaises(drive.DriveError):
                drive.push_file(self.mirror, {"path": path}, root, ledger)
            self.assertTrue(os.path.exists(path), "must not delete on mismatch")
            self.assertIsNone(ledger.get("CE2003/a.pdf"))

    def test_missing_local_file_raises(self):
        with TemporaryDirectory() as root:
            with self.assertRaises(drive.DriveError):
                drive.push_file(
                    self.mirror, {"path": os.path.join(root, "nope.pdf")},
                    root, Ledger(root),
                )

    def test_reupload_replaces_instead_of_duplicating(self):
        with TemporaryDirectory() as root:
            ledger = Ledger(root)
            p1 = make_file(root, "CE2003/a.pdf", b"one")
            drive.push_file(self.mirror, {"path": p1}, root, ledger, move=False)
            p2 = make_file(root, "CE2003/a.pdf", b"one")
            drive.push_file(self.mirror, {"path": p2}, root, ledger, move=False)
            named = [f for f in self.service._files.store.values() if f["name"] == "a.pdf"]
            self.assertEqual(len(named), 1, "should update, not create a duplicate")

    def test_move_prunes_emptied_directories(self):
        with TemporaryDirectory() as root:
            path = make_file(root, os.path.join("CE2003", "Tutorials", "only.pdf"))
            drive.push_file(self.mirror, {"path": path}, root, Ledger(root), move=True)
            self.assertFalse(os.path.isdir(os.path.join(root, "CE2003", "Tutorials")))
            self.assertFalse(os.path.isdir(os.path.join(root, "CE2003")))
            self.assertTrue(os.path.isdir(root), "must never prune the root itself")

    def test_prune_stops_at_non_empty_directory(self):
        with TemporaryDirectory() as root:
            keep = make_file(root, os.path.join("CE2003", "keep.pdf"))
            path = make_file(root, os.path.join("CE2003", "Tutorials", "only.pdf"))
            drive.push_file(self.mirror, {"path": path}, root, Ledger(root), move=True)
            self.assertFalse(os.path.isdir(os.path.join(root, "CE2003", "Tutorials")))
            self.assertTrue(os.path.exists(keep))


class TestDuplicateFiles(unittest.TestCase):
    """Two pushes racing the same not-yet-indexed folder can each miss the other's
    fresh upload and both create - this is how the fallout is found and cleaned up.
    """

    def setUp(self):
        self.service = FakeService()
        self.mirror = drive.DriveMirror(self.service, root_folder="iNTUition")
        self.root_id = self.mirror.root_id()

    def _add_file(self, name, parent_id, size=100, modified="2026-01-01T00:00:00Z"):
        fid = self.service._files._new_id()
        self.service._files.store[fid] = {
            "name": name, "parents": [parent_id], "mimeType": "application/pdf",
            "size": size, "modifiedTime": modified,
        }
        return fid

    def _add_folder(self, name, parent_id):
        fid = self.service._files._new_id()
        self.service._files.store[fid] = {"name": name, "parents": [parent_id],
                                          "mimeType": FOLDER_MIME}
        return fid

    def test_no_duplicates_returns_empty(self):
        self._add_file("a.pdf", self.root_id)
        self.assertEqual(drive.find_duplicate_files(self.service, "iNTUition"), [])

    def test_duplicate_file_picks_the_most_recent_by_default(self):
        older = self._add_file("a.pdf", self.root_id, modified="2026-01-01T00:00:00Z")
        newer = self._add_file("a.pdf", self.root_id, modified="2026-02-01T00:00:00Z")
        clusters = drive.find_duplicate_files(self.service, "iNTUition")
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["type"], "file")
        self.assertEqual(cluster["rel_path"], "a.pdf")
        self.assertEqual(cluster["keep"], newer)
        self.assertEqual(cluster["trash"], [older])

    def test_ledgers_recorded_copy_wins_over_recency(self):
        """The ledger, notes and chat memory already point at one id for this path -
        trashing it (just because a newer duplicate exists) would orphan those."""
        newer = self._add_file("a.pdf", self.root_id, modified="2026-02-01T00:00:00Z")
        older = self._add_file("a.pdf", self.root_id, modified="2026-01-01T00:00:00Z")
        with TemporaryDirectory() as root:
            ledger = Ledger(root)
            ledger.record("a.pdf", drive_id=older, remote_modified=None, size=100)
            clusters = drive.find_duplicate_files(self.service, "iNTUition", ledger=ledger)
        self.assertEqual(clusters[0]["keep"], older)
        self.assertEqual(clusters[0]["trash"], [newer])

    def test_nested_duplicate_reports_the_full_rel_path(self):
        sub = self._add_folder("Tutorials", self.root_id)
        a = self._add_file("tut1.pdf", sub)
        b = self._add_file("tut1.pdf", sub)
        clusters = drive.find_duplicate_files(self.service, "iNTUition")
        self.assertEqual(clusters[0]["rel_path"], "Tutorials/tut1.pdf")
        self.assertEqual(set(clusters[0]["trash"] + [clusters[0]["keep"]]), {a, b})

    def test_duplicate_folders_are_reported_not_resolved(self):
        one = self._add_folder("Tutorials", self.root_id)
        two = self._add_folder("Tutorials", self.root_id)
        clusters = drive.find_duplicate_files(self.service, "iNTUition")
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["type"], "folder")
        self.assertEqual(set(clusters[0]["ids"]), {one, two})
        self.assertNotIn("trash", clusters[0])

    def test_cross_root_duplicate_is_found_by_rel_path(self):
        """A renamed app's old material sits un-migrated under its old root (see
        LEGACY_ROOT_FOLDERS); do_drive_list already merges the listing, so the same
        course material pushed again after the rename has to be caught here too."""
        legacy_root_id = drive.DriveMirror(self.service, root_folder="NTULearn").root_id()
        old = self._add_file("a.pdf", legacy_root_id, modified="2026-01-01T00:00:00Z")
        new = self._add_file("a.pdf", self.root_id, modified="2026-06-01T00:00:00Z")
        clusters = drive.find_duplicate_files(
            self.service, "iNTUition", legacy_roots=("NTULearn",))
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["type"], "file")
        self.assertEqual(clusters[0]["rel_path"], "a.pdf")
        self.assertEqual(clusters[0]["keep"], new)
        self.assertEqual(clusters[0]["trash"], [old])

    def test_without_legacy_roots_cross_root_files_are_not_flagged(self):
        legacy_root_id = drive.DriveMirror(self.service, root_folder="NTULearn").root_id()
        self._add_file("a.pdf", legacy_root_id)
        self._add_file("a.pdf", self.root_id)
        self.assertEqual(drive.find_duplicate_files(self.service, "iNTUition"), [])

    def test_trash_files_marks_trashed_without_deleting(self):
        a = self._add_file("a.pdf", self.root_id)
        drive.trash_files(self.service, [a])
        self.assertTrue(self.service._files.store[a]["trashed"])
        self.assertIn(a, self.service._files.store, "must not be hard-deleted")


class TestGetCredentials(unittest.TestCase):
    """A revoked/expired refresh token (invalid_grant) must not linger as a dead
    token_present()==True - that is exactly what let Drive read as "linked" in the
    dashboard while every real call kept failing behind it.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_path = os.path.join(self.tmp.name, "google_token.json")
        with open(self.token_path, "w") as f:
            f.write("{}")
        self.patches = [
            patch.object(drive, "CONFIG_DIR", self.tmp.name),
            patch.object(drive, "TOKEN_PATH", self.token_path),
            patch.object(drive, "_require_libs", lambda: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _stub_google_modules(self, creds):
        class RefreshError(Exception):
            pass

        credentials_cls = type("Credentials", (), {
            "from_authorized_user_file": staticmethod(lambda path, scopes: creds),
        })
        modules = {
            "google.auth.exceptions": type("m", (), {"RefreshError": RefreshError}),
            "google.auth.transport.requests": type("m", (), {"Request": object}),
            "google.oauth2.credentials": type("m", (), {"Credentials": credentials_cls}),
            "google_auth_oauthlib.flow": type("m", (), {"InstalledAppFlow": object}),
        }
        patcher = patch.dict("sys.modules", modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        return RefreshError

    def test_a_revoked_refresh_token_is_dropped_and_reported(self):
        creds = MagicMock(valid=False, expired=True, refresh_token="r")
        RefreshError = self._stub_google_modules(creds)
        creds.refresh.side_effect = RefreshError("invalid_grant")

        with self.assertRaises(drive.DriveError) as ctx:
            drive.get_credentials(interactive=False)

        self.assertIn("expired or been revoked", str(ctx.exception))
        self.assertFalse(
            os.path.exists(self.token_path),
            "a dead token must not keep token_present() reporting Drive as linked")

    def test_a_working_refresh_is_saved_and_returned(self):
        creds = MagicMock(valid=False, expired=True, refresh_token="r")
        creds.to_json.return_value = "{}"
        self._stub_google_modules(creds)

        result = drive.get_credentials(interactive=False)

        self.assertIs(result, creds)
        self.assertTrue(os.path.exists(self.token_path))


class TestPushLock(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = os.path.join(self.tmp.name, "push.lock")
        self.patches = [
            patch.object(drive, "CONFIG_DIR", self.tmp.name),
            patch.object(drive, "PUSH_LOCK_PATH", self.lock_path),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_lock_file_exists_only_while_held(self):
        with drive.push_lock():
            self.assertTrue(os.path.exists(self.lock_path))
        self.assertFalse(os.path.exists(self.lock_path))

    def test_lock_is_released_even_if_the_body_raises(self):
        with self.assertRaises(ValueError):
            with drive.push_lock():
                raise ValueError("boom")
        self.assertFalse(os.path.exists(self.lock_path))

    def test_a_held_lock_blocks_a_second_acquire(self):
        with drive.push_lock():
            # Skip the real 5s retry window: the second call's deadline check just
            # needs to see time already past it.
            with patch.object(drive.time, "monotonic", side_effect=[0.0, 10.0]):
                with self.assertRaises(drive.PushLockError):
                    with drive.push_lock():
                        pass

    def test_a_stale_lock_is_reclaimed(self):
        with open(self.lock_path, "w") as f:
            f.write("99999")
        stale = time.time() - drive.PUSH_LOCK_STALE_SECONDS - 1
        os.utime(self.lock_path, (stale, stale))
        with drive.push_lock():
            self.assertTrue(os.path.exists(self.lock_path))
            self.assertTrue(time.time() - os.path.getmtime(self.lock_path) < 5,
                            "reclaiming must replace the stale lock, not reuse its age")


class TestDriveHelpers(unittest.TestCase):
    def test_setup_help_names_the_expected_path(self):
        self.assertIn("google_client_secret.json", drive.SETUP_HELP)

    def test_scope_is_narrow(self):
        self.assertEqual(drive.SCOPES, ["https://www.googleapis.com/auth/drive.file"])

    def test_pull_rejects_path_outside_staging(self):
        with TemporaryDirectory() as root, patch.object(drive, "_require_libs", lambda: None):
            with self.assertRaises(drive.DriveError):
                drive.pull_file(object(), {"id": "x", "rel_path": "../escape.pdf"}, root)

    def test_pull_downloads_then_replaces_target(self):
        payload = b"from drive"

        class Files:
            def get_media(self, **kwargs):
                return payload

        class Service:
            def files(self):
                return Files()

        class Downloader:
            def __init__(self, stream, request, chunksize=None):
                self.stream, self.request = stream, request

            def next_chunk(self):
                self.stream.write(self.request)
                return FakeStatus(1), True

        module = type("m", (), {"MediaIoBaseDownload": Downloader})
        with TemporaryDirectory() as root, patch.object(drive, "_require_libs", lambda: None), \
                patch.dict("sys.modules", {"googleapiclient.http": module}):
            target = drive.pull_file(
                Service(), {"id": "f1", "rel_path": "CE2003/Tut.pdf",
                            "size": len(payload)}, root)
            with open(target, "rb") as stream:
                self.assertEqual(stream.read(), payload)

    def test_pull_shortens_a_path_that_would_blow_past_windows_max_path(self):
        """A Drive-mirrored rel_path carries no length limit, unlike the on-disk
        paths sync.py produces for the original download - restoring a deeply
        nested course archive (a folder-per-year exam bank six segments deep) used
        to rejoin the full logical path under the download root, blow past
        Windows' 260-char MAX_PATH, and fail with ENOENT before a single byte was
        written.
        """
        payload = b"midterm pdf bytes"

        class Files:
            def get_media(self, **kwargs):
                return payload

        class Service:
            def files(self):
                return Files()

        class Downloader:
            def __init__(self, stream, request, chunksize=None):
                self.stream, self.request = stream, request

            def next_chunk(self):
                self.stream.write(self.request)
                return FakeStatus(1), True

        rel_path = (
            "AY2026-2027 Semester 1 MH2100 (Calculus III)/"
            "Past MH2100 (Calculus III) Midterm Exam 1-Midterm Exam 2-Midterm Exam Papers/"
            "Past MH2100 (Calculus III) Midterm Exam 1 Papers/"
            "NTU AY2022-2023 Semester 1 MH2100 (Calculus III) - Midterm Exam 1.pdf"
        )
        module = type("m", (), {"MediaIoBaseDownload": Downloader})
        with TemporaryDirectory() as root, patch.object(drive, "_require_libs", lambda: None), \
                patch.dict("sys.modules", {"googleapiclient.http": module}):
            target = drive.pull_file(
                Service(), {"id": "f1", "rel_path": rel_path, "size": len(payload)}, root)
            self.assertLessEqual(len(target), 259)
            with open(target, "rb") as stream:
                self.assertEqual(stream.read(), payload)

    def test_list_files_builds_relative_paths(self):
        items = [
            {"id": "course", "name": "CE2003", "mimeType": FOLDER_MIME,
             "parents": ["root-id"]},
            {"id": "pdf", "name": "Tut.pdf", "mimeType": "application/pdf",
             "size": "12", "modifiedTime": "2026-08-12T00:00:00Z",
             "parents": ["course"]},
        ]

        class Files:
            def list(self, q=None, **kwargs):
                return FakeRequest({"files": items})

        mirror = drive.DriveMirror(type("S", (), {"files": lambda self: Files()})())
        mirror._root_id = "root-id"
        self.assertEqual(mirror.list_files()[0]["rel_path"], "CE2003/Tut.pdf")

    def test_native_google_doc_is_exported_as_pdf(self):
        payload = b"pdf bytes"

        class Files:
            def export_media(self, fileId=None, mimeType=None):
                self.mime = mimeType
                return payload

        files = Files()

        class Downloader:
            def __init__(self, stream, request, chunksize=None):
                self.stream, self.request = stream, request

            def next_chunk(self):
                self.stream.write(self.request)
                return FakeStatus(1), True

        service = type("S", (), {"files": lambda self: files})()
        module = type("m", (), {"MediaIoBaseDownload": Downloader})
        with TemporaryDirectory() as root, patch.object(drive, "_require_libs", lambda: None), \
                patch.dict("sys.modules", {"googleapiclient.http": module}):
            target = drive.pull_file(service, {
                "id": "doc", "rel_path": "Notes", "size": 0,
                "mime_type": "application/vnd.google-apps.document",
            }, root)
            self.assertTrue(target.endswith("Notes.pdf"))
            self.assertEqual(files.mime, "application/pdf")


class TestInspectClientSecret(unittest.TestCase):
    """The two mistakes people actually make when following the console steps."""

    def _inspect(self, payload):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "client.json")
            with open(path, "w", encoding="utf-8") as f:
                if isinstance(payload, str):
                    f.write(payload)
                else:
                    import json
                    json.dump(payload, f)
            with patch.object(drive, "CLIENT_SECRET_PATH", path):
                return drive.inspect_client_secret()

    def test_desktop_client_accepted(self):
        got = self._inspect(
            {"installed": {"client_id": "abc.apps.googleusercontent.com",
                           "project_id": "my-proj"}}
        )
        self.assertTrue(got["ok"])
        self.assertEqual(got["project"], "my-proj")

    def test_service_account_key_rejected(self):
        got = self._inspect({"type": "service_account", "private_key": "x"})
        self.assertFalse(got["ok"])
        self.assertIn("service-account", got["problem"])

    def test_web_client_rejected(self):
        got = self._inspect({"web": {"client_id": "abc"}})
        self.assertFalse(got["ok"])
        self.assertIn("Desktop app", got["problem"])

    def test_garbage_json_rejected(self):
        got = self._inspect("{not json")
        self.assertFalse(got["ok"])
        self.assertIn("valid JSON", got["problem"])

    def test_missing_file_rejected(self):
        with patch.object(drive, "CLIENT_SECRET_PATH", "/no/such/file.json"):
            got = drive.inspect_client_secret()
        self.assertFalse(got["ok"])


class TestDriveSearchEnhancements(unittest.TestCase):
    def setUp(self):
        self.files = [
            {"id": "f1", "name": "tut4_solution.pdf", "rel_path": "CE2003/Tutorials/tut4_solution.pdf"},
            {"id": "f2", "name": "Lecture_1.pdf", "rel_path": "CZ2006/Lectures/lec01.pdf"},
            {"id": "f3", "name": "Tutorial_Solutions.pdf", "rel_path": "CE2003/Content/Tutorials/Tutorial_Solutions.pdf"},
        ]

    def test_search_compound_tokens(self):
        results = drive.semantic_search(self.files, "ce2003 tut 4")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "f1")

    def test_search_split_course_codes(self):
        results = drive.semantic_search(self.files, "ce 2003 tutorial 04")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "f1")

    def test_search_lecture_tokens(self):
        results = drive.semantic_search(self.files, "cz 2006 lecture 1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "f2")


class TestTreeLevel(unittest.TestCase):
    def setUp(self):
        self.files = [
            {"id": "f1", "name": "slides.pdf", "size": 100, "modified": "2024-01-01T00:00:00Z",
             "rel_path": "CE2003 Software Eng/Lectures/wk1/slides.pdf"},
            {"id": "f2", "name": "notes.pdf", "size": 200, "modified": "2024-02-01T00:00:00Z",
             "rel_path": "CE2003 Software Eng/Lectures/wk2/notes.pdf"},
            {"id": "f3", "name": "tut1.pdf", "size": 50, "modified": "2024-01-15T00:00:00Z",
             "rel_path": "CE2003 Software Eng/Tutorials/tut1.pdf"},
            {"id": "f4", "name": "syllabus.pdf", "size": 10, "modified": "2024-01-01T00:00:00Z",
             "rel_path": "CE2003 Software Eng/syllabus.pdf"},
            {"id": "f5", "name": "lec01.pdf", "size": 30, "modified": "2024-01-01T00:00:00Z",
             "rel_path": "CZ2006 Software Eng/lec01.pdf"},
        ]

    def test_root_level_groups_by_course_folder(self):
        level = drive.tree_level(self.files, "")
        self.assertEqual(level["path"], "")
        self.assertEqual([f["name"] for f in level["folders"]],
                         ["CE2003 Software Eng", "CZ2006 Software Eng"])
        self.assertEqual(level["files"], [])
        ce = level["folders"][0]
        self.assertEqual(ce["count"], 4)
        self.assertEqual(ce["size"], 360)
        self.assertEqual(ce["modified"], "2024-02-01T00:00:00Z")

    def test_one_level_down_mixes_folders_and_files(self):
        level = drive.tree_level(self.files, "CE2003 Software Eng")
        self.assertEqual([f["name"] for f in level["folders"]], ["Lectures", "Tutorials"])
        self.assertEqual([f["name"] for f in level["files"]], ["syllabus.pdf"])

    def test_leaf_level_returns_only_files(self):
        level = drive.tree_level(self.files, "CE2003 Software Eng/Tutorials")
        self.assertEqual(level["folders"], [])
        self.assertEqual([f["id"] for f in level["files"]], ["f3"])

    def test_unmatched_prefix_yields_an_empty_level(self):
        level = drive.tree_level(self.files, "does/not/exist")
        self.assertEqual(level["folders"], [])
        self.assertEqual(level["files"], [])

    def test_leading_and_trailing_slashes_are_ignored(self):
        level = drive.tree_level(self.files, "/CE2003 Software Eng/Tutorials/")
        self.assertEqual([f["id"] for f in level["files"]], ["f3"])


def _fake_pdf_page(text):
    page = MagicMock()
    page.extract_text.return_value = text
    return page


def _write_office_zip(path, entries):
    """``entries``: {internal XML path: plain text to wrap as one text run}."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, "<root><t>{}</t></root>".format(text))


class TestExtractLearningPages(unittest.TestCase):
    """Page-anchored extraction: Compendium cites a page number back to the
    source, so page order and boundaries have to be exactly right."""

    @patch("pypdf.PdfReader")
    def test_pdf_pages_are_numbered_in_order(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page("first page text"),
            _fake_pdf_page("second page text"),
            _fake_pdf_page("third page text"),
        ]
        pages = drive.extract_learning_pages("lecture.pdf")
        self.assertEqual([n for n, _t in pages], [1, 2, 3])
        self.assertEqual(pages[1][1], "second page text")

    @patch("pypdf.PdfReader")
    def test_pdf_blank_pages_are_dropped_but_numbering_survives(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page("content"),
            _fake_pdf_page(""),
            _fake_pdf_page("more content"),
        ]
        pages = drive.extract_learning_pages("lecture.pdf")
        self.assertEqual([n for n, _t in pages], [1, 3])

    @patch("pypdf.PdfReader")
    def test_pdf_extraction_failure_raises_drive_error(self, mock_reader):
        mock_reader.side_effect = RuntimeError("corrupt")
        with self.assertRaises(drive.DriveError):
            drive.extract_learning_pages("lecture.pdf")

    def test_pptx_slides_sort_numerically_not_lexicographically(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deck.pptx")
            # slide10 must not sort before slide2 the way plain string sort would.
            _write_office_zip(path, {
                "ppt/slides/slide1.xml": "opening",
                "ppt/slides/slide2.xml": "middle",
                "ppt/slides/slide10.xml": "closing",
            })
            pages = drive.extract_learning_pages(path)
            self.assertEqual([n for n, _t in pages], [1, 2, 10])
            self.assertEqual([t for _n, t in pages], ["opening", "middle", "closing"])

    def test_docx_has_no_page_boundary_and_reports_page_one(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.docx")
            _write_office_zip(path, {"word/document.xml": "the whole document"})
            pages = drive.extract_learning_pages(path)
            self.assertEqual(pages, [(1, "the whole document")])

    def test_plain_text_reports_page_one(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transcript.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("spoken word transcript")
            pages = drive.extract_learning_pages(path)
            self.assertEqual(pages, [(1, "spoken word transcript")])

    def test_unsupported_extension_raises(self):
        with self.assertRaises(drive.DriveError):
            drive.extract_learning_pages("video.mp4")

    def test_all_blank_pages_raise_no_readable_text(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("   \n\t  ")
            with self.assertRaises(drive.DriveError):
                drive.extract_learning_pages(path)

    @patch("pypdf.PdfReader")
    def test_limit_drops_whole_trailing_pages_rather_than_truncating_each(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page("a" * 10),
            _fake_pdf_page("b" * 10),
            _fake_pdf_page("c" * 10),
        ]
        pages = drive.extract_learning_pages("lecture.pdf", limit=15)
        # Page 1 fits whole (10 chars); page 2 is truncated to the remaining 5;
        # page 3 is dropped entirely rather than appearing empty or clipped to 0.
        self.assertEqual(pages, [(1, "a" * 10), (2, "b" * 5)])

    @patch("pypdf.PdfReader")
    def test_extract_learning_text_joins_pages_with_newlines(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page("first"), _fake_pdf_page("second"),
        ]
        self.assertEqual(drive.extract_learning_text("lecture.pdf"), "first\nsecond")


def _fake_pdf_image(data):
    image = MagicMock()
    image.data = data
    return image


def _fake_pdf_page_with_images(images):
    page = MagicMock()
    page.images = images
    return page


_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20
_JPEG = b"\xff\xd8\xff" + b"x" * 20


class TestExtractLearningFigures(unittest.TestCase):
    """Compendium's raw material for real embedded images - best-effort by design,
    see the docstring on extract_learning_figures for why nothing here ever raises."""

    @patch("pypdf.PdfReader")
    def test_returns_page_accurate_png_and_jpeg_images(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page_with_images([_fake_pdf_image(_PNG)]),
            _fake_pdf_page_with_images([_fake_pdf_image(_JPEG)]),
        ]
        figures = drive.extract_learning_figures("lecture.pdf", min_bytes=10)
        self.assertEqual([(page, ext) for page, _data, ext in figures],
                         [(1, "png"), (2, "jpg")])

    @patch("pypdf.PdfReader")
    def test_images_below_the_size_floor_are_dropped(self, mock_reader):
        """A tiny image is almost always a bullet/icon/logo, not a real figure."""
        mock_reader.return_value.pages = [
            _fake_pdf_page_with_images([_fake_pdf_image(b"\x89PNG\r\n\x1a\n\x00")]),
        ]
        figures = drive.extract_learning_figures("lecture.pdf", min_bytes=1000)
        self.assertEqual(figures, [])

    @patch("pypdf.PdfReader")
    def test_unrecognised_image_formats_are_skipped(self, mock_reader):
        mock_reader.return_value.pages = [
            _fake_pdf_page_with_images([_fake_pdf_image(b"not an image" * 5)]),
        ]
        self.assertEqual(drive.extract_learning_figures("lecture.pdf", min_bytes=10), [])

    @patch("pypdf.PdfReader")
    def test_largest_images_win_and_output_stays_in_page_order(self, mock_reader):
        small = _PNG + b"y" * 5
        large = _PNG + b"y" * 500
        mock_reader.return_value.pages = [
            _fake_pdf_page_with_images([_fake_pdf_image(small)]),
            _fake_pdf_page_with_images([_fake_pdf_image(large)]),
        ]
        figures = drive.extract_learning_figures("lecture.pdf", limit=1, min_bytes=10)
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0][0], 2)  # the larger image's page, not page 1

    @patch("pypdf.PdfReader")
    def test_a_broken_image_stream_does_not_lose_the_rest_of_the_page(self, mock_reader):
        class _BrokenImage:
            @property
            def data(self):
                raise RuntimeError("corrupt image stream")
        mock_reader.return_value.pages = [
            _fake_pdf_page_with_images([_BrokenImage(), _fake_pdf_image(_JPEG)]),
        ]
        figures = drive.extract_learning_figures("lecture.pdf", min_bytes=10)
        self.assertEqual([ext for _p, _d, ext in figures], ["jpg"])

    def test_non_pdf_returns_no_figures(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.docx")
            _write_office_zip(path, {"word/document.xml": "text only"})
            self.assertEqual(drive.extract_learning_figures(path), [])

    @patch("pypdf.PdfReader")
    def test_reader_failure_returns_no_figures_rather_than_raising(self, mock_reader):
        mock_reader.side_effect = RuntimeError("corrupt")
        self.assertEqual(drive.extract_learning_figures("lecture.pdf"), [])


class TestRasterizePages(unittest.TestCase):
    """Compendium's other eye onto a PDF - a rendered page image alongside the
    extracted text, best-effort by design like extract_learning_figures."""

    def _real_pdf(self, page_count: int) -> str:
        import fitz
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "deck.pdf")
        doc = fitz.open()
        for i in range(page_count):
            page = doc.new_page()
            page.insert_text((72, 72), "Slide {}".format(i + 1))
        doc.save(path)
        doc.close()
        return path

    def test_renders_one_png_per_page_in_order(self):
        path = self._real_pdf(3)
        pages = drive.rasterize_pages(path)
        self.assertEqual([n for n, _data in pages], [1, 2, 3])
        for _n, data in pages:
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_stops_at_max_pages(self):
        path = self._real_pdf(5)
        pages = drive.rasterize_pages(path, max_pages=2)
        self.assertEqual([n for n, _data in pages], [1, 2])

    def test_oversized_page_is_shrunk_to_the_dimension_cap(self):
        path = self._real_pdf(1)
        pages = drive.rasterize_pages(path, dpi=400, max_dimension_px=200)
        self.assertEqual(len(pages), 1)
        import fitz
        pixmap = fitz.Pixmap(pages[0][1])
        self.assertLessEqual(max(pixmap.width, pixmap.height), 200)

    def test_non_pdf_returns_no_pages(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.docx")
            _write_office_zip(path, {"word\document.xml": "text only"})
            self.assertEqual(drive.rasterize_pages(path), [])

    def test_missing_pymupdf_degrades_to_no_pages_rather_than_raising(self):
        path = self._real_pdf(1)
        real_import = __import__

        def _no_fitz(name, *args, **kwargs):
            if name == "fitz":
                raise ImportError("no module named fitz")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_no_fitz):
            self.assertEqual(drive.rasterize_pages(path), [])

    def test_corrupt_pdf_returns_no_pages_rather_than_raising(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.pdf")
            with open(path, "wb") as f:
                f.write(b"not a real pdf")
            self.assertEqual(drive.rasterize_pages(path), [])


if __name__ == "__main__":
    unittest.main()

