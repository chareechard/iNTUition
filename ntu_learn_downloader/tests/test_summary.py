"""Tests for Compendium's corpus assembly, prompt building, and the compile-and-
repair orchestration. No model and no LaTeX toolchain in the loop - every AI and
latex.py call is mocked, per docs/compendium.md's own testability requirement for
these two build steps.
"""
import unittest
from unittest.mock import patch

from ntu_learn_downloader import ai_provider, drive, latex, summary


def _ok_body(text="\\section{X}\nSome content.\n"):
    return "% BEGIN BODY\n{}\n% END BODY".format(text)


class TestAssembleCorpus(unittest.TestCase):
    @patch("ntu_learn_downloader.drive.extract_learning_pages")
    def test_material_scope_only_reads_ring_one(self, mock_pages):
        mock_pages.return_value = [(1, "intro"), (2, "more")]
        corpus = summary.assemble_corpus(
            "x.pdf", "Lecture 1", summary.SCOPE_MATERIAL,
            note_text="ignored", chat_turns=[{"role": "user", "content": "ignored"}])
        self.assertEqual(corpus.pages, [(1, "intro"), (2, "more")])
        self.assertEqual(corpus.note_text, "")
        self.assertEqual(corpus.chat_turns, [])
        self.assertEqual(corpus.siblings, [])

    @patch("ntu_learn_downloader.drive.extract_learning_pages")
    def test_notes_scope_includes_ring_two_but_not_three(self, mock_pages):
        mock_pages.return_value = [(1, "intro")]
        corpus = summary.assemble_corpus(
            "x.pdf", "Lecture 1", summary.SCOPE_NOTES,
            note_text="my note", chat_turns=[{"role": "user", "content": "q"}],
            course="SC2005")
        self.assertEqual(corpus.note_text, "my note")
        self.assertEqual(corpus.chat_turns, [{"role": "user", "content": "q"}])
        self.assertEqual(corpus.siblings, [])  # no materials.select call at all

    @patch("ntu_learn_downloader.materials.clear")
    @patch("ntu_learn_downloader.materials.stage_dir", return_value="/sandbox/materials")
    @patch("ntu_learn_downloader.drive.extract_learning_pages")
    @patch("ntu_learn_downloader.materials.stage")
    @patch("ntu_learn_downloader.materials.select")
    def test_topic_scope_stages_and_clears_siblings(
            self, mock_select, mock_stage, mock_pages, _mock_dir, mock_clear):
        mock_select.return_value = [{"rel": "wk1/slides.pdf", "local": None,
                                     "drive_id": "d1", "size": 100}]
        mock_stage.return_value = [{"name": "slides.pdf", "rel": "wk1/slides.pdf"}]
        mock_pages.side_effect = [
            [(1, "material text")],   # the open material itself
            [(1, "sibling text")],    # the staged sibling
        ]
        corpus = summary.assemble_corpus(
            "x.pdf", "Lecture 1", summary.SCOPE_TOPIC, course="SC2005",
            download_root="NTU")
        self.assertEqual(corpus.siblings, [("wk1/slides.pdf", [(1, "sibling text")])])
        mock_clear.assert_called_once()  # staged copies never outlive the run

    @patch("ntu_learn_downloader.materials.clear")
    @patch("ntu_learn_downloader.materials.stage_dir", return_value="/sandbox/materials")
    @patch("ntu_learn_downloader.drive.extract_learning_pages")
    @patch("ntu_learn_downloader.materials.stage")
    @patch("ntu_learn_downloader.materials.select")
    def test_topic_scope_clears_siblings_even_if_extraction_raises(
            self, mock_select, mock_stage, mock_pages, _mock_dir, mock_clear):
        mock_select.return_value = [{"rel": "a.pdf"}]
        mock_stage.return_value = [{"name": "a.pdf", "rel": "a.pdf"}]
        mock_pages.side_effect = [
            [(1, "material text")],
            drive.DriveError("boom"),
        ]
        summary.assemble_corpus("x.pdf", "L1", summary.SCOPE_TOPIC, course="SC2005",
                                download_root="NTU")
        mock_clear.assert_called_once()

    def test_unknown_scope_raises(self):
        with self.assertRaises(ValueError):
            summary.assemble_corpus("x.pdf", "L1", "everything")


