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

The fixture HTTP server is started by the session fixture in `conftest.py`. (The suite previously
relied on nose's `setup_package` hook, which pytest ignores and which no longer runs on modern
Python.)

## Packaging

```
python setup.py sdist
```

The `tar.gz` file will be created in the `dist` folder
