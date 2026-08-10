# NTU Learn Downloader

Command line interface to downloading files from [NTULearn](http://ntulearn.ntu.edu.sg). See
[NTULearn-Downloader-GUI](https://github.com/leafgecko/NTULearn-Downloader-GUI) if you prefer a graphical user interface (GUI).

## Update (2026)

NTULearn has changed substantially since this tool was written. It has been updated to match:

- **Platform**: NTU now runs Blackboard Learn SaaS (4000.x) with **Ultra base navigation**, not the
  self-hosted Original install this tool originally scraped. Content is now read through the
  Blackboard **public REST API**, which works for both Ultra and Original courses. The old
  `listContent.jsp` HTML scraper is kept as a fallback and can be forced with `--legacy`.
- **Login**: NTU federates to **Microsoft Entra ID** with MFA, not ADFS. Username/password can no
  longer be exchanged for a session non-interactively, so `-username`/`-password` are gone. You log
  in with a browser once and hand the tool the resulting `BbRouter` cookie, which it caches.
- **Recorded lectures**: AcuStudio has been **decommissioned** (its hosts no longer resolve), so
  `--download_recorded_lectures` and the `recorded_lecture` node type are effectively dead.
  In practice this matters less than it sounds: videos uploaded into a course are now ordinary
  `resource/x-bb-file` items (e.g. `video/mp4`) and are downloaded through the **normal file path**
  with no special flag. Only recordings that live in an externally hosted tool (Zoom, Panopto,
  Kaltura, ... surfaced as LTI links) cannot be fetched with a Learn session cookie; those are
  reported as skipped rather than silently dropped.
- Quizzes (`x-bb-asmt-test-link`) and SCORM packages (`x-plugin-scormengine`) have no file behind
  them and are ignored.

## Getting a session token

1. Open <https://ntulearn.ntu.edu.sg/ultra> in your browser and log in as usual.
2. Open DevTools (<kbd>F12</kbd>) → Application (Chrome) or Storage (Firefox) → Cookies →
   `https://ntulearn.ntu.edu.sg`
3. Copy the full value of the `BbRouter` cookie.
4. Pass it with `--bbrouter "<value>"`.

The token is cached at `~/.ntu_learn_downloader/session.json` and reused until it expires (a few
hours), so you only need to repeat this when it goes stale. Use `--no_cache` to disable caching.

## iNTUition dashboard (recommended)

```
python -m ntu_learn_downloader.dashboard --download_to NTU
```

Opens a local HUD at <http://127.0.0.1:8384> that wraps the whole workflow: establish the link with
your token, pick course nodes, **scan** for changes, and retrieve only what you queue.

Layout: a telemetry bar (link state, session countdown, node/queue counts, target folder), a course
rail on the left, the delta manifest on the right, and a console feed underneath. It collapses to a
single column below 900px. The page lives in `ntu_learn_downloader/static/dashboard.html` and is
read per request, so design tweaks show up on refresh without restarting the server.

The scan diffs Blackboard against your local folder and labels every file:

| Status | Meaning |
|---|---|
| `new` | Not on disk yet |
| `updated` | On disk, but Blackboard's `modified` timestamp is newer than your copy |
| `current` | On disk and unchanged — nothing to do |
| `unknown` | Legacy scraper item whose filename only appears in the download redirect |
| `ignored` | A video you previously declined (dummy marker file present) |

`updated` is the part plain re-running the CLI can't give you: the old behaviour skips any file that
already exists, so a revised set of slides would never be re-fetched. The dashboard flags it and
replaces the stale copy.

Options: `--port`, `--legacy`, `--no_browser`. It binds to loopback only, and the session token is
never sent anywhere except NTULearn.

## Course scope

By default the tool reads **only the courses belonging to the semester in progress**, worked out
from today's date. No curation, no flags to update each term.

```
--scope semester    # default: courses labelled with the current semester
--scope favourites  # courses starred in Ultra
```

There is deliberately no "everything" option. A sync tool that can be pointed at every
enrolment you have ever had is one mis-click away from dragging years of stale material into
Drive.

NTU's academic year runs August to July, and the year label names the *starting* year:

| Date | Current semester |
|---|---|
| Aug 2026 – Dec 2026 | `26S1` |
| Jan 2027 – Jul 2027 | `26S2` |
| Aug 2027 – Dec 2027 | `27S1` |

June–July is the special term; it stays on the outgoing year's S2 until the new S1 begins in
August.

**Course names are not uniformly formatted**, so a `26S1-` prefix match is not enough. All three
observed forms are recognised:

```
26S1-SC2005-OPERATING SYSTEMS                      compact
AY2026-2027, Semester 1, MH2100 (Calculus III)     verbose
CC0015-HEALTH & WELLBEING (T002) AY2025/26 SEM 2   trailing
```

Courses stating no semester at all — PDPA, risk-management and other admin modules — are excluded
unless you pass `--include_undated`.

Verified against a live account of 45 enrolments: 7 current, 28 from other semesters, 10 undated.
The 7 matched the hand-curated Favourites list exactly.

## Transcription (Whisper)

Lecture videos that come without a transcript can be transcribed locally with open-source Whisper
(`faster-whisper`, CPU int8 — no GPU or torch required).

```
python -m ntu_learn_downloader.transcribe_run --download_to NTU --list   # survey only
python -m ntu_learn_downloader.transcribe_run --download_to NTU          # transcribe staged
python -m ntu_learn_downloader.transcribe_run --backfill                 # already in Drive
```

**It checks before it offers.** Every media file is classified first:

| Status | Meaning |
|---|---|
| `provided` | The course already supplies a transcript — never regenerated |
| `generated` | Produced here previously |
| `missing` | Nothing exists; a candidate for Whisper |

Detection matches both shapes lecturers actually use: a same-stem caption file (`Lecture 1.vtt`,
`.srt`, `.sbv`) or a named document (`Lecture 1 transcript.docx`). Generated files carry a
`NOTE Generated by iNTUition` header in the VTT so they can always be told apart from a lecturer's
own — without that the tool would eventually overwrite someone's work.

Output is `<name>.vtt` (captions, playable in Drive's preview) plus `<name>.txt` (flat prose, which
is what makes a lecture searchable), written beside the video.

**Run it before Push.** Move mode deletes the local video once Drive confirms it, so a transcript
generated afterwards would have no source. `--backfill` exists to repair that case: it pulls videos
back from Drive, transcribes, uploads the transcript alongside, and discards the temp copy.

Measured on an 8-core CPU with `small.en`: ~59 minutes of lecture audio took 29 minutes, roughly 2x
realtime. `base.en` is faster but noticeably worse on technical vocabulary; `medium.en` is about
realtime on CPU.

## R&D board and the Claude backend

The R&D panel is a single board across every course — a place to log tools you are thinking of
building, each with a course tag and a status (`idea → researching → prototyping → built`, plus
`parked`). It is plain local state in `.ntu_learn_downloader/rnd.json` and works with no network at
all.

Pressing **research** on one entry sends it to Claude, which searches the web and answers with a
verdict, prior art with links, the smallest first version worth building, and the likely pitfalls.
The finding is stored on the entry, so it survives restarts and a semester rollover.

```
python -m ntu_learn_downloader.research_run --setup      # the two backends, and what each needs
python -m ntu_learn_downloader.research_run --check      # backend, isolation, then one live call
python -m ntu_learn_downloader.research_run --list
python -m ntu_learn_downloader.research_run --id <id>    # or --all for every unresearched entry
```

### Two backends

| | `cli` (default) | `api` |
|---|---|---|
| Needs | Claude Code installed and logged in | `pip install anthropic` + a key |
| Credential | the login you already have — nothing stored by iNTUition | `ANTHROPIC_API_KEY`, `~/.ntu_learn_downloader/anthropic_key`, or an `ant auth login` profile |
| Billing | your Claude Code usage | Anthropic API, billed separately from a Claude subscription |
| Model | `opus` alias, resolved by the CLI | `claude-opus-5`, adaptive thinking, streamed |
| Cost cap | `--max-budget-usd`, hard, per entry | none (bounded by `max_tokens`) |

Pick one with `--backend cli|api` or `INTUITION_RESEARCH_BACKEND`; otherwise the CLI is used when
installed and the API otherwise. Either way only the *name* of the active credential appears in the
UI, never its value.

### The isolated research session (cli backend)

"Independent of the terminal and other channels" is enforced by the flags in
`research.build_cli_command`, each of which has a test asserting it is passed:

| Flag | Effect |
|---|---|
| `--safe-mode` | no CLAUDE.md, skills, plugins, hooks, custom agents, output styles or MCP servers — none of your Claude Code setup reaches it |
| `--strict-mcp-config` (no `--mcp-config`) | zero MCP servers, belt and braces |
| `--no-session-persistence` | nothing written to the session store: the run cannot be resumed and never shows up in `claude -c` or `--resume` |
| `--tools "WebSearch,WebFetch"` | the built-in tool set is pinned to the two web tools — no Bash, Read, Edit, Write or Task |
| `--allowed-tools WebSearch WebFetch` | pre-approves exactly those, since print mode has no human to ask |
| `--permission-mode dontAsk` | anything else is refused outright rather than hanging on a prompt |
| `--system-prompt` | replaces the coding-agent prompt entirely — a researcher, not an engineer with your filesystem |
| `--max-budget-usd` | hard per-entry ceiling |
| cwd | an empty sandbox at `<download_root>/.ntu_learn_downloader/research/`, not the repo and not your course tree |

`--dangerously-skip-permissions` is never passed, and a test asserts it never will be. If a tool is
denied anyway, the finding says so in the text rather than passing an unsourced answer off as
researched. The CLI returns prose rather than content blocks, so citations are the links it printed.

**What leaves the machine, and what does not.** This is the only part of iNTUition that talks to a
third party other than NTULearn and Drive, so the boundary is deliberately narrow:

| Always sent | Sent only with materials sharing on | Never sent |
|---|---|---|
| The entry you typed: title, notes, link, course tag | A capped selection of readable files for **that entry's course tag** | Material for any other course |
| The course codes you are enrolled in | Transcripts, slides, handouts — staged, then deleted | Videos and audio (skipped: unreadable, enormous) |
| — | Whatever the model chooses to read from the sandbox | Your session token, or anything from another course's folder |

**Materials sharing is a toggle in the panel, and it changes the boundary.** With it off, only the
first column leaves the machine. With it on, `materials.select` picks at most 12 files totalling at
most 12 MB for the entry's own course — transcripts ranked above documents — copies them into the
sandbox, and deletes them when the run ends (in a `finally`, so a crash cleans up too). Every finding
records exactly which files it read, so it stays auditable after the fact. Files already moved to
Drive are pulled back for the run.

The prompt is assembled in one place, `research.build_prompt`, so it can be read in full in a few
lines; tests assert a path or filename on an entry reaches neither the prompt nor the CLI's argv, and
that read tools appear only when material was actually staged. Nothing is ever sent on a scan, a poll,
or a page load — only on that press, per entry.

## Inbound (optional)

If Cerberus — a separate, private email-triage project — is checked out beside this one, the
dashboard shows an **Inbound** panel: NTU mail it flagged as
needing action (scholarship milestones, URECA, recruiting deadlines), most urgent first, beside the
teaching week.

The coupling is the weakest kind available. `inbound.py` opens `cerberus/storage/flagged.db` with
SQLite's `mode=ro` URI, so a bug here cannot mark something done or delete a row — Cerberus stays the
only writer of its own state. Neither project imports the other; the file path is the whole interface,
overridable with `--cerberus_db` or `INTUITION_CERBERUS_DB`. Email **bodies are never read** — subject,
sender, priority and the one-line reason are enough to decide whether to open the mail, and a test
asserts `body_content` never reaches the payload. If the database is absent, locked, corrupt or on an
older schema, the panel hides itself rather than failing a poll.

Both projects drive the Claude CLI through the same `claude_bridge` module, which owns the isolation
flags. Cerberus carries a vendored copy (it runs standalone and cannot import this package);
`tools/check_vendored.py` and a test fail on drift, because a copy that quietly loses a flag is the
entire risk that module exists to prevent.

## Google Drive pipeline

The full flow is **scan → retrieve → push**: Blackboard content is staged into the local folder,
then moved into Drive with the folder structure mirrored, and the local copy reclaimed.

```
NTULearn/                                    (Drive)
└── 26S1-ML0004-CAREER DESIGN.../
    ├── Tutorial Materials/
    │   └── Tutorial 1/
    │       └── AY26S1 Tutorial 1 ....pdf
    └── Important Information/
```

### One-time credential setup

```
python -m ntu_learn_downloader.drive_push --setup
```

Google requires an OAuth client belonging to *your* account; there is no way to ship one. The
command prints the Cloud Console steps. Save the downloaded JSON to
`~/.ntu_learn_downloader/google_client_secret.json`. The first push opens a browser for consent —
you complete it, and the token is cached at `~/.ntu_learn_downloader/google_token.json`. The tool
requests only the `drive.file` scope, so it can see nothing in your Drive that it did not create,
and it never handles your Google password.

### How change detection stays cheap

A scan does **not** re-walk the whole course. Two mechanisms keep it to roughly one
request per course:

1. **One recursive listing per course.** `contents?recursive=true` returns the entire
   content tree flat — every item with its `parentId`, `modified` stamp and handler — so
   the hierarchy is rebuilt locally instead of issuing one request per folder.
2. **Attachment lists cached against `modified`.** An item's attachments can only change
   when the item does, so they are cached in
   `<download_to>/.ntu_learn_downloader/content_cache.json` keyed by the stamp they were
   fetched under. Unchanged item → zero requests. Changed item → exactly one.

Measured against a live NTU account, 7 favourite courses / 113 files:

| | API calls | Wall time |
|---|---|---|
| Before (per-folder walk) | 225 | 111 s |
| First scan (cold cache) | 225 | 111 s |
| **Repeat scan, nothing changed** | **10** | **8.5 s** |
| One item edited by the professor | 11 | ~9 s |

So the steady-state cost of "has anything changed?" is a handful of requests, and only
genuinely new or edited material is fetched, downloaded and relayed to Drive. Deleting
the cache is safe — the next scan pays full price once and rebuilds it.

Verify the whole chain at any time:

```
python -m ntu_learn_downloader.drive_push --check
```

It stops at the first failure across four stages — libraries, client file, authorisation, and a
**live API probe** that creates and deletes a real file in Drive. Only the last stage proves the
upload path actually works. It also names the two misconfigurations that fail cryptically otherwise
(a service-account key, or a "Web application" client instead of "Desktop app").

Two gates are separate and both required: granting consent does **not** enable the Drive API on the
project. A missing API shows up as `403 accessNotConfigured`.

### Pushing

```
python -m ntu_learn_downloader.drive_push --download_to NTU --dry_run   # preview
python -m ntu_learn_downloader.drive_push --download_to NTU            # move
python -m ntu_learn_downloader.drive_push --download_to NTU --keep_local  # copy instead
```

Or press **Push to Drive** in the dashboard. Options: `--drive_folder` sets the Drive root
(default `NTULearn`).

### The ledger — why deleting local files is safe

Moving files destroys what the diff was reading: an absent file would look `new` and be
re-downloaded on every scan, forever. So each verified upload is recorded in
`<download_to>/.ntu_learn_downloader/drive_ledger.json` — the file's Drive id and the Blackboard
`modified` stamp at the time it was archived.

A later scan then reports:

- **`archived`** — gone locally, ledger confirms it is in Drive, unchanged upstream → not re-fetched
- **`updated`** — archived, but Blackboard has a newer timestamp → re-fetched and re-pushed

Safety properties: the local file is deleted only after Drive echoes back a matching byte size; a
mismatch keeps the file and reports the error. The ledger is written after every file, so an
interrupted run keeps its record. Re-uploading an existing name replaces it rather than creating a
Drive duplicate. Emptied folders are pruned, never above the staging root.

## CLI usage

```
usage: main.py [-h] [--bbrouter BBROUTER] [--token_file TOKEN_FILE]
               [--no_cache] [--legacy] [--download_to DOWNLOAD_TO]
               [--ignore IGNORE] [--ignore_files]
               [--download_recorded_lectures] [--sem SEM] [--prompt]

CLI wrapper to NTULearn Downloader

options:
  -h, --help            show this help message and exit
  --bbrouter BBROUTER   BbRouter session cookie copied from a logged-in browser.
                        Cached after first use, so you only need to pass it again
                        once it expires.
  --token_file TOKEN_FILE
                        Where to cache the session token
  --no_cache            Do not read or write the cached session token
  --legacy              Force the legacy Original-course-view HTML scraper instead
                        of the REST API
  --download_to DOWNLOAD_TO
                        Download destination (required if downloading files)
  --ignore IGNORE       Comma seperated list of modules to ignore, will ignore
                        module if it contains any of the supplied values (e.g.
                        CE2006)
  --ignore_files        Ignore downloading of files, useful if only
                        downloading lectures
  --download_recorded_lectures
                        Download recorded lectures. WARNING downloading large
                        files
  --sem SEM             Which semester to download from (e.g. AY2019/20
                        Semester 2 would be 19S2, see you are taking the
                        following courses output)
  --prompt              Prompt whether to download lecture video or files
                        above set (set with --max_size)
```

## Example

List your courses:

```
python main.py --bbrouter "expires:...,user:...,v:2,xsrf:..."
```

Download all files from 20/21 semester 1 (token already cached):

```
python main.py --sem 20S1 --download_to NTU
```

## Tests

```
python -m pytest ntu_learn_downloader/tests
```

292 tests, no network: both research backends are exercised against a fake client and a fake
subprocess runner, so the suite never issues a billed call and never spawns the CLI. The isolation
flags are pinned by tests — if a future edit drops `--no-session-persistence` or widens `--tools`,
the suite fails. The fixture HTTP server is started by the session fixture in `conftest.py`. (The suite previously
relied on nose's `setup_package` hook, which pytest ignores and which no longer runs on modern
Python.)

## Packaging

```
python setup.py sdist
```

The `tar.gz` file will be created in the `dist` folder
