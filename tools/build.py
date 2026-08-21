"""One entry point for producing a desktop build you can trust.

    python tools/build.py                 # test, build, verify, smoke
    python tools/build.py --skip-tests    # iterate on packaging alone
    python tools/build.py --no-smoke      # skip launching the executable
    python tools/build.py --clean         # discard build/ and dist/ first

The steps exist in this order because each one can invalidate the next. Tests
first, so packaging never ships a known-broken tree. The freshness verify runs
*after* PyInstaller rather than trusting that it copied what it was told to -
a build that silently kept an old asset is the failure this whole tool exists to
prevent. The smoke step then proves the frozen runtime starts at all, which no
amount of source-tree testing can establish: imports resolve differently once
frozen, and PyInstaller drops what it cannot see.

``--diagnostics`` is used as the smoke test because it exercises the frozen
runtime and exits on its own; launching the shell proper would open a window and
wait for a human to close it.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

SPEC = os.path.join(HERE, "packaging", "intuition.spec")
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "build")
EXE = os.path.join(DIST, "iNTUition", "iNTUition.exe")


def announce(step: str, index: int, total: int) -> float:
    print("\n[{}/{}] {}".format(index, total, step), flush=True)
    return time.time()


def done(started: float) -> None:
    print("      ok ({:.0f}s)".format(time.time() - started), flush=True)


def dist_holders():
    """Processes with a file from dist/ mapped, which will block the rebuild.

    PyInstaller clears its output directory *before* it discovers it cannot write
    there, so a held file does not merely fail the build - it destroys the working
    one you had. The usual culprit is not the app itself: the desktop shell starts
    OmniRoute with no ``cwd`` (``omniroute_provider.ensure_running``), so its Node
    process inherits the bundle directory and keeps VCRUNTIME140.dll mapped long
    after the app is closed.
    """
    if not sys.platform.startswith("win"):
        return []
    script = (
        "$root='{}'; Get-Process | ForEach-Object {{ $p=$_; try {{ "
        "$p.Modules | Where-Object {{ $_.FileName -like ($root + '*') }} | "
        "Select-Object -First 1 | ForEach-Object {{ "
        "'{{0}} (PID {{1}})' -f $p.ProcessName, $p.Id }} }} catch {{}} }}"
    ).format(os.path.join(DIST, "iNTUition").replace("'", "''"))
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []  # Best effort: never block a build on the diagnostic itself.
    return sorted(set(line.strip() for line in result.stdout.splitlines() if line.strip()))


def preflight() -> bool:
    holders = dist_holders()
    if not holders:
        return True
    print("      these processes hold files in dist/ and would break the build:")
    for holder in holders:
        print("        {}".format(holder))
    print("      close them (OmniRoute can be restarted afterwards) and retry")
    return False


def run_tests() -> bool:
    result = subprocess.run([sys.executable, "-m", "pytest",
                             os.path.join("intuition", "tests"), "-q"],
                            cwd=HERE)
    return result.returncode == 0


def run_pyinstaller(clean: bool) -> bool:
    if clean:
        for path in (os.path.join(WORK, "intuition"), os.path.join(DIST, "iNTUition")):
            shutil.rmtree(path, ignore_errors=True)
    result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                             "--clean", "--distpath", DIST, "--workpath", WORK, SPEC],
                            cwd=HERE)
    return result.returncode == 0


def verify_fresh() -> bool:
    from tools import check_build_fresh
    return check_build_fresh.main([]) == 0


def smoke() -> bool:
    """Run the frozen executable end to end without opening a window."""
    if not os.path.exists(EXE):
        print("      no executable to smoke test")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "smoke-diagnostics.zip")
        try:
            result = subprocess.run([EXE, "--diagnostics", target], timeout=180)
        except subprocess.TimeoutExpired:
            print("      executable did not exit within 180s")
            return False
        if result.returncode != 0:
            print("      executable exited {}".format(result.returncode))
            return False
        if not os.path.exists(target):
            print("      executable wrote no diagnostics")
            return False
        with zipfile.ZipFile(target) as archive:
            report = json.loads(archive.read("diagnostics.json"))
        if not report.get("frozen"):
            print("      diagnostics report frozen=False; not a packaged run")
            return False
        print("      {} {} (frozen, python {})".format(
            report.get("app"), report.get("version"), report.get("python")))
    return True


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--no-smoke", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)

    steps = [("Test suite", run_tests)] if not args.skip_tests else []
    steps.append(("Preflight: dist/ not locked", preflight))
    steps.append(("Package (PyInstaller)", lambda: run_pyinstaller(args.clean)))
    steps.append(("Verify bundle matches source", verify_fresh))
    if not args.no_smoke:
        steps.append(("Smoke test frozen executable", smoke))

    for index, (label, step) in enumerate(steps, start=1):
        started = announce(label, index, len(steps))
        if not step():
            print("\nFAILED at step {}/{}: {}".format(index, len(steps), label))
            return 1
        done(started)

    print("\nBuilt {}".format(EXE))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
