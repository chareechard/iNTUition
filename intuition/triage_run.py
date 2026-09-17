"""Triage NTU mail from the command line.

    python -m intuition.triage_run --setup
    python -m intuition.triage_run --login
    python -m intuition.triage_run --scan
    python -m intuition.triage_run --scan --since 2026-07-01 --all
    python -m intuition.triage_run --list
    python -m intuition.triage_run --done <email_id>
    python -m intuition.triage_run --import_from ../J.A.R.V.I.S/cerberus

A scan is the whole pipeline in one pass: read unread mail since the cutoff,
prefilter it locally, classify what survives, and record the flags the dashboard's
Inbound panel reads. Bodies stay in this process - only subject, sender, priority
and the reason are ever written down.

The default cutoff is the start of the current semester, so the first scan of a
term catches up and every later one is cheap.
"""
import argparse
import io
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from intuition import academic_calendar, owa, semester, triage, triage_store

# Fallback window when the calendar has no entry for the current semester - long
# enough to be a real catch-up, short enough not to walk the whole mailbox.
FALLBACK_WINDOW_DAYS = 90

BACKTEST_FILENAME = "backtest.jsonl"
BACKTEST_SAMPLE = 20    # each one past the prefilter is a paid model call


def _oldest_first(emails):
    """Replay mailbox results bottom-up; OWA itself presents newest first."""
    return sorted(emails, key=lambda email: email.get("timestamp") or "")

# Subjects and senders carry arbitrary Unicode that the default Windows console
# codepage cannot encode; printing a results table must not die on an emoji.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SETUP = """\
Inbound reads your NTU mailbox through Outlook Web App in a real browser.

1. Install the browser driver (one time):

       pip install playwright
       python -m playwright install chromium

2. Link your mailbox (one time, and again whenever the session expires):

       python -m intuition.triage_run --login

   A browser window opens on office.com. Complete the NTU login and MFA exactly
   as you normally would. Nothing types on your behalf and no password or MFA
   code is ever seen by this tool - once your inbox renders, the resulting
   session cookies are saved to:

       {session}

3. Tune what gets watched (optional):

       {config}

4. Scan:

       python -m intuition.triage_run --scan

