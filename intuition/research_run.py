"""Set up and check the Claude research backend from the command line.

    python -m intuition.research_run --setup
    python -m intuition.research_run --save_key
    python -m intuition.research_run --check

Configures the same backend the dashboard's AI-backed features (Todo research, the
materials chat, the Drive learning chat) resolve through ``ai_provider``/``research``.
"""
import argparse
import getpass
import os
import sys

from intuition import research


def main():
    parser = argparse.ArgumentParser(
        description="Set up and check the Claude research backend")
    parser.add_argument("--download_to", default="NTU",
                        help="Folder whose .intuition/ holds the sandbox")
    parser.add_argument("--setup", action="store_true", help="Print setup instructions")
    parser.add_argument("--save_key", action="store_true",
                        help="Prompt for an API key and save it (input is not echoed)")
    parser.add_argument("--check", action="store_true",
                        help="Verify the SDK, the key, and one live call")
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
