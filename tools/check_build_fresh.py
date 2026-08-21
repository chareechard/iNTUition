"""Report whether dist/iNTUition still matches the working tree.

    python tools/check_build_fresh.py            # report drift, exit 1 if stale
    python tools/check_build_fresh.py --quiet    # exit code only

The desktop build is a snapshot: ``packaging/intuition.spec`` copies
``intuition/static`` in as PyInstaller data, and the Python modules are
frozen into the archive. Nothing about a stale .exe looks stale - it renders the
page it was built with, so an edit that "did not apply" is indistinguishable from
a bug in the edit. This tells the two apart before you go hunting.

Static assets are compared byte for byte. Python cannot be compared that way once
it is inside the PYZ archive, so the executable's own timestamp is checked against
the newest source module instead - coarser, but it catches the case that matters
(sources edited after the last build).
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(HERE, "intuition")
SOURCE_STATIC = os.path.join(PACKAGE, "static")
DIST = os.path.join(HERE, "dist", "iNTUition")
EXE = os.path.join(DIST, "iNTUition.exe")

# PyInstaller moved bundled data under _internal in 6.x; older layouts put it
# beside the executable. Accept either rather than pinning a version here.
BUNDLE_CANDIDATES = (
    os.path.join(DIST, "_internal", "intuition", "static"),
    os.path.join(DIST, "intuition", "static"),
)


class NoBuild(Exception):
    pass


def bundle_static() -> str:
    for candidate in BUNDLE_CANDIDATES:
        if os.path.isdir(candidate):
            return candidate
    raise NoBuild("no bundled static directory under {}".format(DIST))


def digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def walk(root: str):
    """Relative paths of every file below *root*, using forward slashes."""
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def stale_assets():
    """(missing, differing) static files, relative to the source tree."""
    bundled = bundle_static()
    missing, differing = [], []
    for rel, source_path in walk(SOURCE_STATIC):
        target = os.path.join(bundled, rel.replace("/", os.sep))
        if not os.path.exists(target):
            missing.append(rel)
        elif digest(source_path) != digest(target):
            differing.append(rel)
    return sorted(missing), sorted(differing)


def newer_sources():
    """Package modules modified after the executable was written."""
    if not os.path.exists(EXE):
        raise NoBuild("no executable at {}".format(EXE))
    built = os.path.getmtime(EXE)
    newer = []
    for base, dirs, files in os.walk(PACKAGE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests", "static")]
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(base, name)
            if os.path.getmtime(full) > built:
                newer.append(os.path.relpath(full, HERE).replace(os.sep, "/"))
    return sorted(newer)


def check():
    missing, differing = stale_assets()
    return missing, differing, newer_sources()


def main(argv) -> int:
    quiet = "--quiet" in argv
    try:
        missing, differing, newer = check()
    except NoBuild as exc:
        if not quiet:
            print("no build to check: {}".format(exc))
            print("run: powershell -File packaging/build-desktop.ps1")
        return 2

    stale = missing or differing or newer
    if not quiet:
        for rel in differing:
            print("STALE asset:   static/{}".format(rel))
        for rel in missing:
            print("MISSING asset: static/{}".format(rel))
        for rel in newer:
            print("NEWER source:  {}".format(rel))
        if stale:
            print("\ndist/iNTUition is out of date - rebuild before trusting the .exe:")
            print("  powershell -File packaging/build-desktop.ps1")
        else:
            print("dist/iNTUition matches the working tree")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
