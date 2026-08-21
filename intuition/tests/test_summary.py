"""Tests for Compendium's corpus assembly, prompt building, and the compile-and-
repair orchestration. No model and no LaTeX toolchain in the loop - every AI and
latex.py call is mocked, per docs/compendium.md's own testability requirement for
these two build steps.
"""
import unittest
from unittest.mock import patch

from intuition import ai_provider, drive, latex, summary


def _ok_body(text="\\section{X}\nSome content.\n"):
    return "% BEGIN BODY\n{}\n% END BODY".format(text)


class TestAssembleCorpus(unittest.TestCase):
    @patch("intuition.drive.extract_learning_pages")
    def test_material_scope_only_reads_ring_one(self, mock_pages):
        mock_pages.return_value = [(1, "intro"), (2, "more")]
        corpus = summary.assemble_corpus(
            "x.pdf", "Lecture 1", summary.SCOPE_MATERIAL,
            note_text="ignored", chat_turns=[{"role": "user", "content": "ignored"}])
        self.assertEqual(corpus.pages, [(1, "intro"), (2, "more")])
        self.assertEqual(corpus.note_text, "")
        self.assertEqual(corpus.chat_turns, [])
        self.assertEqual(corpus.siblings, [])

    @patch("intuition.drive.extract_learning_pages")
    def test_notes_scope_includes_ring_two_but_not_three(self, mock_pages):
        mock_pages.return_value = [(1, "intro")]
        corpus = summary.assemble_corpus(
            "x.pdf", "Lecture 1", summary.SCOPE_NOTES,
            note_text="my note", chat_turns=[{"role": "user", "content": "q"}],
            course="SC2005")
        self.assertEqual(corpus.note_text, "my note")
        self.assertEqual(corpus.chat_turns, [{"role": "user", "content": "q"}])
        self.assertEqual(corpus.siblings, [])  # no materials.select call at all

    @patch("intuition.materials.clear")
    @patch("intuition.materials.stage_dir", return_value="/sandbox/materials")
    @patch("intuition.drive.extract_learning_pages")
    @patch("intuition.materials.stage")
    @patch("intuition.materials.select")
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

    @patch("intuition.materials.clear")
    @patch("intuition.materials.stage_dir", return_value="/sandbox/materials")
    @patch("intuition.drive.extract_learning_pages")
    @patch("intuition.materials.stage")
    @patch("intuition.materials.select")
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


class TestCleanText(unittest.TestCase):
    def test_collapses_whitespace_before_punctuation(self):
        self.assertEqual(summary._clean_text("word , next .\n"), "word, next.\n")

    def test_literal_ellipsis_becomes_ldots(self):
        self.assertEqual(summary._clean_text("wait for it..."), "wait for it\\ldots{}")

    def test_doubled_spaces_are_collapsed(self):
        self.assertEqual(summary._clean_text("two  spaces   here"), "two spaces here")

    def test_verbatim_content_is_left_untouched(self):
        body = "\\begin{verbatim}\ndef f(x):\n    return x  ,  y...\n\\end{verbatim}\n"
        self.assertEqual(summary._clean_text(body), body)

    def test_prose_around_a_verbatim_block_is_still_cleaned(self):
        body = "two  spaces\n\\begin{verbatim}\nkeep  this\n\\end{verbatim}\nmore  spaces"
        cleaned = summary._clean_text(body)
        self.assertIn("two spaces", cleaned)
        self.assertIn("keep  this", cleaned)   # untouched inside verbatim
        self.assertIn("more spaces", cleaned)

    def test_lstlisting_content_is_left_untouched(self):
        body = "\\begin{lstlisting}[language=Python]\nx  =  1  ,  2\n\\end{lstlisting}\n"
        self.assertEqual(summary._clean_text(body), body)


