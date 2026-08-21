# Module and structure map

This is the public source layout. Runtime state, downloaded material, caches, build output,
and browser profiles are deliberately outside this map and are ignored by Git.

## Top-level entry points

- `main.py` — compatibility CLI wrapper for authenticated course downloads.
- `intuition/__main__.py` — package execution entry point.
- `intuition/dashboard.py` — local dashboard server and API orchestration.
- `intuition/desktop.py` — optional desktop/webview shell.
- `tools/build.py` — reproducible desktop build helper.
- `packaging/intuition.spec` and `packaging/build-desktop.ps1` — PyInstaller packaging.

## Runtime modules

### Authentication, API, and synchronisation

- `auth.py` — BbRouter/session-token handling.
- `api.py` — Blackboard course and content API compatibility layer.
- `rest.py` — REST client and response helpers.
- `sync.py` — incremental course synchronisation.
- `contentcache.py` — downloaded-content cache.
- `ledger.py` — local file/download ledger.
- `storage.py` — legacy download-tree persistence retained for CLI compatibility.
- `models.py`, `smodels.py` — structured API and serialised tree models.
- `parsing.py` — course-tree and response parsing.
- `semester.py`, `academic_calendar.py` — semester and academic-date helpers.
- `constants.py`, `utils.py`, `build_info.py` — shared constants, utilities, and build metadata.

### Dashboard and study workflows

- `dashboard.py` — HTTP server, dashboard state, and feature orchestration.
- `notes.py` — local notes and recall cards.
- `chat_memory.py` — material-scoped conversation memory.
- `summary.py`, `latex.py`, `compendium.py` — summary and document-generation features.
- `todo.py` — local task/query storage and AI assistance.
- `lab.py`, `lab_runtime.py`, `lab_analysis.py` — code-lab editing, execution, and analysis.
- `materials.py` — material selection and text extraction.

### Academic data and optional integrations

- `schedule.py`, `schedule_import.py` — timetable storage and import.
- `announcements.py` — announcement extraction and dashboard feed.
- `inbound.py`, `owa.py` — optional mailbox access and message normalisation.
- `drive.py`, `drive_push.py`, `drive_dedupe.py` — optional Google Drive indexing, relay,
  and duplicate handling.
- `transcribe.py`, `transcribe_run.py` — optional media discovery and transcription.

### AI, research, and safety boundaries

- `ai_provider.py` — provider selection and common AI interface.
- `omniroute_provider.py` — optional OmniRoute integration.
- `claude_bridge.py` — isolated Claude CLI subprocess boundary shared by research and triage.
- `research.py`, `research_run.py` — research backend setup and provider execution.
- `ureca.py` — URECA proposal drafting and validation.
- `profile.py` — user-supplied research profile storage.
- `saved_topics.py` — locally saved research topics.
- `triage.py`, `triage_run.py`, `triage_store.py` — optional inbox classification, runner,
  and local triage state.

## Data and static assets

- `intuition/static/dashboard.html` — dashboard shell, empty-state markup, and browser logic.
- `intuition/static/vendor/` — vendored browser libraries used by the dashboard.
- `packaging/JARVIS.ico` and `packaging/JARVIS-theme.png` — package artwork.

## Tests and tools

- `intuition/tests/` — unit/integration tests for the runtime modules; tests use mocks and
  temporary directories rather than live account state.
- `tools/check_build_fresh.py` — verifies a desktop bundle matches source freshness.
- `tools/check_vendored.py` — verifies optional vendored security-boundary copies.
- `tools/audit_course_content.py`, `tools/diagnose_course_scope.py`, and
  `tools/backtest_all.py` — local audit/backtest utilities; they require explicit user data
  or live services and are not part of the normal first run.

All imports and package data use the `intuition` package name.