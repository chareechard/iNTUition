import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from intuition import transcribe


class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeInfo:
    duration = 10.0


class FakeModel:
    def __init__(self, *a, **k):
        self.kwargs = k

    def transcribe(self, path, **kw):
        return iter([
            FakeSegment(0.0, 2.5, " Welcome to operating systems."),
            FakeSegment(2.5, 6.25, " Today we cover interrupts and DMA."),
        ]), FakeInfo()


class TestFormatting(unittest.TestCase):
    def test_timestamp_format(self):
        self.assertEqual(transcribe._timestamp(0), "00:00:00.000")
        self.assertEqual(transcribe._timestamp(2.5), "00:00:02.500")
        self.assertEqual(transcribe._timestamp(3661.25), "01:01:01.250")

    def test_negative_timestamp_clamps(self):
        self.assertEqual(transcribe._timestamp(-1), "00:00:00.000")

    def test_vtt_structure(self):
        vtt = transcribe.to_vtt([
            {"start": 0.0, "end": 2.5, "text": " hello "},
            {"start": 2.5, "end": 4.0, "text": "world"},
        ])
        lines = vtt.splitlines()
        self.assertEqual(lines[0], "WEBVTT")
        self.assertIn("00:00:00.000 --> 00:00:02.500", lines)
        self.assertIn("hello", lines)
        self.assertIn("00:00:02.500 --> 00:00:04.000", lines)

    def test_text_is_flat_prose_and_wrapped(self):
        segs = [{"start": i, "end": i + 1, "text": "word" * 5} for i in range(40)]
        txt = transcribe.to_text(segs)
        self.assertNotIn("-->", txt)
        self.assertTrue(all(len(l) <= 100 for l in txt.splitlines()))
        self.assertTrue(txt.endswith("\n"))


class TestPaths(unittest.TestCase):
    def test_transcript_paths_sit_beside_the_video(self):
        p = transcribe.transcript_paths(os.path.join("a", "b", "Lecture 1.mp4"))
        self.assertEqual(p["vtt"], os.path.join("a", "b", "Lecture 1.vtt"))
        self.assertEqual(p["txt"], os.path.join("a", "b", "Lecture 1.txt"))

    def test_is_media_covers_video_and_audio(self):
        self.assertTrue(transcribe.is_media("x.mp4"))
        self.assertTrue(transcribe.is_media("x.WMV"))
        self.assertTrue(transcribe.is_media("x.m4a"))
        self.assertFalse(transcribe.is_media("x.pdf"))
        self.assertFalse(transcribe.is_media("x.vtt"))

    def test_already_transcribed_needs_both_outputs(self):
        with TemporaryDirectory() as d:
            v = os.path.join(d, "a.mp4")
            open(v, "w").close()
            self.assertFalse(transcribe.already_transcribed(v))
            open(os.path.join(d, "a.vtt"), "w").close()
            self.assertFalse(transcribe.already_transcribed(v))
            open(os.path.join(d, "a.txt"), "w").close()
            self.assertTrue(transcribe.already_transcribed(v))


class TestFindMedia(unittest.TestCase):
    def test_finds_media_and_skips_done_and_non_media(self):
        with TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Week 1"))
            todo = os.path.join(d, "Week 1", "lec.mp4")
            done = os.path.join(d, "Week 1", "old.mp4")
            for p in (todo, done):
                open(p, "w").close()
            open(os.path.join(d, "Week 1", "old.vtt"), "w").close()
            open(os.path.join(d, "Week 1", "old.txt"), "w").close()
            open(os.path.join(d, "Week 1", "notes.pdf"), "w").close()

            found = transcribe.find_media(d)
        self.assertEqual(found, [todo])

    def test_skip_transcribed_false_returns_everything(self):
        with TemporaryDirectory() as d:
            v = os.path.join(d, "a.mp4")
            open(v, "w").close()
            open(os.path.join(d, "a.vtt"), "w").close()
            open(os.path.join(d, "a.txt"), "w").close()
            self.assertEqual(transcribe.find_media(d, skip_transcribed=False), [v])


