"""Verify (or refresh) vendored copies of claude_bridge.py in sibling projects.

    python tools/check_vendored.py          # report drift
    python tools/check_vendored.py --sync   # overwrite copies from the source

A copy exists when a project runs standalone and cannot import this package. A
copy that drifts is how a security flag gets quietly dropped from one caller, so
the test suite checks this whenever a sibling is present.

``VENDOR_TARGETS`` is currently empty - every caller imports the source directly -
so this reports nothing until a target is added back.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(HERE, "intuition", "claude_bridge.py")
HEADER = ("# VENDORED COPY - do not edit here.\n"
          "# Source: iNTUition/intuition/claude_bridge.py\n"
          "# Edit the source, then re-run its tools/check_vendored.py to sync.\n")


def targets():
    from intuition import claude_bridge
    parent = os.path.dirname(HERE)
    return [os.path.join(parent, rel.replace("/", os.sep))
            for rel in claude_bridge.VENDOR_TARGETS]


def expected() -> str:
    return HEADER + io.open(SOURCE, encoding="utf-8").read()


def check(sync: bool = False):
    drifted, missing = [], []
    for path in targets():
        if not os.path.exists(path):
            missing.append(path)
            continue
        current = io.open(path, encoding="utf-8").read()
        if current == expected():
            continue
        if sync:
            io.open(path, "w", encoding="utf-8", newline="\n").write(expected())
        else:
            drifted.append(path)
    return drifted, missing


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    drifted, missing = check("--sync" in sys.argv)
    for p in missing:
        print("absent (skipped): {}".format(p))
    for p in drifted:
        print("DRIFTED: {}".format(p))
    from intuition import claude_bridge
    if not claude_bridge.VENDOR_TARGETS:
        print("no vendored copies declared")
    else:
        print("synced" if "--sync" in sys.argv else
              ("drift found" if drifted else "vendored copies match"))
    sys.exit(1 if drifted and "--sync" not in sys.argv else 0)
