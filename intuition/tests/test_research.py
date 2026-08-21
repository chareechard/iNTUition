import json
import os
import unittest
from tempfile import TemporaryDirectory

from intuition import claude_bridge, research


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Usage:
    input_tokens = 1200
    output_tokens = 340


class Message:
    def __init__(self, content, stop_reason="end_turn", model="claude-opus-5"):
        self.content = content
        self.stop_reason = stop_reason
        self.model = model
        self.usage = Usage()


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeMessages:
    def __init__(self, message=None, error=None):
        self.message = message
        self.error = error
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return FakeStream(self.message)


class FakeClient:
    def __init__(self, message=None, error=None):
        self.messages = FakeMessages(message, error)


def ok_message(text="**Verdict** - worth building."):
    return Message([
        Block(type="text", text=text),
        Block(type="web_search_tool_result", content=[
            Block(url="https://a.test/x", title="A"),
            Block(url="https://a.test/x", title="A duplicate"),
            Block(url="https://b.test/y", title="B"),
        ]),
    ])


class TestPrompt(unittest.TestCase):
    def test_only_the_entry_and_course_codes_are_sent(self):
        prompt = research.build_prompt(
            {"title": "Flashcards", "course": "MH2500", "notes": "from transcripts",
             "link": "https://x.test"},
            courses=["SC2002", "MH2500"])
        for expected in ("Flashcards", "MH2500", "from transcripts", "https://x.test",
                         "SC2002"):
            self.assertIn(expected, prompt)

    def test_optional_fields_are_omitted_when_empty(self):
        prompt = research.build_prompt({"title": "Just a title"})
        self.assertIn("Just a title", prompt)
        self.assertNotIn("Course:", prompt)
        self.assertNotIn("My notes:", prompt)
        self.assertNotIn("Courses I am taking", prompt)

    def test_the_direction_is_absent_unless_one_is_set(self):
        """Optional means the prompt is byte-identical when nothing is typed."""
        item = {"title": "Flashcards", "course": "MH2500"}
        self.assertEqual(research.build_prompt(item),
                         research.build_prompt(item, direction=""))
        self.assertEqual(research.build_prompt(item),
                         research.build_prompt(item, direction="   \n  "))
        self.assertNotIn("direction I want", research.build_prompt(item))

    def test_the_direction_reaches_the_prompt_when_set(self):
        prompt = research.build_prompt(
            {"title": "Flashcards"}, direction="Prefer things I can finish quickly")
        self.assertIn("Prefer things I can finish quickly", prompt)
        self.assertIn("direction I want", prompt)

    def test_the_direction_is_a_preference_not_a_cage(self):
        """A steer must not be able to suppress a better answer outside it."""
        prompt = research.build_prompt({"title": "x"}, direction="only Rust")
        self.assertIn("falls outside it, say so and give it anyway", prompt)

    def test_the_direction_does_not_displace_the_entry(self):
        prompt = research.build_prompt(
            {"title": "Flashcards", "course": "MH2500", "notes": "from transcripts"},
            courses=["SC2002"], direction="Lean mathematical")
        for expected in ("Flashcards", "MH2500", "from transcripts", "SC2002",
                         "Lean mathematical"):
            self.assertIn(expected, prompt)

    def test_the_academic_year_is_not_mistaken_for_a_course(self):
        """"AY2026" has a course code's exact shape and leads the verbose name.

        Lives here because the consequence is prompt content: every run used to tell
        the model it was taking a course called AY2026.
        """
        import threading

        from intuition import dashboard

        class FakeState:
            lock = threading.Lock()
            courses = [
                {"name": "26S1-SC2002-OBJECT ORIENTED DESIGN & PROGRAMMING"},
                {"name": "AY2026-2027, Semester 1, MH2100 (Calculus III)"},
                {"name": "CC0015-HEALTH & WELLBEING (T002) AY2025/26 SEM 2"},
                {"name": "Personal Data Protection Act (PDPA) e-Learning"},
            ]

        codes = dashboard.course_codes(FakeState())
        self.assertEqual(codes, ["CC0015", "MH2100", "SC2002"])
        self.assertNotIn("AY2026", codes)
        self.assertNotIn("AY2025", codes)

    def test_no_filesystem_paths_leak_into_the_prompt(self):
        """The prompt builder must not be able to reach the download folder."""
        prompt = research.build_prompt(
            {"title": "x", "path": "NTU/26S1-SC2002/Week 1/lecture.pdf",
             "filename": "lecture.pdf"})
        self.assertNotIn("lecture.pdf", prompt)
        self.assertNotIn("NTU", prompt)


