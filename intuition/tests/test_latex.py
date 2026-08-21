"""Tests for the LaTeX lint/assemble/compile primitives behind Compendium.

lint/assemble/extract_errors are pure functions and run unconditionally. compile()
shells out to a real pdflatex binary, so its unit tests mock subprocess.run rather
than depending on a LaTeX distribution being installed; one integration test at the
bottom exercises the real binary and skips itself when none is on PATH.
"""
import os
import subprocess
import unittest
from unittest.mock import patch

from intuition import latex


class TestLint(unittest.TestCase):
    def test_clean_body_has_no_violations(self):
        body = "\\section{Intro}\nPlain prose with \\(x^2\\) inline maths.\n"
        self.assertEqual(latex.lint(body), [])

    def test_forbidden_commands_are_named_with_their_line(self):
        body = "line one\n\\input{secrets.tex}\n"
        violations = latex.lint(body)
        self.assertTrue(any("line 2" in v and "\\input" in v for v in violations))

    def test_write18_is_flagged_even_with_internal_spacing(self):
        self.assertTrue(any("shell-escape" in v for v in latex.lint("\\write18{ls}")))

    def test_include_is_forbidden(self):
        self.assertTrue(any("\\include" in v for v in latex.lint("\\include{x}")))

    def test_includegraphics_is_forbidden(self):
        """Only \\srcfig{N}{caption} may place an image - \\includegraphics reaches
        an arbitrary filename, so a body may never use it directly."""
        violations = latex.lint("\\includegraphics{whatever}")
        self.assertTrue(any("\\includegraphics" in v for v in violations))

    def test_tikzpicture_does_not_trip_the_lint(self):
        body = ("\\begin{tikzpicture}\n"
               "\\node[draw] (a) {Start};\n"
               "\\node[draw, right=2cm of a] (b) {End};\n"
               "\\draw[-Stealth] (a) -- (b);\n"
               "\\end{tikzpicture}\n")
        self.assertEqual(latex.lint(body), [])

    def test_srcfig_call_does_not_trip_the_lint(self):
        self.assertEqual(latex.lint("\\srcfig{1}{A labelled diagram.}"), [])

    def test_unescaped_percent_is_flagged(self):
        violations = latex.lint("100% of the class passed\n")
        self.assertTrue(any("unescaped %" in v for v in violations))

    def test_escaped_percent_is_not_flagged(self):
        self.assertEqual(latex.lint("100\\% of the class passed\n"), [])

    def test_unescaped_ampersand_outside_tabular_is_flagged(self):
        violations = latex.lint("Alice & Bob went to lecture\n")
        self.assertTrue(any("unescaped &" in v for v in violations))

    def test_ampersand_inside_tabularx_is_allowed(self):
        body = "\\begin{tabularx}{\\linewidth}{lX}\nAlice & Bob \\\\\n\\end{tabularx}\n"
        self.assertEqual(latex.lint(body), [])

    def test_ampersand_inside_longtable_is_allowed(self):
        body = "\\begin{longtable}{ll}\nAlice & Bob \\\\\n\\end{longtable}\n"
        self.assertEqual(latex.lint(body), [])

    def test_plain_tabular_is_forbidden(self):
        body = "\\begin{tabular}{ll}\nAlice & Bob \\\\\n\\end{tabular}\n"
        violations = latex.lint(body)
        self.assertEqual(len(violations), 1)
        self.assertIn("line 1", violations[0])
        self.assertIn("tabularx", violations[0])

    def test_plain_tabular_star_is_forbidden(self):
        body = "\\begin{tabular*}{\\linewidth}{ll}\nAlice & Bob \\\\\n\\end{tabular*}\n"
        violations = latex.lint(body)
        self.assertEqual(len(violations), 1)
        self.assertIn("tabular*", violations[0])

    def test_ampersand_inside_align_is_allowed(self):
        body = "\\begin{align}\na &= b \\\\\n\\end{align}\n"
        self.assertEqual(latex.lint(body), [])

    def test_unescaped_underscore_outside_math_is_flagged(self):
        violations = latex.lint("the variable foo_bar is undefined\n")
        self.assertTrue(any("underscore" in v for v in violations))

    def test_underscore_inside_inline_math_is_allowed(self):
        self.assertEqual(latex.lint("\\(x_i\\) is the i-th term\n"), [])

    def test_underscore_inside_dollar_math_is_allowed(self):
        self.assertEqual(latex.lint("$x_i$ is the i-th term\n"), [])

    def test_odd_unescaped_dollar_count_is_flagged(self):
        violations = latex.lint("It costs $5 for the textbook\n")
        self.assertTrue(any("odd number" in v for v in violations))

    def test_paired_dollar_math_is_not_flagged_for_the_dollar_check(self):
        violations = latex.lint("$x + y$ is the sum\n")
        self.assertFalse(any("odd number" in v for v in violations))

    def test_escaped_dollar_is_not_counted(self):
        self.assertEqual(latex.lint("It costs \\$5 for the textbook\n"), [])

    def test_special_characters_inside_verbatim_are_not_flagged(self):
        """verbatim/lstlisting are LaTeX's literal-text environments - none of the
        %, &, _, #, $ prose checks apply, the same way they don't apply in real
        LaTeX. A real code sample is full of these unescaped."""
        body = ("\\begin{verbatim}\n"
               "def f(x_1, y): return x_1 & y  # 100% not a comment here\n"
               "\\end{verbatim}\n")
        self.assertEqual(latex.lint(body), [])

    def test_special_characters_inside_lstlisting_are_not_flagged(self):
        body = ("\\begin{lstlisting}[language=Python]\n"
               "cost = \"$5\"  # a & b, x_1\n"
               "\\end{lstlisting}\n")
        self.assertEqual(latex.lint(body), [])

    def test_prose_after_a_verbatim_block_is_still_checked(self):
        """Skipping the raw span must not swallow lint checks for what follows it."""
        body = "\\begin{verbatim}\nx & y\n\\end{verbatim}\n100% wrong outside it\n"
        violations = latex.lint(body)
        self.assertTrue(any("unescaped %" in v for v in violations))
        self.assertTrue(any("line 4" in v for v in violations))

    def test_unclosed_verbatim_is_skipped_to_end_of_body_without_crashing(self):
        body = "\\begin{verbatim}\nx & y, 100% no closing tag\n"
        self.assertEqual(latex.lint(body), [])