class TestTranscriber(unittest.TestCase):
    def test_writes_both_outputs_and_reports_progress(self):
        with TemporaryDirectory() as d:
            video = os.path.join(d, "Lecture 1.mp4")
            open(video, "wb").write(b"fake")

            seen = []
            t = transcribe.Transcriber()
            with patch.dict("sys.modules", {
                "faster_whisper": type("m", (), {"WhisperModel": FakeModel})
            }):
                paths = t.transcribe(video, progress=lambda f, txt: seen.append(f))

            self.assertTrue(os.path.exists(paths["vtt"]))
            self.assertTrue(os.path.exists(paths["txt"]))
            self.assertIn("WEBVTT", open(paths["vtt"], encoding="utf-8").read())
            body = open(paths["txt"], encoding="utf-8").read()
            self.assertIn("interrupts and DMA", body)
            self.assertTrue(seen, "should report progress")

    def test_missing_file_raises(self):
        t = transcribe.Transcriber()
        with self.assertRaises(transcribe.TranscribeError):
            t.transcribe("/no/such/video.mp4")

    def test_missing_backend_gives_install_help(self):
        t = transcribe.Transcriber()
        with patch.dict("sys.modules", {"faster_whisper": None}):
            with patch("builtins.__import__", side_effect=ImportError("nope")):
                with self.assertRaises(transcribe.TranscribeError) as ctx:
                    t.load()
        self.assertIn("pip install faster-whisper", str(ctx.exception))

    def test_silent_media_raises_rather_than_writing_empty_files(self):
        class Silent(FakeModel):
            def transcribe(self, path, **kw):
                return iter([]), FakeInfo()

        with TemporaryDirectory() as d:
            video = os.path.join(d, "silent.mp4")
            open(video, "wb").write(b"fake")
            t = transcribe.Transcriber()
            with patch.dict("sys.modules", {
                "faster_whisper": type("m", (), {"WhisperModel": Silent})
            }):
                with self.assertRaises(transcribe.TranscribeError):
                    t.transcribe(video)
            self.assertFalse(os.path.exists(os.path.join(d, "silent.vtt")))