class TestExtractReport(unittest.TestCase):
    def test_extracts_between_markers(self):
        text = ("% BEGIN BODY\n\\section{X}\n% END BODY\n\n"
                "%% BEGIN REPORT\nPages: 3/3.\n%% END REPORT")
        self.assertEqual(summary._extract_report(text), "Pages: 3/3.")

    def test_missing_report_returns_empty_string_rather_than_raising(self):
        self.assertEqual(
            summary._extract_report("% BEGIN BODY\n\\section{X}\n% END BODY"), "")


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
        patcher = patch("intuition.drive.extract_learning_pages",
                        return_value=[(1, "material text")])
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
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

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
    def test_success_surfaces_the_composing_calls_self_report(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(
            _ok_body() + "\n\n%% BEGIN REPORT\nPages: 1/1. No figures.\n%% END REPORT")
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertEqual(result.report, "Pages: 1/1. No figures.")

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
    def test_a_repair_turns_reply_does_not_overwrite_the_composing_report(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.side_effect = [
            _tier_result(_ok_body("bad %") +
                        "\n\n%% BEGIN REPORT\nComposing report.\n%% END REPORT"),
            _tier_result(_ok_body()),  # a repair reply carries no report at all
        ]
        mock_lint.side_effect = [["line 1: bad"], []]
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertEqual(result.report, "Composing report.")

    @patch("intuition.latex.lint", return_value=["line 1: bad"])
    @patch("intuition.ai_provider.complete_tier")
    def test_failure_leaves_report_empty(self, mock_complete, mock_lint):
        mock_complete.return_value = _tier_result(
            _ok_body() + "\n\n%% BEGIN REPORT\nshould not surface\n%% END REPORT")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.report, "")

    @patch("intuition.drive.rasterize_pages")
    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
    def test_composing_call_gets_page_images_but_repair_call_does_not(
            self, mock_complete, mock_lint, mock_compile, mock_rasterize):
        mock_rasterize.return_value = [(1, b"\x89PNG\r\n\x1a\nfirst"),
                                       (2, b"\x89PNG\r\n\x1a\nsecond")]
        mock_complete.side_effect = [
            _tier_result(_ok_body("bad %")),          # composing: fails lint
            _tier_result(_ok_body()),                 # repair: clean
        ]
        # First call fails lint, second (the repair) doesn't - see lint's own
        # side_effect wiring in the lint-repair tests below for the pattern.
        mock_lint.side_effect = [["line 1: bad"], []]
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        summary.generate("x.pdf", "L1", "summarise", "NTU",
                         scope=summary.SCOPE_MATERIAL)
        composing_kwargs = mock_complete.call_args_list[0].kwargs
        repair_kwargs = mock_complete.call_args_list[1].kwargs
        self.assertEqual(composing_kwargs["images"], [b"\x89PNG\r\n\x1a\nfirst",
                                                       b"\x89PNG\r\n\x1a\nsecond"])
        self.assertNotIn("images", repair_kwargs)

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
    def test_success_reports_which_pages_were_actually_cited(
            self, mock_complete, mock_lint, mock_compile):
        mock_complete.return_value = _tier_result(_ok_body(
            "\\srcref{L1}{3} a claim\n\\srcref{L1}{1} another claim"))
        mock_compile.return_value = latex.CompileResult(
            ok=True, pdf=b"%PDF-x", tex="doc", log="", errors=[])
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertEqual(result.pages_cited, [1, 3])

    @patch("intuition.ai_provider.complete_tier")
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

    @patch("intuition.latex.lint", return_value=["line 1: bad"])
    @patch("intuition.ai_provider.complete_tier")
    def test_failure_reports_no_pages_cited(self, mock_complete, mock_lint):
        mock_complete.return_value = _tier_result(_ok_body())
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.pages_cited, [])

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint")
    @patch("intuition.ai_provider.complete_tier")
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

    @patch("intuition.latex.lint", return_value=["line 1: bad"])
    @patch("intuition.ai_provider.complete_tier")
    def test_lint_failure_persisting_after_repair_gives_up_without_compiling(
            self, mock_complete, mock_lint):
        mock_complete.return_value = _tier_result(_ok_body())
        with patch("intuition.latex.compile") as mock_compile:
            result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                      scope=summary.SCOPE_MATERIAL)
            mock_compile.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "lint")
        self.assertIn("line 1: bad", result.errors)

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
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

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
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

    @patch("intuition.ai_provider.complete_tier")
    def test_provider_error_on_first_pass_fails_cleanly(self, mock_complete):
        mock_complete.side_effect = ai_provider.ProviderError("no backend available")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "generate")
        self.assertIn("no backend available", result.errors[0])

    @patch("intuition.ai_provider.complete_tier")
    def test_missing_body_markers_fails_cleanly(self, mock_complete):
        mock_complete.return_value = _tier_result("I refuse to use the marker format.")
        result = summary.generate("x.pdf", "L1", "summarise", "NTU",
                                  scope=summary.SCOPE_MATERIAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "generate")

    @patch("intuition.latex.compile")
    @patch("intuition.latex.lint", return_value=[])
    @patch("intuition.ai_provider.complete_tier")
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