class TestAssemble(unittest.TestCase):
    def test_body_is_wrapped_in_the_fixed_preamble(self):
        doc = latex.assemble("\\section{X}\n")
        self.assertIn("\\documentclass", doc)
        self.assertIn("\\begin{document}", doc)
        self.assertIn("\\section{X}", doc)
        self.assertIn("\\end{document}", doc)

    def test_srcref_and_sourcequote_are_defined_in_the_preamble(self):
        self.assertIn("\\newcommand{\\srcref}", latex.PREAMBLE)
        self.assertIn("sourcequote", latex.PREAMBLE)

    def test_srcfig_tikz_and_graphicx_are_defined_in_the_preamble(self):
        self.assertIn("\\newcommand{\\srcfig}", latex.PREAMBLE)
        self.assertIn("\\usepackage{tikz}", latex.PREAMBLE)
        self.assertIn("\\usepackage{graphicx}", latex.PREAMBLE)

    def test_assemble_does_not_choke_on_a_percent_in_the_preamble(self):
        """Regression: assemble() used to be `%`-style ("%s") templating via
        DOCUMENT_TEMPLATE % body, which a stray literal % anywhere in the preamble
        half (a LaTeX comment character, easy to introduce with a \\newcommand{...}{%
        line-continuation idiom) broke with a ValueError before a single body ever
        reached it - exactly what happened adding \\srcfig. assemble() now does plain
        concatenation, so a % on either side is never re-interpreted as a format
        specifier."""
        with patch.object(latex, "_DOCUMENT_HEAD",
                          latex._DOCUMENT_HEAD + "% a stray comment\n"):
            doc = latex.assemble("\\section{X}\n")
        self.assertIn("\\section{X}", doc)
        self.assertIn("\\end{document}", doc)


class TestExtractErrors(unittest.TestCase):
    def test_pulls_bang_lines_with_context(self):
        log = (
            "This is pdfTeX\n"
            "! Undefined control sequence.\n"
            "l.4 \\bogus\n"
            "        command\n"
            "!  ==> Fatal error occurred, no output PDF file produced!\n"
        )
        errors = latex.extract_errors(log)
        self.assertEqual(len(errors), 2)
        self.assertIn("Undefined control sequence", errors[0])
        self.assertIn("l.4", errors[0])

    def test_no_bang_lines_returns_empty(self):
        self.assertEqual(latex.extract_errors("nothing wrong here\n"), [])