class TestResearch(unittest.TestCase):
    def test_happy_path(self):
        client = FakeClient(ok_message())
        out = research.research({"title": "Scheduler simulator", "course": "SC2005"},
                                courses=["SC2005"], client=client)
        self.assertIn("worth building", out["text"])
        self.assertEqual(out["model"], "claude-opus-5")
        self.assertEqual(out["tokens"], {"in": 1200, "out": 340})
        self.assertTrue(out["at"])

    def test_sources_are_deduped_by_url(self):
        out = research.research({"title": "x"}, client=FakeClient(ok_message()))
        self.assertEqual([s["url"] for s in out["sources"]],
                         ["https://a.test/x", "https://b.test/y"])

    def test_a_failed_search_block_does_not_break_extraction(self):
        message = Message([
            Block(type="text", text="No prior art found."),
            # A capped or unavailable search returns an error object, not a list.
            Block(type="web_search_tool_result",
                  content=Block(type="web_search_tool_result_error",
                                error_code="max_uses_exceeded")),
        ])
        out = research.research({"title": "x"}, client=FakeClient(message))
        self.assertEqual(out["sources"], [])
        self.assertEqual(out["text"], "No prior art found.")

    def test_web_search_can_be_disabled(self):
        client = FakeClient(ok_message())
        research.research({"title": "x"}, client=client, web=False)
        self.assertNotIn("tools", client.messages.calls[0])

        client = FakeClient(ok_message())
        research.research({"title": "x"}, client=client)
        self.assertEqual(client.messages.calls[0]["tools"][0]["type"],
                         research.WEB_SEARCH_TOOL)

    def test_adaptive_thinking_and_no_sampling_params(self):
        """Opus 5 rejects budget_tokens and temperature outright."""
        client = FakeClient(ok_message())
        research.research({"title": "x"}, client=client)
        sent = client.messages.calls[0]
        self.assertEqual(sent["thinking"], {"type": "adaptive"})
        for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
            self.assertNotIn(banned, sent)

    def test_a_title_is_required(self):
        with self.assertRaises(research.ResearchError):
            research.research({"title": "  "}, client=FakeClient(ok_message()))

    def test_refusal_is_reported_not_stored_as_text(self):
        message = Message([Block(type="text", text="")], stop_reason="refusal")
        with self.assertRaises(research.ResearchError):
            research.research({"title": "x"}, client=FakeClient(message))

    def test_empty_response_is_an_error(self):
        message = Message([Block(type="thinking", thinking="")])
        with self.assertRaises(research.ResearchError):
            research.research({"title": "x"}, client=FakeClient(message))

    def test_api_errors_surface_as_research_errors(self):
        class AuthenticationError(Exception):
            pass

        client = FakeClient(error=AuthenticationError("401 invalid x-api-key"))
        with self.assertRaises(research.ResearchError) as cm:
            research.research({"title": "x"}, client=client)
        self.assertIn("rejected", str(cm.exception))

    def test_error_text_never_echoes_the_key(self):
        secret = "sk-ant-secret-value"

        class AuthenticationError(Exception):
            pass

        client = FakeClient(error=AuthenticationError("bad key " + secret))
        with self.assertRaises(research.ResearchError) as cm:
            research.research({"title": "x"}, client=client)
        self.assertNotIn(secret, str(cm.exception))


class FakeProc:
    def __init__(self, payload, stderr="", returncode=0):
        self.stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.stderr = stderr
        self.returncode = returncode


def cli_payload(result="**Verdict** - build it. See https://a.test/x",
                **overrides):
    data = {
        "is_error": False,
        "subtype": "success",
        "stop_reason": "end_turn",
        "result": result,
        "total_cost_usd": 0.0542,
        "permission_denials": [],
        "usage": {"input_tokens": 900, "output_tokens": 410,
                  "server_tool_use": {"web_search_requests": 3,
                                      "web_fetch_requests": 1}},
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"inputTokens": 527, "outputTokens": 13,
                                          "webSearchRequests": 0,
                                          "canonicalModel": "claude-haiku-4-5"},
            "claude-opus-5": {"inputTokens": 61000, "outputTokens": 410,
                              "webSearchRequests": 3,
                              "canonicalModel": "claude-opus-5"},
        },
    }
    data.update(overrides)
    return data