class TestBuildPrompt(unittest.TestCase):
    def test_material_scope_omits_notes_and_siblings_sections(self):
        corpus = summary.Corpus("Lecture 1", [(1, "text")], "a note", [{"role": "user", "content": "q"}], [])
        prompt = summary.build_prompt("summarise it", corpus, summary.SCOPE_MATERIAL)
        self.assertIn("[[p.1]]", prompt)
        self.assertNotIn("a note", prompt)
        self.assertNotIn("FRIDAY conversation", prompt)

    def test_notes_scope_includes_note_and_chat(self):
        corpus = summary.Corpus("Lecture 1", [(1, "text")], "a note",
                                [{"role": "user", "content": "q"}], [])
        prompt = summary.build_prompt("summarise it", corpus, summary.SCOPE_NOTES)
        self.assertIn("a note", prompt)
        self.assertIn("User: q", prompt)

    def test_topic_scope_includes_siblings(self):
        corpus = summary.Corpus("Lecture 1", [(1, "text")], "", [],
                                [("wk2/notes.pdf", [(1, "sibling")])])
        prompt = summary.build_prompt("summarise it", corpus, summary.SCOPE_TOPIC)
        self.assertIn("wk2/notes.pdf", prompt)
        self.assertIn("sibling", prompt)

    def test_the_users_request_is_always_present(self):
        corpus = summary.Corpus("L1", [(1, "t")], "", [], [])
        prompt = summary.build_prompt("just the deadlock part", corpus, summary.SCOPE_MATERIAL)
        self.assertIn("just the deadlock part", prompt)


class TestExtractBody(unittest.TestCase):
    def test_extracts_between_markers(self):
        text = "some preamble chatter\n% BEGIN BODY\n\\section{X}\n% END BODY\ntrailing"
        self.assertEqual(summary._extract_body(text), "\\section{X}")

    def test_missing_markers_raises(self):
        with self.assertRaises(summary.SummaryError):
            summary._extract_body("just prose, no markers")

    def test_missing_end_marker_with_no_finish_reason_gives_generic_message(self):
        with self.assertRaisesRegex(summary.SummaryError, "did not contain"):
            summary._extract_body("% BEGIN BODY\n\\section{X}\nno closing marker")

    def test_missing_end_marker_from_a_length_cutoff_names_the_real_cause(self):
        # A reply cut off by max_tokens looks identical to a malformed reply - both
        # are missing % END BODY - but the backend's finish_reason distinguishes
        # them, and the message shown to the user should say which one happened.
        for reason in ("length", "max_tokens"):
            with self.assertRaisesRegex(summary.SummaryError, "length limit"):
                summary._extract_body("% BEGIN BODY\n\\section{X}\ncut off mid-", reason)


class TestRepairPrompt(unittest.TestCase):
    def test_names_the_stage_and_the_errors(self):
        prompt = summary._build_repair_prompt("\\section{X}", "lint", ["line 1: bad"])
        self.assertIn("lint", prompt)
        self.assertIn("line 1: bad", prompt)
        self.assertIn("\\section{X}", prompt)


class TestPagesCited(unittest.TestCase):
    def test_collects_and_dedupes_and_sorts_cited_pages(self):
        body = (
            "\\srcref{Lecture 4}{12} some claim\n"
            "\\srcref{Lecture 4}{7} another claim\n"
            "\\srcref{Lecture 4}{12} repeated page\n"
        )
        self.assertEqual(summary._pages_cited(body), [7, 12])

    def test_no_citations_returns_empty(self):
        self.assertEqual(summary._pages_cited("\\section{X}\nplain prose\n"), [])


def _tier_result(text, backend="omniroute", model="claude-opus-5", rung="claude/claude-opus-5"):
    return {"text": text, "backend": backend, "model": model, "rung": rung}


