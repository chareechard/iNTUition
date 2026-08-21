import unittest
import os

from unittest.mock import patch
from intuition.utils import (
    bounded_filename, get_video_download_size, get_filename_from_url,
    sanitise_filename,
)


class TestUtils(unittest.TestCase):
    def test_bounded_filename_shortens_deep_destination_stably(self):
        directory = os.path.join("C:\\NTU", "x" * 210)
        filename = "NTU AY2025-2026 Semester 1 MH2100 Calculus III Midterm Exam 2.pdf"
        first = bounded_filename(directory, filename)

        self.assertEqual(first, bounded_filename(directory, filename))
        self.assertTrue(first.endswith(".pdf"))
        self.assertLessEqual(
            len(os.path.abspath(os.path.join(directory, first))), 240
        )

    def test_bounded_filename_avoids_truncation_collisions(self):
        directory = os.path.join("C:\\", "x" * 210)
        one = bounded_filename(directory, "same long prefix exam one.pdf")
        two = bounded_filename(directory, "same long prefix exam two.pdf")
        self.assertNotEqual(one, two)

    def test_get_video_download_size(self):
        with patch("intuition.utils.requests.head") as head:
            head.return_value.headers = {"Content-Length": str(7633633)}
            result = get_video_download_size("https://example.invalid/media.mp4")
        self.assertEqual("7.28 MB", result)

    def test_get_filename_from_url(self):
        path = "/bbcswebdav/pid-1619585-dt-content-rid-6387676_1/courses/18S2-CE1007-CZ1007-C-LEC/LinearStructures%281%29.zip"
        expected = "LinearStructures(1).zip"
        filename = get_filename_from_url(path)
        self.assertEqual(expected, filename)

    def test_sanitise_filename(self):
        name = 'Week 1: Tutorial (1/2).mp4'
        self.assertEqual('Week 1 Tutorial (1-2).mp4', sanitise_filename(name))