class Runner:
    """Stands in for subprocess.run so no CLI process is ever spawned in tests."""

    def __init__(self, proc=None, raises=None):
        self.proc = proc if proc is not None else FakeProc(cli_payload())
        self.raises = raises
        self.cmd = None
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        return self.proc


class TestCliIsolation(unittest.TestCase):
    """The isolation flags are the feature; assert each one is actually passed."""

    def cmd(self, **kw):
        return research.build_cli_command("a prompt", **kw)

    def test_runs_non_interactively_as_json(self):
        cmd = self.cmd()
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")

    def test_no_local_configuration_reaches_the_session(self):
        cmd = self.cmd()
        # CLAUDE.md, skills, plugins, hooks, custom agents, MCP servers.
        self.assertIn("--safe-mode", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_leaves_no_resumable_session_behind(self):
        """Independent of the terminal: invisible to `claude -c` and `--resume`."""
        cmd = self.cmd()
        self.assertIn("--no-session-persistence", cmd)
        for shared in ("--continue", "-c", "--resume", "-r", "--fork-session"):
            self.assertNotIn(shared, cmd)

    def test_tools_are_pinned_to_the_web_and_pre_approved(self):
        cmd = self.cmd()
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertIn("--allowed-tools", cmd)
        # Print mode cannot ask a human, so anything unlisted must be refused outright.
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")

    def test_never_bypasses_permissions(self):
        cmd = self.cmd()
        for danger in ("--dangerously-skip-permissions",
                       "--allow-dangerously-skip-permissions"):
            self.assertNotIn(danger, cmd)
        self.assertNotIn("bypassPermissions", cmd)

    def test_no_filesystem_reach_beyond_the_sandbox(self):
        self.assertNotIn("--add-dir", self.cmd())

    def test_spend_is_capped_per_entry(self):
        cmd = self.cmd(max_usd=0.25)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.25")

    def test_disabling_web_removes_every_tool(self):
        cmd = self.cmd(web=False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowed-tools", cmd)

    def test_the_research_prompt_replaces_the_coding_agent_prompt(self):
        cmd = self.cmd()
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], research.SYSTEM_PROMPT)
        self.assertNotIn("--append-system-prompt", cmd)

    def test_sandbox_is_an_empty_dir_outside_the_course_tree(self):
        with TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "26S1-SC2002"))
            open(os.path.join(root, "26S1-SC2002", "lecture.pdf"), "w").write("x")
            sandbox = research.sandbox_dir(root)
            self.assertTrue(os.path.isdir(sandbox))
            self.assertEqual(os.listdir(sandbox), [])
            self.assertIn(research.STORAGE_DIR, sandbox)

    def test_the_session_runs_in_the_sandbox(self):
        with TemporaryDirectory() as root:
            runner = Runner()
            research.research_via_cli({"title": "x"}, download_root=root, runner=runner)
            self.assertEqual(runner.kwargs["cwd"], research.sandbox_dir(root))

    def test_the_session_gets_no_console_of_its_own(self):
        """Without this the windowed desktop build flashes a terminal per run."""
        with TemporaryDirectory() as root:
            runner = Runner()
            research.research_via_cli({"title": "x"}, download_root=root, runner=runner)
            self.assertEqual(runner.kwargs["creationflags"],
                             claude_bridge.no_window())


