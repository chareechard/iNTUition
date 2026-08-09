"""Drive mirror tests against an in-memory stand-in for the Drive v3 service.

The real API cannot be exercised without the user's own OAuth client, so this models
the pieces the mirror actually depends on: folder lookup by name+parent, folder
creation, resumable upload, and the size Drive echoes back.
"""
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ntu_learn_downloader import drive
from ntu_learn_downloader.ledger import Ledger

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

    def list(self, q=None, fields=None, pageSize=None, **kw):
        self.list_calls += 1
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

    def update(self, fileId=None, media_body=None, fields=None, **kw):
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
        self.mirror = drive.DriveMirror(self.service, root_folder="NTULearn")
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
        self.assertEqual(names, ["NTULearn", "CE2003", "Tutorials"])

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


class TestDriveHelpers(unittest.TestCase):
    def test_setup_help_names_the_expected_path(self):
        self.assertIn("google_client_secret.json", drive.SETUP_HELP)

    def test_scope_is_narrow(self):
        self.assertEqual(drive.SCOPES, ["https://www.googleapis.com/auth/drive.file"])


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


if __name__ == "__main__":
    unittest.main()