Classification runs through the Claude CLI with no tools at all, so an email can
ask for nothing and get nothing.\
"""


def _cutoff(text: str, config: Optional[dict] = None,
            today: Optional[date] = None) -> date:
    """The oldest date a run will read, in order of authority.

    ``--since`` for one run, then ``since`` in triage.json, then the start of the
    current semester. Mail from before the cutoff is somebody else's problem: the
    first run catches up, and every later one only pays for what arrived since.
    """
    stated = (text or "").strip() or ((config or {}).get("since") or "").strip()
    if stated:
        try:
            return datetime.strptime(stated, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(
                "since must be an ISO date, e.g. 2026-08-10 (got {!r})".format(stated))
    today = today or date.today()
    key = semester.format_semester(semester.current_semester(today))
    sem = academic_calendar.get(key)
    if sem is not None:
        return sem.week1_monday
    return today - timedelta(days=FALLBACK_WINDOW_DAYS)


def _scan(root: str, since: str, unread_only: bool, limit: int) -> int:
    config = triage.load_config(root)
    cutoff = _cutoff(since, config)
    store = triage_store.TriageStore(root)
    known = store.known_ids()
    started = datetime.now().astimezone().isoformat(timespec="seconds")

    print("Reading mail since {} ...".format(cutoff.isoformat()))
    try:
        with owa.Mailbox(root) as box:
            emails = box.fetch(cutoff, unread_only=unread_only, max_emails=limit)
    except owa.SessionExpired as exc:
        store.record_scan(started_at=started,
                          completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          cutoff=cutoff.isoformat(), status="session_expired",
                          error=str(exc), read=0, fresh=0, flagged=0)
        print(exc, file=sys.stderr)
        return 2

    # Skip anything already triaged before paying for a classification.
    fresh = _oldest_first(
        [e for e in emails if e.get("email_id") not in known])
    print("{} message(s) read, {} not yet triaged.".format(len(emails), len(fresh)))
    if not fresh:
        store.expire_due()
        store.record_scan(started_at=started,
                          completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          cutoff=cutoff.isoformat(), status="ok", read=len(emails),
                          fresh=0, flagged=0)
        return 0

    def progress(i, total, subject):
        print("  [{}/{}] {}".format(i, total, (subject or "(no subject)")[:60]))

    stats: dict = {}
    flags = triage.run_batch(fresh, config, sandbox=root, store=store,
                             on_progress=progress, stats=stats)
    store.expire_due()
    store.record_scan(started_at=started,
                      completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                      cutoff=cutoff.isoformat(), status="ok", read=len(emails),
                      fresh=len(fresh), flagged=len(flags),
                      analysed=stats.get("analysed", 0),
                      spent_usd=stats.get("spent_usd", 0.0),
                      stopped_early=stats.get("stopped_early", False))
    print("\n{} flagged, {} open in total.".format(len(flags), len(store.list_open())))
    print("{} classified, ${:.4f} spent.".format(
        stats.get("analysed", 0), stats.get("spent_usd", 0.0)))
    if stats.get("stopped_early"):
        print("Stopped at the ${:.2f} per-run ceiling with {} message(s) unclassified; "
              "they stay untriaged and will be picked up next run."
              .format(stats.get("budget_usd", 0.0),
                      stats.get("surviving", 0) - stats.get("analysed", 0)))
    for f in flags:
        print("  {:<8} {}".format(f["priority"], (f.get("subject") or "")[:64]))
    return 0


def _backtest(root: str, since: str, limit: int) -> int:
    """Run the pipeline over mail already in the inbox, and change nothing.

    The point is to see how the prefilter and the classifier behave on real mail
    before trusting what they write down. So: read mail is included, nothing is
    marked read, no flag is recorded, and the store is not even opened. What it
    does cost is one model call per email that survives the prefilter - which is
    exactly the number this reports, so the next run can be budgeted.
    """
    config = triage.load_config(root)
    cutoff = _cutoff(since, config)
    out_path = os.path.join(root, triage.STORAGE_DIR, BACKTEST_FILENAME)

    print("Backtest: reading mail dated {} onwards (read and unread, "
          "up to {}).".format(cutoff.isoformat(), limit))
    try:
        with owa.Mailbox(root) as box:
            emails = box.fetch(cutoff, unread_only=False, max_emails=limit)
    except owa.SessionExpired as exc:
        print(exc, file=sys.stderr)
        return 2

    pattern = triage.compile_keywords(config.get("keywords") or [])
    watched = config.get("watched_senders") or []
    rows = []
    for email in emails:
        passed = triage.prefilter(email, pattern, watched)
        analysis = None
        if passed:
            analysis = triage.analyse(
                email, config.get("user_goals", ""),
                config.get("user_intentions", ""), sandbox=root)
        rows.append({"email": email, "passed_prefilter": passed,
                     "analysis": analysis})

    _write_backtest(out_path, rows, cutoff)
    _report(rows, out_path)
    return 0


def _write_backtest(path, rows, cutoff):
    """Full results, including the bodies the store deliberately never keeps.

    A backtest is the one place the body matters - you cannot judge a verdict
    without the text it was passed - so this file is the sensitive artifact here,
    and it lives beside the rest of the local state rather than anywhere shared.
    """
    import json
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with io.open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(dict(r, tested_at=stamp, cutoff=cutoff.isoformat()),
                                ensure_ascii=False) + "\n")


def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _report(rows, out_path):
    print("\n{:<6} {:<14} {:<44} {}".format("FILTER", "PRIORITY", "SUBJECT", "SENDER"))
    print("-" * 104)
    for r in rows:
        analysis = r["analysis"]
        print("{:<6} {:<14} {:<44} {}".format(
            "PASS" if r["passed_prefilter"] else "skip",
            analysis["priority"] if analysis else "-",
            _clip(r["email"].get("subject") or "(no subject)", 44),
            _clip(r["email"].get("sender"), 32)))

    passed = [r for r in rows if r["passed_prefilter"]]
    analysed = [r for r in rows if r["analysis"]]
    flagged = [r for r in analysed
               if triage.should_flag(r["analysis"])]
    failed = [r for r in analysed if not r["analysis"].get("ok")]
    spend = sum(r["analysis"].get("cost_usd") or 0 for r in analysed)

    print("\n{} read | {} passed the prefilter | {} would be flagged ({})".format(
        len(rows), len(passed), len(flagged), "/".join(triage.FLAG_AT_OR_ABOVE)))
    counts = {}
    for r in analysed:
        p = r["analysis"]["priority"]
        counts[p] = counts.get(p, 0) + 1
    if counts:
        print("verdicts: " + ", ".join(
            "{} {}".format(counts[p], p) for p in triage.PRIORITIES if p in counts))
    if failed:
        print("{} analysis failure(s) - see the reasoning field".format(len(failed)))
    print("cost: ${:.4f} over {} model call(s)".format(spend, len(analysed)))
    print("nothing was flagged, marked read, or recorded. Full results: {}".format(
        out_path))


def _list(root: str) -> int:
    store = triage_store.TriageStore(root)
    rows = store.list_open()
    if not rows:
        print("No open flags.")
        return 0
    for r in rows:
        print("{:<8} {:<14} {}".format(
            r.get("priority", ""), (r.get("email_id") or "")[:14],
            (r.get("subject") or r.get("matched_snippet") or "")[:60]))
    return 0


def _import_from(root: str, source: str) -> int:
    """Bring a prototype's tuned config and open flags across, once.

    Independence should not cost the tuning that made the thing useful, and the
    stores share a schema, so this is a copy rather than a migration.
    """
    import json
    import sqlite3

    source = os.path.abspath(source)
    moved = []

    config_file = os.path.join(source, "config", "config.json")
    if os.path.isfile(config_file):
        try:
            with open(config_file, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError) as exc:
            print("Could not read {}: {}".format(config_file, exc), file=sys.stderr)
            old = {}
        config = triage.load_config(root, create=False)
        for key in ("keywords", "watched_senders", "user_goals", "user_intentions"):
            if old.get(key):
                config[key] = old[key]
        path = triage.save_config(root, config)
        moved.append("config -> {} ({} keywords, {} senders)".format(
            path, len(config.get("keywords") or []),
            len(config.get("watched_senders") or [])))

    db = os.path.join(source, "storage", "flagged.db")
    if os.path.isfile(db):
        store = triage_store.TriageStore(root)
        copied = 0
        try:
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(db.replace("\\", "/")), uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM flagged_emails WHERE status = 'open'").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print("Could not read {}: {}".format(db, exc), file=sys.stderr)
            rows = []
        for row in rows:
            keys = row.keys()
            get = lambda k: (row[k] if k in keys and row[k] is not None else "")
            store.record({
                "email_id": get("email_id"),
                "sender": get("sender"),
                "subject": get("subject"),
                "priority": get("priority") or "Medium",
                "reasoning": get("reasoning"),
                "action_items": get("action_items"),
                "flagged_at": get("flagged_at"),
                "confidence": get("confidence"),
                "matched_snippet": get("snippet") or get("matched_snippet"),
                "link": get("link"),
                "due": get("due"),
            })
            copied += 1
        moved.append("{} open flag(s) -> {}".format(copied, store.path))

    if not moved:
        print("Nothing found to import under {}".format(source), file=sys.stderr)
        return 1
    for line in moved:
        print("imported " + line)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Triage NTU mail for Inbound")
    parser.add_argument("--download_to", default="NTU",
                        help="Folder whose .intuition/ holds the store")
    parser.add_argument("--setup", action="store_true",
                        help="Print setup instructions")
    parser.add_argument("--login", action="store_true",
                        help="Open a browser to link the mailbox, and save the session")
    parser.add_argument("--scan", action="store_true",
                        help="Read, classify and record new mail")
    parser.add_argument("--backtest", action="store_true",
                        help="Dry run over mail already in the inbox: classify and "
                             "report, but record nothing and mark nothing read")
    parser.add_argument("--since", default="",
                        help="ISO cutoff date (default: start of the current semester)")
    parser.add_argument("--all", action="store_true",
                        help="Include already-read mail, and do not mark anything read")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap messages read in one scan (default: from triage.json)")
    parser.add_argument("--list", action="store_true", dest="list_open",
                        help="List open flags")
    parser.add_argument("--done", metavar="EMAIL_ID", help="Mark a flag done")
    parser.add_argument("--import_from", metavar="DIR",
                        help="Copy config and open flags from a Cerberus checkout")
    args = parser.parse_args()

    root = os.path.abspath(args.download_to)

    if args.setup:
        print(SETUP.format(session=owa.session_path(root),
                           config=triage.config_path(root)))
        return 0
    if args.login:
        try:
            owa.login(root, on_status=print)
        except owa.SessionExpired as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    if args.import_from:
        return _import_from(root, args.import_from)
    if args.list_open:
        return _list(root)
    if args.done:
        ok = triage_store.TriageStore(root).mark_done(args.done)
        print("marked done" if ok else "no open flag with that id")
        return 0 if ok else 1
    if args.backtest:
        return _backtest(root, args.since, args.limit or BACKTEST_SAMPLE)
    if args.scan:
        limit = args.limit or triage.load_config(root).get("max_emails_per_run", 50)
        return _scan(root, args.since, unread_only=not args.all, limit=limit)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
