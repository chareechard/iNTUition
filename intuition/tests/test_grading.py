"""Tests for grading a student's worked tutorial against a professor's solution."""
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import ai_provider, drive, grading


class ExtractWorkContentTests(unittest.TestCase):
    def test_image_returns_raw_bytes(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.png")
            data = b"\x89PNG\r\n\x1a\n" + b"fake png body"
            with open(path, "wb") as f:
                f.write(data)
            text, image = grading.extract_work_content(path)
            self.assertIsNone(text)
            self.assertEqual(image, data)

    def test_text_file_with_content_is_extracted(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("This is my worked answer to question 1, in full detail.")
            text, image = grading.extract_work_content(path)
            self.assertIsNone(image)
            self.assertIn("worked answer", text)

    def test_near_empty_text_raises(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("hi")
            with self.assertRaises(grading.GradingError):
                grading.extract_work_content(path)

    def test_pdf_with_no_text_layer_raises_the_actionable_grading_error(self):
        """A scanned PDF with zero text on every page fails inside
        drive.extract_learning_text with drive.DriveError, before this
        module's own MIN_TEXT_CHARS check ever runs. That must still surface
        as a GradingError pointing the student at the PNG/JPEG alternative,
        not the low-level DriveError."""
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.pdf")
            with patch("intuition.drive.extract_learning_text",
                      side_effect=drive.DriveError("No readable text was found in this material")):
                with self.assertRaises(grading.GradingError) as ctx:
                    grading.extract_work_content(path)
            self.assertIn("photo", str(ctx.exception))


class BuildGradingPromptTests(unittest.TestCase):
    def test_includes_practice_solution_and_work(self):
        prompt = grading.build_grading_prompt(
            "MH1300/Tut01.pdf", "Q1: differentiate x^2", "d/dx x^2 = 2x",
            "Tut01_solutions.pdf", "I got 2x")
        self.assertIn("differentiate x^2", prompt)
        self.assertIn("2x", prompt)
        self.assertIn("Tut01_solutions.pdf", prompt)
        self.assertIn("I got 2x", prompt)

    def test_omits_practice_section_when_absent(self):
        prompt = grading.build_grading_prompt(
            "MH1300/Tut01.pdf", None, "d/dx x^2 = 2x", "Tut01_solutions.pdf", "I got 2x")
        self.assertNotIn("Tutorial questions", prompt)

    def test_image_work_notes_attachment_instead_of_text(self):
        prompt = grading.build_grading_prompt(
            "MH1300/Tut01.pdf", None, "d/dx x^2 = 2x", "Tut01_solutions.pdf", None)
        self.assertIn("attached as an image", prompt)


class GradeTests(unittest.TestCase):
    def test_unreadable_upload_short_circuits_before_calling_the_model(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
            with patch.object(ai_provider, "complete_tier") as mock_complete:
                result = grading.grade(path, "solution text", "sol.pdf", None, "Tut01.pdf")
            mock_complete.assert_not_called()
            self.assertFalse(result.ok)
            self.assertIn("readable text", result.error)

    def test_successful_text_grade_uses_scholar_tier(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("My full worked answer goes here with plenty of detail.")
            with patch.object(ai_provider, "complete_tier") as mock_complete:
                mock_complete.return_value = {
                    "text": "Q1: Correct.", "backend": "omniroute",
                    "model": "claude-opus-5", "rung": "claude/claude-opus-5",
                }
                result = grading.grade(path, "solution text", "sol.pdf",
                                       "Q1: ...", "Tut01.pdf",
                                       preferred_backend="omniroute",
                                       download_root=tmp)
            self.assertTrue(result.ok)
            self.assertEqual(result.feedback, "Q1: Correct.")
            self.assertEqual(result.model, "claude-opus-5")
            tier, prompt, system = mock_complete.call_args[0]
            self.assertEqual(tier, "scholar")
            self.assertIn("My full worked answer", prompt)
            self.assertIsNone(mock_complete.call_args.kwargs.get("images"))

    def test_image_work_is_passed_through_as_vision_input(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.png")
            data = b"\x89PNG\r\n\x1a\n" + b"fake"
            with open(path, "wb") as f:
                f.write(data)
            with patch.object(ai_provider, "complete_tier") as mock_complete:
                mock_complete.return_value = {"text": "Correct.", "backend": "omniroute",
                                              "model": "claude-opus-5", "rung": ""}
                grading.grade(path, "solution text", "sol.pdf", None, "Tut01.pdf")
            self.assertEqual(mock_complete.call_args.kwargs.get("images"), [data])

    def test_provider_failure_is_reported_not_raised(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "work.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("My full worked answer goes here with plenty of detail.")
            with patch.object(ai_provider, "complete_tier",
                             side_effect=ai_provider.ProviderError("scholar tier down")):
                result = grading.grade(path, "solution text", "sol.pdf", None, "Tut01.pdf")
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "scholar tier down")


if __name__ == "__main__":
    unittest.main()
