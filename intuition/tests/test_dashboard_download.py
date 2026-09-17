import threading
from types import SimpleNamespace
from unittest.mock import patch

from intuition import dashboard


def test_download_reports_current_file_progress_before_completion(tmp_path):
    snapshots = []
    entry = {
        "path": str(tmp_path / "file.pdf"),
        "rel_path": "Course/file.pdf",
        "type": "file",
        "name": "file.pdf",
        "filename": "file.pdf",
        "predownload_link": "link",
        "status": "new",
    }
    state = SimpleNamespace(
        lock=threading.RLock(),
        token="token",
        downloading=True,
        plan=[entry],
        progress={},
        note=lambda _message: None,
        refresh_media=lambda: None,
        session_current=lambda _token, _generation: True,
    )

    def fake_download(_token, _url, _target, callback):
        callback(250, 1000)
        snapshots.append(dict(state.progress))
        callback(1000, 1000)

    with patch.object(dashboard, "get_file_download_link", return_value="url"), \
            patch.object(dashboard, "download", side_effect=fake_download):
        dashboard.do_download(state, [entry["path"]], "token", 1)

    assert snapshots[0]["done"] == 0
    assert snapshots[0]["total"] == 1
    assert snapshots[0]["bytes"] == 250
    assert snapshots[0]["pct"] == 25
    assert state.progress["done"] == 1
    assert state.downloading is False
