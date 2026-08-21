# iNTUition

A local-first learning operations dashboard and CLI for NTULearn/Blackboard course material.
It synchronises course content, keeps a searchable local catalogue, and provides optional
schedule, inbox-triage, transcription, Drive, study-lab, and research workflows.

The project is designed to run against a user's own account. It does not ship with an
account, course enrolment, timetable, inbox, downloaded material, credentials, or a
pre-populated dashboard state.

## What is included

- Blackboard/NTULearn course and file synchronisation with incremental downloads.
- A browser dashboard for sync, course selection, local material search, notes, recall,
  task queries, announcements, and study tools.
- Optional schedule import from a timetable PDF, text file, ICS, or CSV.
- Optional local Drive relay, media transcription, inbox triage, and research drafting.
- Profile-driven research drafting without bundled supervisor, faculty, or institutional directory records.
- Desktop packaging helpers for PyInstaller.
- Offline tests use synthetic inputs only; downloaded account exports and institutional reference snapshots are intentionally omitted.

## Requirements

- Python 3.10 or newer is recommended.
- A supported NTULearn/Blackboard account for live synchronisation.
- Optional integrations may require their own credentials, browser session, or external
  service. They are not required for the core downloader and dashboard.

## Install for development

```powershell
git clone <your-repository-url>
cd iNTUition
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

On macOS or Linux, activate the environment with `source .venv/bin/activate` instead.

Install optional feature groups only when needed:

```powershell
python -m pip install -e ".[schedule,drive,transcribe,research,inbound,desktop]"
```

The feature groups are independent; for example, `.[schedule]` is enough to import a
timetable, while `.[desktop]` installs the webview and PyInstaller tooling.

## First run

1. Sign in to NTULearn/Blackboard in a browser.
2. Copy the `BbRouter` session cookie from the browser's developer tools.
3. Start the dashboard and paste the cookie into its local authorisation form:

```powershell
python -m intuition.dashboard --download_to NTU
```

To start the local server without opening a browser automatically:

```powershell
python -m intuition.dashboard --download_to NTU --no_browser
```

The cookie is cached in local state so it does not need to be pasted on every run. The
exact cache location is platform-dependent; all local state and the default `NTU/`
staging directory are ignored by Git. Never copy either into an issue, pull request,
archive, or public repository.

For a non-dashboard sync, the legacy-compatible CLI remains available:

```powershell
python main.py --bbrouter "<BbRouter-cookie>" --download_to NTU
```

Use a real cookie only in a local shell or the dashboard form. Do not put it in a script,
`.env` file that might be committed, test fixture, screenshot, or log.

## Common workflows

```powershell
# Run the dashboard without opening a browser
python -m intuition.dashboard --download_to NTU --no_browser

# Import an optional timetable through the dashboard
# (the dashboard accepts PDF, TXT, ICS, and CSV files)

# Run the test suite
python -m pytest intuition/tests -q

# Build the desktop bundle after installing the desktop extra
python tools/build.py --skip-tests --clean --no-smoke
```

The dashboard and CLI create user-specific material under the selected download root.
That data is intentionally separate from the package and is not suitable for sharing.

## Project structure

The main package is `intuition`:

- `dashboard.py` ? local HTTP dashboard and orchestration layer.
- `api.py`, `rest.py`, `auth.py`, `sync.py`, `contentcache.py`, and `ledger.py` ?
  Blackboard access, authentication, incremental sync, caching, and download state.
- `schedule.py`, `academic_calendar.py`, `semester.py`, `announcements.py`, and
  `inbound.py` ? academic dates, timetable/announcement parsing, and optional inbox triage.
- `drive.py`, `drive_push.py`, and `materials.py` ? local/Drive material discovery and relay.
- `notes.py`, `todo.py`, `triage.py`, `triage_store.py`, and `triage_run.py` ? local study
  notes, task queries, and triage workflows.
- research.py, ureca.py, profile.py, and saved_topics.py - profile-driven research drafting and locally saved topics.
- `lab.py`, `lab_analysis.py`, `chat_memory.py`, `summary.py`, and `compendium.py` ?
  study-lab, AI-assisted study, memory, and document features.
- `desktop.py`, `build_info.py`, `utils.py`, and `__main__.py` ? desktop entry point,
  build metadata, shared helpers, and package execution support.
- `static/` ? dashboard HTML and vendored browser assets.
- `tests/` ? unit and integration tests; they use temporary directories and mocks.

The repository also retains `main.py` as a small compatibility CLI wrapper. New feature
work should generally target the `intuition` package and its dashboard APIs.

## Privacy and repository hygiene

This is a local-first application. Treat the following as private and keep them outside
Git:

- session cookies, access tokens, OAuth files, browser profiles, and `.env` files;
- `NTU/` or another download root containing course files, filenames, IDs, or metadata;
- `.intuition/` state, Drive ledgers, caches, inbox exports, schedules, and research drafts;
- screenshots, PDFs, logs, and debug exports created from a real account.

Before publishing, run a repository scan for email addresses, absolute home paths, account
IDs, cookies, and downloaded filenames. institutional directory records.

## Contributions

Please include focused tests for behavior changes, avoid live account data in fixtures, and
run `python -m pytest intuition/tests -q` before opening a pull request. Do not submit real
credentials, course exports, or screenshots containing student information.

## License

MIT. See [LICENSE](LICENSE).

Repository maintainer: [@chareechard](https://github.com/chareechard)