class TestTranscriptProvenance(unittest.TestCase):
    """Detect an existing transcript, and who produced it, before offering Whisper."""

    def _video(self, d, name="Lecture 1.mp4"):
        p = os.path.join(d, name)
        open(p, "wb").write(b"fake")
        return p

    def test_missing_when_nothing_beside_it(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            self.assertEqual(transcribe.transcript_status(v)["status"],
                             transcribe.MISSING)

    def test_course_supplied_vtt_is_provided(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            open(os.path.join(d, "Lecture 1.vtt"), "w").write("WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")
            got = transcribe.transcript_status(v)
            self.assertEqual(got["status"], transcribe.PROVIDED)
            self.assertTrue(got["sources"])

    def test_course_supplied_srt_is_provided(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            open(os.path.join(d, "Lecture 1.srt"), "w").write("1\n")
            self.assertEqual(transcribe.transcript_status(v)["status"],
                             transcribe.PROVIDED)

    def test_named_transcript_document_is_provided(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            open(os.path.join(d, "Lecture 1 transcript.docx"), "w").write("x")
            self.assertEqual(transcribe.transcript_status(v)["status"],
                             transcribe.PROVIDED)

    def test_our_own_output_is_generated_not_provided(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            paths = transcribe.transcript_paths(v)
            open(paths["vtt"], "w", encoding="utf-8").write(
                transcribe.to_vtt([{"start": 0, "end": 1, "text": "hi"}], model="small.en"))
            open(paths["txt"], "w", encoding="utf-8").write("hi\n")
            self.assertEqual(transcribe.transcript_status(v)["status"],
                             transcribe.GENERATED)

    def test_generated_vtt_carries_the_marker(self):
        vtt = transcribe.to_vtt([{"start": 0, "end": 1, "text": "hi"}], model="small.en")
        self.assertIn(transcribe.GENERATED_MARKER, vtt)
        self.assertIn("small.en", vtt)
        self.assertTrue(vtt.startswith("WEBVTT"))

    def test_unrelated_files_do_not_count(self):
        with TemporaryDirectory() as d:
            v = self._video(d)
            open(os.path.join(d, "Some other notes.pdf"), "w").write("x")
            open(os.path.join(d, "Lecture 2.vtt"), "w").write("WEBVTT")
            self.assertEqual(transcribe.transcript_status(v)["status"],
                             transcribe.MISSING)

    def test_find_media_skips_course_supplied_transcripts(self):
        """Never burn CPU regenerating something the lecturer already gave you."""
        with TemporaryDirectory() as d:
            has = self._video(d, "Has transcript.mp4")
            open(os.path.join(d, "Has transcript.vtt"), "w").write("WEBVTT")
            needs = self._video(d, "Needs transcript.mp4")
            self.assertEqual(transcribe.find_media(d), [needs])

    def test_survey_reports_each_status_with_sources(self):
        with TemporaryDirectory() as d:
            self._video(d, "A.mp4")
            self._video(d, "B.mp4")
            open(os.path.join(d, "B.vtt"), "w").write("WEBVTT")
            rows = {r["name"]: r for r in transcribe.survey(d)}
            self.assertEqual(rows["A.mp4"]["status"], transcribe.MISSING)
            self.assertEqual(rows["A.mp4"]["sources"], [])
            self.assertEqual(rows["B.mp4"]["status"], transcribe.PROVIDED)
            self.assertIn("B.vtt", rows["B.mp4"]["sources"])

    def test_survey_is_empty_for_a_folder_with_no_media(self):
        with TemporaryDirectory() as d:
            open(os.path.join(d, "notes.pdf"), "w").write("x")
            self.assertEqual(transcribe.survey(d), [])


class TestClassifyDriveMedia(unittest.TestCase):
    """The Drive-hosted counterpart of survey()/transcript_status() - move mode has
    already deleted the local copy, so this is the only place left to detect it."""

    def _file(self, name, rel_path, size=1000, mime="video/mp4"):
        return {"id": "id-" + rel_path, "name": name, "rel_path": rel_path,
                "mime_type": mime, "size": size, "modified": "2026-01-01T00:00:00Z"}

    def test_missing_when_nothing_beside_it(self):
        files = [self._file("Lecture 1.mp4", "CE2003/Lecture 1.mp4")]
        media = transcribe.classify_drive_media(files)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["status"], transcribe.MISSING)
        self.assertEqual(media[0]["location"], "drive")
        self.assertEqual(media[0]["drive_id"], "id-CE2003/Lecture 1.mp4")

    def test_course_supplied_vtt_is_provided(self):
        files = [
            self._file("Lecture 1.mp4", "CE2003/Lecture 1.mp4"),
            self._file("Lecture 1.vtt", "CE2003/Lecture 1.vtt", mime="text/vtt"),
        ]
        media = transcribe.classify_drive_media(files)
        self.assertEqual(media[0]["status"], transcribe.PROVIDED)
        self.assertIn("Lecture 1.vtt", media[0]["sources"])

    def test_our_own_vtt_and_txt_pair_is_generated(self):
        files = [
            self._file("Lecture 1.mp4", "CE2003/Lecture 1.mp4"),
            self._file("Lecture 1.vtt", "CE2003/Lecture 1.vtt", mime="text/vtt"),
            self._file("Lecture 1.txt", "CE2003/Lecture 1.txt", mime="text/plain"),
        ]
        media = transcribe.classify_drive_media(files)
        self.assertEqual(media[0]["status"], transcribe.GENERATED)
        self.assertEqual(set(media[0]["sources"]), {"Lecture 1.vtt", "Lecture 1.txt"})

    def test_only_a_vtt_with_no_matching_txt_is_provided_not_generated(self):
        """A lone .vtt could be ours (upload interrupted after one file) or a
        lecturer's - without downloading to check the marker, treating it as
        provided is the safe direction: it skips a re-transcribe rather than risk
        clobbering someone else's captions."""
        files = [
            self._file("Lecture 1.mp4", "CE2003/Lecture 1.mp4"),
            self._file("Lecture 1.vtt", "CE2003/Lecture 1.vtt", mime="text/vtt"),
        ]
        media = transcribe.classify_drive_media(files)
        self.assertEqual(media[0]["status"], transcribe.PROVIDED)

    def test_different_folders_do_not_cross_contaminate(self):
        files = [
            self._file("Lecture 1.mp4", "CE2003/Lecture 1.mp4"),
            self._file("Lecture 1.vtt", "CZ2006/Lecture 1.vtt", mime="text/vtt"),
        ]
        media = transcribe.classify_drive_media(files)
        self.assertEqual(media[0]["status"], transcribe.MISSING)

    def test_non_media_files_are_excluded(self):
        files = [self._file("Notes.pdf", "CE2003/Notes.pdf", mime="application/pdf")]
        self.assertEqual(transcribe.classify_drive_media(files), [])

    def test_sorted_by_rel_path(self):
        files = [
            self._file("B.mp4", "CE2003/B.mp4"),
            self._file("A.mp4", "CE2003/A.mp4"),
        ]
        media = transcribe.classify_drive_media(files)
        self.assertEqual([m["rel_path"] for m in media], ["CE2003/A.mp4", "CE2003/B.mp4"])


if __name__ == "__main__":
    unittest.main()
