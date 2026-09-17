"""Small, shared persistence primitives for iNTUition's JSON stores.

All long-lived state files are replaced atomically. A process crash can therefore
leave either the old complete document or the new complete document, never a
half-written JSON file that poisons the next dashboard session.
"""

import json
import os
import tempfile
from typing import Any


def atomic_write_text(path: str, text: str) -> None:
    """Write UTF-8 text beside path and atomically replace the destination."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".intuition-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json_dump(path: str, value: Any, *, indent: int = 2) -> None:
    """Serialize JSON and persist it through atomic_write_text."""
    atomic_write_text(path, json.dumps(value, indent=indent))
