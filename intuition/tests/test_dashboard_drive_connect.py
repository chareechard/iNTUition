"""Drive can be connected from the desktop dashboard, and the loopback API auth
does not depend on the WebView2 shell round-tripping a SameSite cookie.
"""
import json
import os

import pytest

from intuition import dashboard, drive


# ── OAuth client JSON upload ────────────────────────────────────────────────
def _desktop_client():
    return json.dumps({"installed": {
        "client_id": "abc.apps.googleusercontent.com", "project_id": "proj",
        "client_secret": "s", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"}})


@pytest.fixture
def drive_config(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(drive, "CLIENT_SECRET_PATH", str(tmp_path / "client.json"))
    monkeypatch.setattr(drive, "TOKEN_PATH", str(tmp_path / "token.json"))
    return tmp_path


def test_save_client_secret_rejects_non_json(drive_config):
    assert drive.save_client_secret("not json")["ok"] is False
    assert not os.path.exists(drive.CLIENT_SECRET_PATH)


def test_save_client_secret_rejects_service_account(drive_config):
    verdict = drive.save_client_secret(json.dumps({"type": "service_account"}))
    assert verdict["ok"] is False
    assert "service" in verdict["problem"].lower()
    assert not os.path.exists(drive.CLIENT_SECRET_PATH)


def test_save_client_secret_rejects_web_client(drive_config):
    verdict = drive.save_client_secret(json.dumps({"web": {"client_id": "x"}}))
    assert verdict["ok"] is False
    assert not os.path.exists(drive.CLIENT_SECRET_PATH)


def test_save_client_secret_stores_a_desktop_client(drive_config):
    verdict = drive.save_client_secret(_desktop_client())
    assert verdict == {"ok": True, "client_id": "abc.apps.googleusercontent.com",
                       "project": "proj"}
    assert drive.credentials_present()
    assert drive.inspect_client_secret()["ok"] is True


def test_disconnect_forgets_the_token_but_keeps_the_client(drive_config):
    drive.save_client_secret(_desktop_client())
    with open(drive.TOKEN_PATH, "w") as handle:
        handle.write("{}")
    assert drive.token_present()
    drive.disconnect()
    assert not drive.token_present()
    assert drive.credentials_present()


# ── loopback auth via the rewritten <meta>, not only the cookie ─────────────
def test_page_carries_the_session_placeholder_and_fetch_header():
    with open(dashboard.PAGE_PATH, encoding="utf-8") as handle:
        html = handle.read()
    assert '<meta name="intuition-session" content="__INTUITION_SESSION__">' in html
    assert "X-iNTUition-Session" in html


def test_authorized_accepts_the_header_without_a_cookie():
    from types import SimpleNamespace

    handler = object.__new__(dashboard.Handler)
    handler.state = SimpleNamespace(local_secret="s3cret")
    handler.headers = {"X-iNTUition-Session": "s3cret"}
    assert dashboard.Handler._authorized(handler)

    handler.headers = {"X-iNTUition-Session": "wrong"}
    assert not dashboard.Handler._authorized(handler)
