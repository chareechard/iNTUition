import io
from types import SimpleNamespace

import pytest

from intuition import dashboard


def _handler(secret="expected", headers=None, body=b""):
    handler = object.__new__(dashboard.Handler)
    handler.state = SimpleNamespace(local_secret=secret)
    handler.headers = headers or {}
    handler.rfile = io.BytesIO(body)
    return handler


def test_local_api_requires_the_session_cookie():
    assert not dashboard.Handler._authorized(_handler())
    assert dashboard.Handler._authorized(
        _handler(headers={"Cookie": "intuition_session=expected; theme=dark"})
    )
    assert dashboard.Handler._authorized(
        _handler(headers={"X-iNTUition-Session": "expected"})
    )


def test_json_body_is_bounded_and_requires_json_when_declared():
    handler = _handler(
        headers={"Content-Length": "2", "Content-Type": "application/json"},
        body=b"{}",
    )
    assert dashboard.Handler._body(handler) == {}

    oversized = _handler(
        headers={"Content-Length": str(dashboard.Handler.MAX_JSON_BODY + 1)},
        body=b"",
    )
    with pytest.raises(ValueError, match="too large"):
        dashboard.Handler._body(oversized)

    wrong_type = _handler(
        headers={"Content-Length": "2", "Content-Type": "text/plain"},
        body=b"{}",
    )
    with pytest.raises(ValueError, match="application/json"):
        dashboard.Handler._body(wrong_type)