class TestCliBackend(unittest.TestCase):
    def test_parses_a_successful_run(self):
        with TemporaryDirectory() as root:
            out = research.research_via_cli(
                {"title": "Scheduler simulator", "course": "SC2005"},
                courses=["SC2005"], download_root=root, runner=Runner())
        self.assertIn("build it", out["text"])
        self.assertEqual(out["backend"], "cli")
        self.assertEqual(out["model"], "claude-opus-5")   # not the side-task haiku
        # Cumulative across the run, not just its final step.
        self.assertEqual(out["tokens"], {"in": 61527, "out": 423})
        self.assertEqual(out["searches"], 3)
        self.assertEqual(out["cost_usd"], 0.0542)
        self.assertEqual([s["url"] for s in out["sources"]], ["https://a.test/x"])

    def test_the_side_task_model_is_not_credited_with_the_answer(self):
        """On a short answer the CLI's internal helper model out-tokens the real one."""
        runner = Runner(FakeProc(cli_payload(result="READY", modelUsage={
            "claude-haiku-4-5-20251001": {"outputTokens": 13,
                                          "canonicalModel": "claude-haiku-4-5"},
            "claude-opus-5": {"outputTokens": 4, "canonicalModel": "claude-opus-5"},
        })))
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            model="opus", runner=runner)
        self.assertEqual(out["model"], "claude-opus-5")

    def test_final_step_usage_does_not_stand_in_for_the_whole_run(self):
        """Observed live: top-level usage said 4 in-tokens and 0 searches on a run
        that made three searches and cost twelve cents."""
        runner = Runner(FakeProc(cli_payload(
            usage={"input_tokens": 4, "output_tokens": 1116,
                   "server_tool_use": {"web_search_requests": 0}})))
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            runner=runner)
        self.assertEqual(out["tokens"]["in"], 61527)
        self.assertEqual(out["searches"], 3)

    def test_falls_back_to_usage_when_no_per_model_block(self):
        runner = Runner(FakeProc(cli_payload(modelUsage={})))
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            runner=runner)
        self.assertEqual(out["tokens"], {"in": 900, "out": 410})
        self.assertEqual(out["searches"], 3)

    def test_sources_come_from_the_links_in_the_prose(self):
        runner = Runner(FakeProc(cli_payload(
            result="see https://a.test/one, and https://b.test/two. Also https://a.test/one")))
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            runner=runner)
        self.assertEqual([s["url"] for s in out["sources"]],
                         ["https://a.test/one", "https://b.test/two"])

    def test_a_failed_run_raises(self):
        runner = Runner(FakeProc(cli_payload(is_error=True, subtype="error_max_turns")))
        with TemporaryDirectory() as root:
            with self.assertRaises(research.ResearchError):
                research.research_via_cli({"title": "x"}, download_root=root,
                                          runner=runner)

    def test_denied_tools_are_disclosed_not_hidden(self):
        """An answer reached without the web must not pass as a researched one."""
        runner = Runner(FakeProc(cli_payload(
            result="I could not search.",
            permission_denials=[{"tool_name": "WebSearch"}])))
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            runner=runner)
        self.assertIn("WebSearch was denied", out["text"])
        self.assertIn("unsourced", out["text"])

    def test_unparseable_output_raises(self):
        runner = Runner(FakeProc("not json at all"))
        with TemporaryDirectory() as root:
            with self.assertRaises(research.ResearchError):
                research.research_via_cli({"title": "x"}, download_root=root,
                                          runner=runner)

    def test_empty_output_reports_stderr(self):
        runner = Runner(FakeProc("", stderr="command not found: claude"))
        with TemporaryDirectory() as root:
            with self.assertRaises(research.ResearchError) as cm:
                research.research_via_cli({"title": "x"}, download_root=root,
                                          runner=runner)
        self.assertIn("command not found", str(cm.exception))

    def test_a_hang_times_out_rather_than_wedging_the_dashboard(self):
        import subprocess as sp
        runner = Runner(raises=sp.TimeoutExpired(cmd="claude", timeout=1))
        with TemporaryDirectory() as root:
            with self.assertRaises(research.ResearchError) as cm:
                research.research_via_cli({"title": "x"}, download_root=root,
                                          runner=runner)
        self.assertIn("did not finish", str(cm.exception))

    def test_only_the_entry_text_reaches_the_command_line(self):
        with TemporaryDirectory() as root:
            runner = Runner()
            research.research_via_cli(
                {"title": "Flashcards", "course": "MH2500",
                 "path": os.path.join(root, "26S1-MH2500", "lecture.pdf")},
                courses=["MH2500"], download_root=root, runner=runner)
        joined = " ".join(runner.cmd)
        self.assertIn("Flashcards", joined)
        self.assertIn("MH2500", joined)
        self.assertNotIn("lecture.pdf", joined)

    def test_the_direction_reaches_the_cli_backend(self):
        """The steer is wired end to end, not just into build_prompt."""
        with TemporaryDirectory() as root:
            runner = Runner()
            research.research({"title": "Flashcards"}, download_root=root,
                              runner=runner, direction="Lean mathematical")
        self.assertIn("Lean mathematical", " ".join(runner.cmd))

    def test_no_direction_leaves_the_command_unchanged(self):
        with TemporaryDirectory() as root:
            without = Runner()
            research.research({"title": "Flashcards"}, download_root=root,
                              runner=without)
            blank = Runner()
            research.research({"title": "Flashcards"}, download_root=root,
                              runner=blank, direction="")
        self.assertEqual(without.cmd, blank.cmd)


