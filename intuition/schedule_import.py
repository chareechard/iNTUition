"""Import a class schedule for the Temporal Protocol panel.

    # paste the timetable, then Ctrl+Z (Windows) / Ctrl+D (POSIX)
    python -m intuition.schedule_import --download_to NTU

    # or from a file, including an .ics export
    python -m intuition.schedule_import --file timetable.txt

STARS itself cannot be read by this tool: ``AUS_STARS_PLANNER.planner`` is a form POST
target behind separate NTU domain credentials, which are deliberately never handled
here. Copying the rendered timetable out of the browser keeps every credential with you.
"""
import argparse
import os
import sys

from intuition import schedule

HOW_TO = """\
Where to copy the timetable from:

  1. https://wish.wis.ntu.edu.sg/pls/webexe/aus_stars_planner.main_display1
     (or Student Intranet -> StudentLink -> Class Schedule)
  2. Select the table of classes - course, index, type, group, day, time, venue
  3. Copy, then paste it here

A STARS "Course(s) Registered" PDF or an .ics export work too: pass with --file.\
"""


def main():
    parser = argparse.ArgumentParser(
        description="Import an NTU class schedule into the Temporal Protocol panel")
    parser.add_argument("--download_to", default="NTU",
                        help="Folder whose .intuition/ holds the schedule")
    parser.add_argument("--file", help="Read the timetable or .ics from this file")
    parser.add_argument("--show", action="store_true",
                        help="Print the stored schedule and exit")
    parser.add_argument("--clear", action="store_true", help="Delete the stored schedule")
    parser.add_argument("--dry_run", action="store_true",
                        help="Parse and print without saving")
    args = parser.parse_args()

    root = os.path.abspath(args.download_to)
    os.makedirs(root, exist_ok=True)
    store = schedule.Schedule(root)

    if args.show:
        if not len(store):
            print("No schedule stored. Import one first.\n\n" + HOW_TO)
            return 1
        print("{} session(s), imported {}".format(len(store), store.imported_at))
        for day, rows in store.week().items():
            if not rows:
                continue
            print("\n{}".format(day))
            for r in rows:
                print("  {}-{}  {:<8} {:<7} {:<10} {}".format(
                    r["start"], r["end"], r["course"], r["type"] or "-",
                    r["venue"] or "-", r["weeks"] or ""))
        return 0

    if args.clear:
        if os.path.exists(store.path):
            os.remove(store.path)
            print("Removed {}".format(store.path))
        else:
            print("Nothing stored.")
        return 0

    if args.file and args.file.lower().endswith(".pdf"):
        result = schedule.parse_file(args.file)
        sessions = result["sessions"]
        if sessions:
            print("\nParsed {} session(s), {} exam(s) from the STARS PDF:".format(
                len(sessions), len(result["exams"])))
            for r in sessions:
                print("  {:<4} {}-{}  {:<8} {:<5} {:<11} {}".format(
                    r["day"], r["start"], r["end"], r["course"], r["type"] or "-",
                    r["venue"] or "-", r["weeks"] or ""))
            if args.dry_run:
                print("\n(dry run, nothing saved)")
                return 0
            store.replace(sessions, exams=result["exams"],
                          courses=result["courses"],
                          semester=result.get("semester"))
            store.save()
            print("\nSaved to {}".format(store.path))
            return 0
        print("No classes found in that PDF.")
        return 1

    if args.file:
        with open(args.file, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    else:
        print(HOW_TO)
        print("\nPaste the timetable below, then Ctrl+Z + Enter (Windows) "
              "or Ctrl+D (macOS/Linux):\n")
        text = sys.stdin.read()

    sessions = schedule.dedupe(schedule.parse(text))
    if not sessions:
        print("\nNothing recognised. A row needs at least a day and a HHMM-HHMM time.")
        print("Parsed 0 sessions from {} characters.".format(len(text)))
        return 1

    print("\nParsed {} session(s):".format(len(sessions)))
    for r in sessions:
        print("  {:<4} {}-{}  {:<8} {:<7} {:<10} {}".format(
            r["day"], r["start"], r["end"], r["course"], r["type"] or "-",
            r["venue"] or "-", r["weeks"] or ""))

    if args.dry_run:
        print("\n(dry run, nothing saved)")
        return 0

    store.replace(sessions)
    store.save()
    print("\nSaved to {}".format(store.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
