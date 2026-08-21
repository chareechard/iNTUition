# PyInstaller one-directory technical preview. Run from the repository root.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

repo = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = collect_submodules("googleapiclient") + collect_submodules("google_auth_oauthlib")

# Sync's inbound leg reads OWA through Playwright (do_inbound_sync -> triage_run._scan
# -> owa.Mailbox), so excluding it left the packaged app raising SessionExpired on every
# inbound run. There is no PyInstaller hook for playwright: collect_all is what brings
# the Node driver (driver/node.exe and driver/package) along with the Python package.
# The browsers themselves are deliberately not bundled - Playwright resolves those from
# the user's ms-playwright directory at runtime, as it does for the developer install.
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

a = Analysis(
    [os.path.join(repo, "intuition", "desktop.py")],
    pathex=[repo],
    binaries=pw_binaries,
    datas=[
        (os.path.join(repo, "intuition", "static"), "intuition/static"),
    ] + pw_datas,
    hiddenimports=hidden + pw_hidden + ["webview", "webview.platforms.edgechromium"],
    excludes=["pytest", "faster_whisper"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="iNTUition",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=os.path.join(repo, "packaging", "JARVIS.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="iNTUition")
