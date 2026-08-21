"""Windows desktop shell for the iNTUition dashboard technical preview."""
import argparse
import ctypes
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from intuition import build_info, dashboard
# Single source of truth: the dashboard footer reports the same identity that
# diagnostics does, so a support zip and a screenshot cannot disagree.
from intuition.build_info import APP_NAME, APP_VERSION


def apply_windows_chrome_theme(*_event_args):
    """Make the native Windows frame match the dashboard's midnight palette."""
    if sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    current_pid = os.getpid()
    handles = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                       ctypes.c_void_p)

    @callback_type
    def collect_window(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == current_pid and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    user32.EnumWindows(collect_window, 0)
    enabled = ctypes.c_int(1)
    # COLORREF stores bytes as 0x00BBGGRR. These mirror dashboard.html's
    # #030712 background and restrained cyan border.
    caption = ctypes.c_uint(0x00120703)
    border = ctypes.c_uint(0x003A2A16)
    for hwnd in handles:
        # Windows 10 builds used attribute 19; current Windows uses 20.
        result = dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        if result:
            dwmapi.DwmSetWindowAttribute(
                hwnd, 19, ctypes.byref(enabled), ctypes.sizeof(enabled))
        dwmapi.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))


def user_paths():
    """Return stable writable locations; never write beside a frozen executable."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    documents = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    root = local / APP_NAME
    return {
        "root": root,
        "data": root / "data",
        "logs": root / "logs",
        "cache": root / "cache",
        "materials": documents / APP_NAME / "Materials",
    }


def prepare_paths():
    paths = user_paths()
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


class Tee:
    """Mirror diagnostic output to the developer stream and rotating log file."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, value):
        if self.stream:
            self.stream.write(value)
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self):
        if self.stream:
            self.stream.flush()
        self.log.flush()


def rotate_logs(log_dir: Path, keep: int = 7):
    logs = sorted(log_dir.glob("desktop-*.log"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    for old in logs[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def available_port() -> int:
    """Ask Windows for an unused loopback port instead of hard-coding 8384."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((dashboard.DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = 180.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError("Dashboard did not become ready: {}".format(last_error or "timeout"))


def diagnostics(destination: Path, error: Optional[str] = None) -> Path:
    """Export sanitized runtime metadata and logs; never include tokens or databases."""
    paths = prepare_paths()
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "frozen": build_info.is_frozen(),
        "page_built": build_info.page_built(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": Path(sys.executable).name,
        "paths": {key: str(value) for key, value in paths.items()},
        "error": error,
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(report, indent=2))
        for log in sorted(paths["logs"].glob("desktop-*.log"))[-7:]:
            archive.write(log, "logs/" + log.name)
    return destination


def run(download_root: Optional[str] = None, browser: bool = False) -> int:
    paths = prepare_paths()
    rotate_logs(paths["logs"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = paths["logs"] / ("desktop-" + stamp + ".log")
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Tee(old_out, log), Tee(old_err, log)
        try:
            material_root = Path(download_root).expanduser().resolve() \
                if download_root else paths["materials"]
            material_root.mkdir(parents=True, exist_ok=True)
            port = available_port()
            url = "http://{}:{}/".format(dashboard.DEFAULT_HOST, port)
            worker = threading.Thread(
                target=dashboard.serve,
                kwargs={"download_root": str(material_root), "port": port,
                        "open_browser": False}, daemon=True, name="intuition-service")
            worker.start()
            wait_until_ready(url)
            print("Desktop shell ready at {}".format(url))

            if browser:
                webbrowser.open(url)
                print("Browser development mode; press Ctrl+C to stop.")
                while worker.is_alive():
                    time.sleep(0.5)
                return 0

            try:
                import webview
            except ImportError as exc:
                raise RuntimeError(
                    "Desktop runtime is missing. Install the 'desktop' extra or use --browser."
                ) from exc
            window = webview.create_window(
                APP_NAME, url, width=1380, height=860, min_size=(960, 640),
                background_color="#030712")
            window.events.shown += apply_windows_chrome_theme
            webview.start(gui="edgechromium", debug=False, private_mode=False)
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - top-level crash boundary
            print("Desktop startup failed: {}".format(exc), file=sys.stderr)
            bundle = diagnostics(paths["root"] / "last-crash-diagnostics.zip", str(exc))
            print("Diagnostics: {}".format(bundle), file=sys.stderr)
            return 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err


def main():
    parser = argparse.ArgumentParser(description="iNTUition desktop technical preview")
    parser.add_argument("--download-to", help="Material download folder")
    parser.add_argument("--browser", action="store_true",
                        help="Use the system browser instead of WebView2")
    parser.add_argument("--diagnostics", metavar="ZIP",
                        help="Export sanitized diagnostics and exit")
    args = parser.parse_args()
    if args.diagnostics:
        print(diagnostics(Path(args.diagnostics)))
        return 0
    return run(args.download_to, args.browser)


if __name__ == "__main__":
    raise SystemExit(main())
