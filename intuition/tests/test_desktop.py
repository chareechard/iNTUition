import json
import zipfile
from pathlib import Path
from unittest.mock import patch

from intuition import desktop


def test_readiness_uses_lightweight_health_endpoint(monkeypatch):
    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def open_url(url, timeout):
        seen.append((url, timeout))
        return Response()

    monkeypatch.setattr(desktop.urllib.request, "urlopen", open_url)
    desktop.wait_until_ready("http://127.0.0.1:1234/", timeout=0.1)
    assert seen == [("http://127.0.0.1:1234/api/health", 1)]


def test_available_port_is_ephemeral_and_valid():
    assert 0 < desktop.available_port() < 65536


def test_user_paths_are_outside_the_executable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    paths = desktop.user_paths()
    assert paths["root"] == tmp_path / "local" / "iNTUition"
    assert paths["materials"] == tmp_path / "user" / "Documents" / "iNTUition" / "Materials"


def test_diagnostics_contains_no_databases_or_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    paths = desktop.prepare_paths()
    (paths["logs"] / "desktop-test.log").write_text("safe log", encoding="utf-8")
    (paths["data"] / "chat_memory.sqlite3").write_bytes(b"private")
    target = desktop.diagnostics(tmp_path / "diagnostics.zip")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        report = json.loads(archive.read("diagnostics.json"))
    assert names == ["diagnostics.json", "logs/desktop-test.log"]
    assert "token" not in json.dumps(report).lower()
    assert not any(name.endswith(".sqlite3") for name in names)
