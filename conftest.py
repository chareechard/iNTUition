"""Pytest configuration.

The suite originally relied on nose's ``setup_package``/``teardown_package`` hooks in
``ntu_learn_downloader/tests/__init__.py`` to start the fixture HTTP server. Nose does
not run on Python 3.10+, and pytest ignores those hooks, so the mock server never
started and every networked test failed. This fixture restores that behaviour.
"""
import pytest

from ntu_learn_downloader.tests.mock_server import start_mock_server

MOCK_SERVER_PORT = 8082


@pytest.fixture(scope="session", autouse=True)
def mock_server():
    server = start_mock_server(MOCK_SERVER_PORT)
    yield server
    server.shutdown()
