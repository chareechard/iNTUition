# Desktop technical preview

The desktop edition is a thin WebView2 shell around the same loopback dashboard used
during browser development. The backend remains ordinary Python and the web UI remains
directly testable in Chrome.

## Developer run

```powershell
python -m pip install -e ".[desktop]"
python -m ntu_learn_downloader.desktop --browser
```

Remove `--browser` to open the WebView2 desktop window.

## One-directory build

```powershell
.\packaging\build-desktop.ps1 -Clean
```

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
