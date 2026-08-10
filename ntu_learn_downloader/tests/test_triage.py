import json
import unittest
from tempfile import TemporaryDirectory

from ntu_learn_downloader import inbound, triage, triage_store


class FakeProc:
    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.stderr = ""
        self.returncode = 0


class Runner:
    def __init__(self, result, denials=None):
        self.result = result
        self.denials = denials or []
        self.cmd = None
        self.kwargs = None

    def __call__(self, cmd, **kw):
        self.cmd, self.kwargs = cmd, kw
        return FakeProc({
            "is_error": False, "subtype": "success",
            "result": json.dumps(self.result),
            "permission_denials": [{"tool_name": t} for t in self.denials],
            "total_cost_usd": 0.003,
            "modelUsage": {"claude-opus-5": {"outputTokens": 90}},
        })


def answer(priority="High", confidence=0.8, **kw):
    base = {"priority": priority, "confidence": confidence,
            "matched_snippet": "renewal form due 15 Aug",
            "reasoning": "Direct action required.",
            "action_items": ["Submit the form"], "due": "2026-08-15"}
    base.update(kw)
    return base


def email(sender="spms-undergrad@ntu.edu.sg", subject="Scholarship renewal",
          body="Please submit the renewal form by 15 Aug."):
    return {"email_id": "e1", "sender": sender, "subject": subject,
            "body_content": body, "timestamp": "2026-08-10"}


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        self.pattern = triage.compile_keywords(["Scholarship", "URECA"])
        self.watched = ["spms-undergrad@ntu.edu.sg"]

    def test_a_watched_sender_always_passes(self):
        self.assertTrue(triage.prefilter(
            email(subject="anything", body="nothing relevant"),
            self.pattern, self.watched))

    def test_a_keyword_passes(self):
        self.assertTrue(triage.prefilter(
            email(sender="random@x.test", body="URECA applications open"),
            self.pattern, self.watched))

    def test_unrelated_mail_is_filtered_out(self):
        self.assertFalse(triage.prefilter(
            email(sender="clubs@ntu.edu.sg", subject="Hostel check-in",
                  body="Parking notice"), self.pattern, self.watched))

    def test_a_keyword_only_in_boilerplate_does_not_pass(self):
        """The point of the prefilter: a footer match is not a real match."""
        body = ("Weekly club newsletter, nothing relevant.\n"
                "Unsubscribe if you no longer wish to hear about Scholarship news.")
        self.assertFalse(triage.prefilter(
            email(sender="clubs@ntu.edu.sg", subject="Newsletter", body=body),
            self.pattern, self.watched))

    def test_no_configuration_lets_everything_through(self):
        self.assertTrue(triage.prefilter(email(), None, []))

    def test_keywords_match_whole_words_only(self):
        pattern = triage.compile_keywords(["AI"])
        self.assertFalse(triage.prefilter(
            email(sender="x@y.test", subject="Chair", body="maintain the said plan"),
            pattern, []))


class TestNormalise(unittest.TestCase):
    def test_low_confidence_downgrades_a_critical(self):
        self.assertEqual(
            triage.normalise(answer(priority="Critical", confidence=0.4))["priority"],
            "High")

    def test_low_confidence_downgrades_a_high(self):
        self.assertEqual(
            triage.normalise(answer(priority="High", confidence=0.3))["priority"],
            "Medium")

    def test_confident_calls_are_left_alone(self):
        self.assertEqual(
            triage.normalise(answer(priority="Critical", confidence=0.9))["priority"],
            "Critical")

    def test_a_nonsense_priority_falls_to_low(self):
        self.assertEqual(triage.normalise(answer(priority="URGENT!!"))["priority"],
                         "Low")

    def test_a_nonsense_confidence_does_not_raise(self):
        self.assertEqual(triage.normalise(answer(confidence="very"))["confidence"], 0.0)


