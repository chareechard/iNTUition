"""Where the running copy came from.

The desktop build bundles a snapshot of ``static/``, so a page served by a stale
executable is indistinguishable from a fresh one: it renders perfectly, just not
what you last wrote. The dashboard therefore states its own provenance in the
footer, and ``tools/check_build_fresh.py`` answers the same question offline.

``page_built`` is the modification time of the HTML actually being served - the
moment PyInstaller wrote the snapshot for a frozen run, or the last edit for a
source-tree run. That is the value that settles "am I looking at my change?".
"""
import os
import sys
import time
from typing import Optional

APP_NAME = "iNTUition"
APP_VERSION = "0.3.0-dev"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than the source tree."""
    return bool(getattr(sys, "frozen", False))


def page_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "dashboard.html")


def page_built(path: Optional[str] = None) -> Optional[str]:
    """Local ``YYYY-MM-DD HH:MM`` of the served page, or None if unreadable."""
    try:
        stamp = os.path.getmtime(path or page_path())
    except OSError:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def summary() -> dict:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "frozen": is_frozen(),
        "page_built": page_built(),
    }