class TestMaterialsSharing(unittest.TestCase):
    """Course material is the one thing that changes the data boundary, so the
    read tools must appear only when material was actually staged."""

    def spec(self, root, name="Lecture 1.txt"):
        path = os.path.join(root, "26S1-SC2005-OS", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("round robin scheduling, quantum 4ms")
        return [{"rel": "26S1-SC2005-OS/" + name, "name": name, "size": 36,
                 "local": path, "drive_id": None}]

    def test_no_read_tools_when_nothing_is_shared(self):
        cmd = research.build_cli_command("p", read_materials=False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        for tool in ("Read", "Glob", "Grep"):
            self.assertNotIn(tool, cmd)

    def test_read_tools_appear_only_alongside_staged_material(self):
        cmd = research.build_cli_command("p", read_materials=True)
        self.assertEqual(cmd[cmd.index("--tools") + 1],
                         "WebSearch,WebFetch,Read,Glob,Grep")
        # Reading is added; writing and shelling out never are.
        for never in ("Write", "Edit", "Bash", "Task", "NotebookEdit"):
            self.assertNotIn(never, cmd)
        self.assertNotIn("--add-dir", cmd)

    def test_staged_files_are_named_in_the_prompt(self):
        with TemporaryDirectory() as root:
            runner = Runner()
            research.research_via_cli({"title": "x", "course": "SC2005"},
                                      download_root=root, runner=runner,
                                      material_specs=self.spec(root))
            prompt = runner.cmd[runner.cmd.index("-p") + 1]
        self.assertIn("./materials", prompt)
        self.assertIn("Lecture 1.txt", prompt)

    def test_the_finding_records_what_was_shared(self):
        with TemporaryDirectory() as root:
            out = research.research_via_cli({"title": "x", "course": "SC2005"},
                                            download_root=root, runner=Runner(),
                                            material_specs=self.spec(root))
        self.assertEqual(out["materials"], ["Lecture 1.txt"])

    def test_nothing_is_shared_when_no_specs_are_passed(self):
        with TemporaryDirectory() as root:
            runner = Runner()
            out = research.research_via_cli({"title": "x"}, download_root=root,
                                            runner=runner)
        self.assertEqual(out["materials"], [])
        self.assertNotIn("Read", runner.cmd)

    def test_material_never_outlives_the_run(self):
        with TemporaryDirectory() as root:
            research.research_via_cli({"title": "x", "course": "SC2005"},
                                      download_root=root, runner=Runner(),
                                      material_specs=self.spec(root))
            staged = os.path.join(research.sandbox_dir(root), "materials")
            self.assertFalse(os.path.exists(staged))

    def test_material_is_cleaned_up_even_when_the_run_fails(self):
        import subprocess as sp
        with TemporaryDirectory() as root:
            runner = Runner(raises=sp.TimeoutExpired(cmd="claude", timeout=1))
            with self.assertRaises(research.ResearchError):
                research.research_via_cli({"title": "x", "course": "SC2005"},
                                          download_root=root, runner=runner,
                                          material_specs=self.spec(root))
            self.assertFalse(os.path.exists(
                os.path.join(research.sandbox_dir(root), "materials")))

    def test_the_api_backend_shares_no_files(self):
        out = research.research({"title": "x"}, client=FakeClient(ok_message()),
                                material_specs=[{"name": "Lecture 1.txt"}])
        self.assertEqual(out["materials"], [])


class TestBackendChoice(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("INTUITION_RESEARCH_BACKEND", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["INTUITION_RESEARCH_BACKEND"] = self._env
        else:
            os.environ.pop("INTUITION_RESEARCH_BACKEND", None)

    def test_an_explicit_choice_wins(self):
        self.assertEqual(research.resolve_backend("api"), "api")
        self.assertEqual(research.resolve_backend("cli"), "cli")

    def test_the_environment_can_pin_it(self):
        os.environ["INTUITION_RESEARCH_BACKEND"] = "api"
        self.assertEqual(research.resolve_backend(), "api")

    def test_nonsense_falls_through_to_detection(self):
        self.assertIn(research.resolve_backend("banana"),
                      (research.BACKEND_OMNIROUTE, research.BACKEND_CLI,
                       research.BACKEND_API, None))

    def test_a_runner_routes_to_the_cli_and_a_client_to_the_api(self):
        with TemporaryDirectory() as root:
            viacli = research.research({"title": "x"}, download_root=root,
                                       runner=Runner())
            self.assertEqual(viacli["backend"], "cli")
        viaapi = research.research({"title": "x"}, client=FakeClient(ok_message()))
        self.assertEqual(viaapi["backend"], "api")


class TestKey(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._env
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_environment_wins_over_the_saved_file(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-env"
        self.assertEqual(research.load_key(), "sk-ant-from-env")

    def test_blank_key_is_rejected(self):
        with self.assertRaises(ValueError):
            research.save_key("   ")

    def test_missing_key_file_is_not_an_error(self):
        with TemporaryDirectory() as home:
            original, dirs = research.CONFIG_DIR, research._PROFILE_DIRS
            research.CONFIG_DIR = os.path.join(home, ".intuition")
            research._PROFILE_DIRS = [os.path.join(home, "no-profile")]
            try:
                self.assertIsNone(research.load_key())
                self.assertIsNone(research.credential_source())
                self.assertFalse(research.configured())
            finally:
                research.CONFIG_DIR, research._PROFILE_DIRS = original, dirs

    def test_an_ant_login_profile_counts_as_configured(self):
        """No static key needed: the SDK resolves an `ant auth login` profile itself."""
        with TemporaryDirectory() as home:
            original, dirs = research.CONFIG_DIR, research._PROFILE_DIRS
            profile = os.path.join(home, "credentials")
            os.makedirs(profile)
            open(os.path.join(profile, "default.json"), "w").write("{}")
            research.CONFIG_DIR = os.path.join(home, ".intuition")
            research._PROFILE_DIRS = [profile]
            try:
                self.assertIsNone(research.load_key())
                self.assertTrue(research.configured())
                self.assertEqual(research.credential_source(),
                                 "ant auth login profile")
            finally:
                research.CONFIG_DIR, research._PROFILE_DIRS = original, dirs

    def test_the_source_is_named_never_valued(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-secret-value"
        self.assertEqual(research.credential_source(), "ANTHROPIC_API_KEY")
        self.assertNotIn("secret", research.credential_source())


if __name__ == "__main__":
    unittest.main()


class TestVendoredBridge(unittest.TestCase):
    """Any vendored copy of the bridge must match the source. A copy that drifts
    is how a security flag gets silently dropped from one caller."""

    def test_the_vendored_copies_match_the_source(self):
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(root, "tools"))
        try:
            import check_vendored
        except ImportError:
            self.skipTest("tools/check_vendored.py not present")
        drifted, missing = check_vendored.check()
        if missing and not drifted:
            self.skipTest("no sibling project checked out: {}".format(missing))
        self.assertEqual(drifted, [], "vendored copy drifted; run tools/check_vendored.py --sync")

    def test_a_no_tools_run_passes_an_empty_tool_set(self):
        """Triage is pure classification - it must get no tools at all."""
        from intuition import claude_bridge
        cmd = claude_bridge.build_command("p", tools=(), json_schema='{"type":"object"}')
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowed-tools", cmd)
        self.assertIn("--json-schema", cmd)
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config",
                     "--disable-slash-commands"):
            self.assertIn(flag, cmd)

    def test_untrusted_text_can_be_kept_out_of_argv(self):
        from intuition import claude_bridge
        cmd = claude_bridge.build_command("SECRET EMAIL BODY", prompt_on_stdin=True)
        self.assertNotIn("SECRET EMAIL BODY", " ".join(cmd))
        self.assertEqual(cmd[1], "-p")