class TestToolingStatus(unittest.TestCase):
    def test_reports_ready_when_pdflatex_is_on_path(self):
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"):
            self.assertEqual(latex.tooling_status(), {"pdflatex": True, "ready": True})

    def test_reports_not_ready_when_missing(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual(latex.tooling_status(), {"pdflatex": False, "ready": False})


def _fake_run_writing_pdf(pdf_bytes=b"%PDF-fake"):
    """A subprocess.run stand-in that honours -output-directory by dropping a fake
    PDF there, the way a real pdflatex success would."""
    def run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        out_dir = cmd[cmd.index("-output-directory") + 1]
        with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
            f.write(pdf_bytes)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run


class TestCompile(unittest.TestCase):
    def test_missing_toolchain_fails_without_shelling_out(self):
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run") as mock_run:
            result = latex.compile("\\documentclass{article}\\begin{document}x\\end{document}")
        mock_run.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("pdflatex not found", result.errors[0])

    def test_successful_compile_reads_the_pdf_bytes_back(self):
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=_fake_run_writing_pdf(b"%PDF-1.5 stub")):
            result = latex.compile("doc")
        self.assertTrue(result.ok)
        self.assertEqual(result.pdf, b"%PDF-1.5 stub")
        self.assertEqual(result.errors, [])

    def test_successful_compile_runs_pdflatex_twice_for_toc_and_ref_resolution(self):
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run",
                   side_effect=_fake_run_writing_pdf(b"%PDF-1.5 stub")) as mock_run:
            latex.compile("doc")
        self.assertEqual(mock_run.call_count, 2)

    def test_first_pass_failure_never_spends_a_second_pass(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="! Undefined control sequence.\n", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run) as mock_run:
            result = latex.compile("doc")
        self.assertFalse(result.ok)
        self.assertEqual(mock_run.call_count, 1)

    def test_a_second_pass_failure_is_reported_not_papered_over_by_the_first(self):
        calls = []

        def run(cmd, **kw):
            calls.append(1)
            out_dir = cmd[cmd.index("-output-directory") + 1]
            if len(calls) == 1:
                with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
                    f.write(b"%PDF-first-pass")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 1, stdout="! Undefined control sequence on pass two.\n", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc")
        self.assertFalse(result.ok)
        self.assertTrue(any("pass two" in e for e in result.errors), result.errors)

    def test_second_pass_timeout_still_delivers_the_first_passs_pdf(self):
        calls = []

        def run(cmd, **kw):
            calls.append(1)
            if len(calls) == 1:
                out_dir = cmd[cmd.index("-output-directory") + 1]
                with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
                    f.write(b"%PDF-first-pass")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc")
        self.assertTrue(result.ok)
        self.assertEqual(result.pdf, b"%PDF-first-pass")

    def test_nonzero_exit_extracts_bang_errors_and_returns_no_pdf(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="! Undefined control sequence.\nl.2 \\bogus\n", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc")
        self.assertFalse(result.ok)
        self.assertIsNone(result.pdf)
        self.assertTrue(any("Undefined control sequence" in e for e in result.errors))

    def test_timeout_kills_rather_than_waits_and_reports_cleanly(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc", timeout=5)
        self.assertFalse(result.ok)
        self.assertIsNone(result.pdf)
        self.assertIn("timed out", result.errors[0])

    def test_shell_escape_and_arbitrary_file_io_are_disabled_on_the_invocation(self):
        captured = {}
        def run(cmd, cwd=None, env=None, **kw):
            captured["cmd"] = cmd
            captured["env"] = env
            out_dir = cmd[cmd.index("-output-directory") + 1]
            with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
                f.write(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            latex.compile("doc")
        self.assertIn("-no-shell-escape", captured["cmd"])
        self.assertEqual(captured["env"]["openin_any"], "p")
        self.assertEqual(captured["env"]["openout_any"], "p")

    def test_figures_are_staged_as_numbered_files_before_compiling(self):
        """\\srcfig{N}{...} resolves against fig<N>.<ext> in the sandbox - compile()
        has to have written them before pdflatex ever runs."""
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        jpg = b"\xff\xd8\xff" + b"\x00" * 16
        seen = {}
        def run(cmd, cwd=None, env=None, **kw):
            out_dir = cmd[cmd.index("-output-directory") + 1]
            seen["files"] = sorted(os.listdir(out_dir))
            with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
                f.write(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc", figures=[png, jpg])
        self.assertTrue(result.ok)
        self.assertIn("fig1.png", seen["files"])
        self.assertIn("fig2.jpg", seen["files"])

    def test_unrecognised_figure_bytes_are_skipped_not_fatal(self):
        seen = {}
        def run(cmd, cwd=None, env=None, **kw):
            out_dir = cmd[cmd.index("-output-directory") + 1]
            seen["files"] = sorted(os.listdir(out_dir))
            with open(os.path.join(out_dir, "summary.pdf"), "wb") as f:
                f.write(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch("shutil.which", return_value=r"C:\tex\pdflatex.exe"), \
             patch("subprocess.run", side_effect=run):
            result = latex.compile("doc", figures=[b"not an image"])
        self.assertTrue(result.ok)
        self.assertEqual([f for f in seen["files"] if f.startswith("fig")], [])


@unittest.skipUnless(latex.tooling_status()["ready"],
                     "no LaTeX toolchain on PATH")
class TestCompileIntegration(unittest.TestCase):
    """Exercises the real pdflatex binary when one is available, mirroring the
    manual verification in docs/compendium.md."""

    def test_a_representative_document_compiles_to_a_real_pdf(self):
        # Exercises the new tabularx/longtable tables, a labelled \srcfig \ref'd from
        # prose, and \tableofcontents together - the guidance's own "compile and
        # inspect" (docs' equivalent, see the module docstring) applies to this
        # change too, since \tableofcontents/\ref only resolve on compile()'s new
        # second pass.
        body = (
            "\\tableofcontents\n\n"
            "\\section{Deadlock}\n"
            "\\srcref{Operating Systems}{42}\n\n"
            "\\begin{sourcequote}\nA formal definition.\n\\end{sourcequote}\n\n"
            "Inline maths \\(O(n^2)\\) and a display:\n\n"
            "\\begin{align}\n  a &= b + c \\\\\n  &= d\n\\end{align}\n\n"
            "See Figure~\\ref{fig:1} and Table~\\ref{tab:causes}.\n\n"
            "\\srcfig{1}{A deadlock cycle}\n\n"
            "\\begin{longtable}{lr}\n\\caption{Causes\\label{tab:causes}}\\\\\n"
            "\\toprule\nA & B \\\\\n\\midrule\n1 & 2 \\\\\n\\bottomrule\n"
            "\\end{longtable}\n\n"
            "\\begin{tabularx}{\\linewidth}{lX}\n\\toprule\nName & Description \\\\\n"
            "\\midrule\nP & A process \\\\\n\\bottomrule\n\\end{tabularx}\n"
        )
        self.assertEqual(latex.lint(body), [])
        one_px_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
        result = latex.compile(latex.assemble(body), figures=[one_px_png])
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.pdf.startswith(b"%PDF"))

    def test_malformed_latex_fails_cleanly_with_named_errors(self):
        result = latex.compile(latex.assemble(
            "\\begin{align}\na = b\n\\end{aligned}\n"))
        self.assertFalse(result.ok)
        self.assertTrue(any("align" in e for e in result.errors))

    def test_a_diagram_and_a_real_figure_both_compile(self):
        body = (
            "\\begin{tikzpicture}\n"
            "\\node[draw] (a) {Ready};\n"
            "\\node[draw, right=2cm of a] (b) {Running};\n"
            "\\draw[-Stealth] (a) -- (b);\n"
            "\\end{tikzpicture}\n\n"
            "\\srcfig{1}{A figure pulled from the source material.}\n"
        )
        self.assertEqual(latex.lint(body), [])
        # A real, minimal 1x1 PNG (RGB) - pdflatex validates the actual image data,
        # not just the magic bytes, so a fake/truncated one would fail the compile.
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
              b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
              b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82")
        result = latex.compile(latex.assemble(body), figures=[png])
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
