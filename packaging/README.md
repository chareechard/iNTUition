# Desktop technical preview

The desktop edition is a thin WebView2 shell around the same loopback dashboard used
during browser development. The backend remains ordinary Python and the web UI remains
directly testable in Chrome.

## Developer run

```powershell
python -m pip install -e ".[desktop]"
python -m intuition.desktop --browser
```

Remove `--browser` to open the WebView2 desktop window.

## Where to test a change

The .exe is a **snapshot**, not a view of your working tree: the spec copies
`intuition/static` in as PyInstaller data and freezes the Python into the
archive. A stale build renders perfectly — just not what you last wrote — so an edit
that "did not apply" is indistinguishable from a broken edit.

Iterate in the browser dashboard, which re-reads `dashboard.html` from disk on every
request (`dashboard.load_page`), and rebuild at checkpoints:

| Change | Where to verify |
| --- | --- |
| UI, CSS, dashboard JS | browser dashboard; refresh is enough |
| Python logic | `pytest`, then the browser dashboard |
| Packaging inputs (spec, requirements, new static or vendored assets) | rebuild |
| Anything reading `sys.frozen`, home-directory paths, subprocesses, Playwright | rebuild |
| New third-party imports | rebuild — PyInstaller silently drops what it cannot see |

The dashboard footer states which copy you are looking at: `Build <timestamp> · source`
or `· packaged`. If it says `packaged` and the timestamp predates your edit, you are
looking at a stale build, not a bug.

## One-directory build

```powershell
python tools\build.py            # test -> package -> verify -> smoke
python tools\build.py --clean    # discard build\ and dist\ first
```

Each step can invalidate the next, hence the order. The verify step re-checks that the
bundle actually matches the working tree rather than trusting that PyInstaller copied
what it was told to; the smoke step runs the frozen executable via `--diagnostics`,
which exercises the packaged runtime and exits without opening a window.

`--skip-tests` and `--no-smoke` narrow the loop while iterating on packaging itself.
`.\packaging\build-desktop.ps1 -Clean` remains as the bare PyInstaller invocation.

To ask whether the current build is current, without rebuilding:

```powershell
python tools\check_build_fresh.py
```

It compares every bundled static asset byte for byte and reports package modules newer
than the executable. Exit `0` fresh, `1` stale, `2` no build present.

**Close the app before building.** A running `iNTUition.exe` holds
`_internal\VCRUNTIME140.dll` open, and PyInstaller fails part-way with `WinError 5`
after it has already cleared the previous output — leaving `dist\iNTUition` incomplete.

The executable is written to `dist\iNTUition\iNTUition.exe`. Keep the entire
`dist\iNTUition` directory together; this is intentionally not a one-file build.

## Diagnostics

```powershell
.\dist\iNTUition\iNTUition.exe --diagnostics .\intuition-diagnostics.zip
```

The archive contains runtime metadata and recent desktop logs. It deliberately excludes
session tokens, OAuth credentials, SQLite databases, snapshots and course materials.

## Release boundaries

- WebView2 Evergreen is required on the target computer.
- OmniRoute remains an external optional companion.
- Whisper and Playwright are excluded from the base build; ship them as separately
  tested feature packs if needed.
- The executable icon is `JARVIS.ico`, embedded by the spec. Replacing the file only
  takes effect on the next build; PyInstaller copies it in at link time.
- Public releases still require an installer, version metadata, code signing,
  upgrade testing and SQLite schema migrations.
