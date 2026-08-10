"""Research R&D board entries from the command line.

    python -m ntu_learn_downloader.research_run --setup
    python -m ntu_learn_downloader.research_run --save_key
    python -m ntu_learn_downloader.research_run --check
    python -m ntu_learn_downloader.research_run --list
    python -m ntu_learn_downloader.research_run --id 3f9a12c40b7e
    python -m ntu_learn_downloader.research_run --all

Same guarantees as the dashboard button: nothing is sent unless you name an entry, and
what is sent is the entry text plus your board's course tags - never course files.
"""
import argparse
import getpass
import os
import re
import sys

from ntu_learn_downloader import research, rnd


def _codes(board: rnd.Board) -> list:
    return sorted({c for i in board.items
                   for c in re.findall(r"[A-Z]{2,4}\d{4}", (i.get("course") or ""))})


def main():
    parser = argparse.ArgumentParser(
        description="Research R&D board entries with Claude")
    parser.add_argument("--download_to", default="NTU",
                        help="Folder whose .ntu_learn_downloader/ holds the board")
    parser.add_argument("--setup", action="store_true", help="Print setup instructions")
    parser.add_argument("--save_key", action="store_true",
                        help="Prompt for an API key and save it (input is not echoed)")
    parser.add_argument("--check", action="store_true",
                        help="Verify the SDK, the key, and one live call")
    parser.add_argument("--list", action="store_true",
                        help="List board entries and whether each has a finding")
    parser.add_argument("--id", help="Research the entry with this id")
    parser.add_argument("--all", action="store_true",
                        help="Research every entry that has no finding yet")
    parser.add_argument("--no_web", action="store_true",
                        help="Disable web search (cheaper, no citations)")
    parser.add_argument("--backend", choices=research.BACKENDS,
                        help="cli (Claude Code login) or api (Anthropic key). "
                             "Default: cli when installed, else api.")
    parser.add_argument("--model", help="Model, or a CLI alias like opus")
    args = parser.parse_args()

    if args.setup:
        print(research.SETUP_HELP.format(path=research.key_path(), max_usd=research.CLI_MAX_USD))
        return 0

    if args.save_key:
        key = getpass.getpass("Anthropic API key (not echoed): ")
        try:
            path = research.save_key(key)
        except ValueError as e:
            print(e)
            return 1
        print("Saved to {}".format(path))
        return 0

    if args.check:
        st = research.status(args.backend)
        print("1. Claude CLI     : {}".format(
            st["cli_version"] or "not on PATH"))
        print("2. anthropic SDK  : {}".format(
            "installed" if st["sdk"] else "not installed"))
        print("3. API credential : {}".format(st["source"] if st["backend"] == "api"
                                              else research.credential_source()
                                              or "none"))
        print("4. backend chosen : {}".format(st["backend"] or "NONE"))
        if not st["ready"]:
            print("\n" + research.SETUP_HELP.format(path=research.key_path(), max_usd=research.CLI_MAX_USD))
            return 1
        if st["backend"] == research.BACKEND_CLI:
            print("   isolation      : safe-mode, no session persistence, no MCP, "
                  "tools pinned to WebSearch/WebFetch, ${} cap".format(st["max_usd"]))
            print("   sandbox        : {}".format(
                research.sandbox_dir(os.path.abspath(args.download_to))))
        print("5. live call      : ", end="", flush=True)
        try:
            finding = research.research(
                {"title": "A one-line smoke test; reply with the single word READY"},
                model=args.model, web=False, backend=args.backend,
                download_root=os.path.abspath(args.download_to))
        except research.ResearchError as e:
            print("FAILED - {}".format(e))
            return 1
        print("ok ({}, {} out-tokens{})".format(
            finding["model"], finding["tokens"]["out"],
            ", ${:.4f}".format(finding["cost_usd"]) if finding.get("cost_usd") else ""))
        return 0

    root = os.path.abspath(args.download_to)
    board = rnd.Board(root)

    if args.list:
        if not len(board):
            print("No board entries. Add some in the dashboard.")
            return 1
        for i in board.items:
            f = i.get("research")
            print("{}  {:<10} {:<9} {}".format(
                i["id"], i.get("course") or "-", i.get("status", ""), i["title"]))
            print("{}  {}".format(" " * 12,
                                  "researched {}".format(f["at"]) if f else "no finding"))
        return 0

    targets = []
    if args.id:
        item = board.get(args.id)
        if item is None:
            print("No entry with id {}. Use --list.".format(args.id))
            return 1
        targets = [item]
    elif args.all:
        targets = [i for i in board.items if not i.get("research")]
        if not targets:
            print("Every entry already has a finding. Use --id to redo one.")
            return 0
    else:
        parser.print_help()
        return 1

    codes = _codes(board)
    failed = 0
    for item in targets:
        print("\n=== {} ({}) ===".format(item["title"], item["id"]))
        try:
            finding = research.research(item, courses=codes, model=args.model,
                                        web=not args.no_web, backend=args.backend,
                                        download_root=root)
        except research.ResearchError as e:
            print("FAILED: {}".format(e))
            failed += 1
            continue
        board.set_research(item["id"], finding)
        board.save()
        print(finding["text"])
        for s in finding["sources"]:
            print("  - {}  {}".format(s["title"], s["url"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