class TestAnalyse(unittest.TestCase):
    def test_the_model_call_carries_no_tools(self):
        runner = Runner(answer())
        triage.analyse(email(), sandbox=".", runner=runner)
        self.assertEqual(runner.cmd[runner.cmd.index("--tools") + 1], "")
        self.assertIn("--json-schema", runner.cmd)
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, runner.cmd)

    def test_the_email_body_never_reaches_argv(self):
        runner = Runner(answer())
        triage.analyse(email(body="SENSITIVE CONTENT"), sandbox=".", runner=runner)
        self.assertNotIn("SENSITIVE CONTENT", " ".join(runner.cmd))
        self.assertIn("SENSITIVE CONTENT", runner.kwargs["input"])

    def test_an_enormous_body_is_truncated(self):
        runner = Runner(answer())
        triage.analyse(email(body="x" * 50000), sandbox=".", runner=runner)
        self.assertLess(len(runner.kwargs["input"]), triage.MAX_BODY_CHARS + 2000)

    def test_the_prompt_marks_the_body_as_untrusted(self):
        """An email that tries to instruct the model must be framed as evidence."""
        self.assertIn("untrusted text written by a third party",
                      triage.SYSTEM_PROMPT)

    def test_a_failed_run_returns_a_fallback_not_an_exception(self):
        def boom(cmd, **kw):
            raise OSError("no such binary")
        out = triage.analyse(email(), sandbox=".", runner=boom)
        self.assertFalse(out["ok"])
        self.assertEqual(out["priority"], "Low")

    def test_unparseable_output_returns_a_fallback(self):
        def bad(cmd, **kw):
            return FakeProc({"is_error": False, "subtype": "success",
                             "result": "not json", "permission_denials": []})
        self.assertFalse(triage.analyse(email(), sandbox=".", runner=bad)["ok"])


class TestBatch(unittest.TestCase):
    def test_only_survivors_cost_a_model_call(self):
        calls = []

        class Counting(Runner):
            def __call__(self, cmd, **kw):
                calls.append(kw.get("input", ""))
                return super(Counting, self).__call__(cmd, **kw)

        emails = [email(),
                  email(sender="clubs@ntu.edu.sg", subject="Parking", body="notice")]
        config = {"keywords": ["Scholarship"],
                  "watched_senders": ["spms-undergrad@ntu.edu.sg"]}
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            flags = triage.run_batch(emails, config, sandbox=root, store=store,
                                     runner=Counting(answer()))
        self.assertEqual(len(calls), 1)   # the parking notice never reached a model
        self.assertEqual(len(flags), 1)

    def test_low_priority_results_are_not_flagged(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            flags = triage.run_batch([email()], {}, sandbox=root, store=store,
                                     runner=Runner(answer(priority="Low")))
            self.assertEqual(flags, [])
            self.assertEqual(len(store), 0)


class TestStore(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            store.record({"email_id": "e1", "sender": "s", "subject": "sub",
                          "priority": "High", "action_items": ["do it"],
                          "confidence": 0.8, "due": "2026-08-15"})
            rows = store.list_open()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action_items"], "do it")
            self.assertEqual(rows[0]["due"], "2026-08-15")

    def test_retriaging_updates_rather_than_duplicates(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            store.record({"email_id": "e1", "priority": "Medium"})
            store.record({"email_id": "e1", "priority": "Critical"})
            self.assertEqual(len(store), 1)
            self.assertEqual(store.list_open()[0]["priority"], "Critical")

    def test_mark_done_removes_it_from_the_open_list(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            store.record({"email_id": "e1", "priority": "High"})
            self.assertTrue(store.mark_done("e1"))
            self.assertEqual(store.list_open(), [])
            self.assertFalse(store.mark_done("e1"))

    def test_known_ids_lets_a_rerun_skip_work(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            store.record({"email_id": "e1", "priority": "High"})
            self.assertEqual(store.known_ids(), {"e1"})

    def test_the_reader_prefers_this_store_over_cerberus(self):
        with TemporaryDirectory() as root:
            store = triage_store.TriageStore(root)
            store.record({"email_id": "e1", "sender": "spms@ntu.edu.sg",
                          "subject": "Scholarship renewal", "priority": "High",
                          "action_items": ["Submit form"], "confidence": 0.8})
            self.assertEqual(inbound.resolve_path(root), store.path)
            flags = inbound.open_flags(store.path)
            self.assertEqual(flags[0]["title"], "Scholarship renewal")
            self.assertEqual(flags[0]["actions"], ["Submit form"])

    def test_an_explicit_path_beats_everything(self):
        with TemporaryDirectory() as root:
            triage_store.TriageStore(root)
            self.assertEqual(inbound.resolve_path(root, r"X:\given.db"),
                             r"X:\given.db")


if __name__ == "__main__":
    unittest.main()