class TestGenerate(unittest.TestCase):
    def setUp(self):
        patcher = patch("ntu_learn_downloader.drive.extract_learning_pages",
                        return_value=[(1, "material text")])
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint", return_value=[])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_clean_first_pass_succeeds_with_no_repair_turns(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(_ok_body())
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertTrue(result.ok)
        self.assertEqual(result.pdf, b"%PDF-x")
        self.assertEqual(mock_complete.call_count, 1)  # no repair turn spent
        self.assertEqual(result.backend, "omniroute")
        self.assertEqual(result.rung, "claude/claude-opus-5")

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint", return_value=[])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_success_reports_which_pages_were_actually_cited(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(_ok_body(
            "\\srcref{L1}{3} a claim\n\\srcref{L1}{1} another claim"))
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertEqual(result.pages_cited, [1, 3])

    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_truncated_first_pass_names_the_length_limit_not_a_format_error(
            self, mock_complete):
        # Regression test: a real run against a 24-page lecture deck reproducibly
        # hit finish_reason "length" a few hundred tokens before % END BODY, and
        # the resulting error told the user the model "didn't follow the format" -
        # true of the symptom, false of the cause. The stage/error shown for a
        # length cutoff must name the real cause.
        mock_complete.return_value = dict(
            _tier_result("% BEGIN BODY\n\\section{X}\ncut off mid-sen"),
            finish_reason="length")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "generate")
        self.assertTrue(any("length limit" in e for e in result.errors), result.errors)

    @patch("ntu_learn_downloader.latex.lint", return_value=["line 1: bad"])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_failure_reports_no_pages_cited(self, mock_complete, mock_lint):
        mock_complete.return_value = _tier_result(_ok_body())
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.pages_cited, [])

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint")
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_lint_failure_triggers_one_repair_turn_then_succeeds(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.side_effect = [
            _tier_result(_ok_body("bad %")),
            _tier_result(_ok_body("fixed")),
        ]
        mock_lint.side_effect = [["line 1: unescaped %"], []]
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertTrue(result.ok)
        self.assertEqual(mock_complete.call_count, 2)
        # the repair turn was told exactly what was wrong
        repair_prompt = mock_complete.call_args_list[1].args[1]
        self.assertIn("unescaped %", repair_prompt)

    @patch("ntu_learn_downloader.latex.lint", return_value=["line 1: bad"])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_lint_failure_persisting_after_repair_gives_up_without_compiling(
            self, mock_complete, mock_lint):
        mock_complete.return_value = _tier_result(_ok_body())
        with patch("ntu_learn_downloader.latex.compile") as mock_compile:
            result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                      scope=summary.SCOPE_MATERIAL)
            mock_compile.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "lint")
        self.assertIn("line 1: bad", result.errors)

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint", return_value=[])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_compile_failure_triggers_one_repair_turn_then_succeeds(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.side_effect = [
            _tier_result(_ok_body("broken")),
            _tier_result(_ok_body("fixed")),
        ]
        mock_compile.side_effect = [
            latex.CompileResult(ok=False, pdf=None, tex="doc", log="",
                                errors=["! Undefined control sequence."]),
            latex.CompileResult(ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[]),
        ]
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertTrue(result.ok)
        self.assertEqual(mock_compile.call_count, 2)
        repair_prompt = mock_complete.call_args_list[1].args[1]
        self.assertIn("Undefined control sequence", repair_prompt)

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint", return_value=[])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_compile_failure_persisting_after_repair_hands_back_tex_and_log(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(_ok_body())
        mock_compile.return_value = latex.CompileResult(
            ok=False, pdf=None, tex="doc", log="boom",
            errors=["! Still broken."])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "compile")
        self.assertIsNotNone(result.tex)
        self.assertIsNone(result.pdf)
        self.assertIn("! Still broken.", result.errors)
        # never drop content to make it compile: no more than the two repair turns
        self.assertEqual(mock_complete.call_count, 1 + 1)

    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_provider_error_on_first_pass_fails_cleanly(self, mock_complete):
        mock_complete.side_effect = ai_provider.ProviderError("no backend available")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "generate")
        self.assertIn("no backend available", result.errors[0])

    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_missing_body_markers_fails_cleanly(self, mock_complete):
        mock_complete.return_value = _tier_result("I refuse to use the marker format.")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "generate")

    @patch("ntu_learn_downloader.latex.compile")
    @patch("ntu_learn_downloader.latex.lint", return_value=[])
    @patch("ntu_learn_downloader.ai_provider.complete_tier")
    def test_on_stage_callback_is_fed_progress(self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(_ok_body())
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        stages = []
        summary.generate("x.pdf", "L1", "summarise", "NTU",
                         scope=summary.SCOPE_MATERIAL, on_stage=stages.append)
        self.assertIn("Compiling", stages)
        self.assertTrue(any("Composing" in s for s in stages))


if __name__ == "__main__":
    unittest.main()
