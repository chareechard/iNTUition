"""Pytest configuration.

The suite originally relied on nose's ``setup_package``/``teardown_package`` hooks in
``intuition/tests/__init__.py`` to start the fixture HTTP server. Nose does
not run on Python 3.10+, and pytest ignores those hooks, so the mock server never
started and every networked test failed. This fixture restores that behaviour.
"""
import os
import tempfile
import shutil
from pathlib import Path

import pytest

from intuition import drive
from intuition.tests.mock_server import start_mock_server

MOCK_SERVER_PORT = 8082
TEST_TMP_ROOT = Path(__file__).resolve().parent / ".pytest-tmp-runtime"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# The managed Windows environment can create temporary directories under the
# default AppData temp root but cannot reliably clean them up. Keep all test
# scratch state in the writable project workspace instead.
tempfile.tempdir = str(TEST_TMP_ROOT)


def _accessible_mkdtemp(suffix=None, prefix=None, dir=None):
    """Create test directories with inherited workspace ACLs on Windows."""
    suffix = "" if suffix is None else os.fspath(suffix)
    prefix = "tmp" if prefix is None else os.fspath(prefix)
    directory = tempfile.gettempdir() if dir is None else os.fspath(dir)
    candidates = tempfile._get_candidate_names()
    for _ in range(tempfile.TMP_MAX):
        name = prefix + next(candidates) + suffix
        candidate = os.path.join(directory, name)
        try:
            os.mkdir(candidate, 0o777)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not create an accessible temporary directory")


tempfile.mkdtemp = _accessible_mkdtemp

# The Drive push-lock test must not touch the real user profile in the managed
# environment. This is test-only redirection; production keeps its user path.
drive.PUSH_LOCK_PATH = os.path.join(os.fspath(TEST_TMP_ROOT), "push.lock")
try:
    os.remove(drive.PUSH_LOCK_PATH)
except FileNotFoundError:
    pass


@pytest.fixture(scope="session", autouse=True)
def mock_server():
    server = start_mock_server(MOCK_SERVER_PORT)
    yield server
    server.shutdown()


@pytest.fixture
def tmp_path():
    """Use the accessible tempfile factory instead of pytest's ACL-hostile one."""
    path = Path(tempfile.mkdtemp(prefix="pytest-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)