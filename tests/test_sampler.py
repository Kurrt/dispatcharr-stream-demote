"""The ffmpeg side of content checks: reading its log, and running it if it's installed."""

import os
import shutil
import subprocess
import tempfile
import unittest

from .harness import engine
from stream_demote import sampler

BLACK_LOG = """
[Parsed_volumedetect_1 @ 0x1] n_samples: 0
[Parsed_silencedetect_0 @ 0x2] silence_start: 0
[Parsed_freezedetect_2 @ 0x3] lavfi.freezedetect.freeze_start: 0.0213333
[Parsed_silencedetect_0 @ 0x2] silence_end: 6 | silence_duration: 6
[Parsed_volumedetect_1 @ 0x4] n_samples: 576000
[Parsed_volumedetect_1 @ 0x4] mean_volume: -91.0 dB
[Parsed_volumedetect_1 @ 0x4] max_volume: -91.0 dB
[Parsed_blackdetect_1 @ 0x5] black_start:0.02 black_end:5.98 black_duration:5.96
bench: utime=0.072s stime=0.012s rtime=0.066s
"""
NORMAL_LOG = """
[Parsed_volumedetect_1 @ 0x1] n_samples: 0
[Parsed_volumedetect_1 @ 0x4] n_samples: 288000
[Parsed_volumedetect_1 @ 0x4] mean_volume: -21.1 dB
bench: utime=0.092s stime=0.012s rtime=0.107s
"""


class ReadingTheLog(unittest.TestCase):
    def test_all_black_and_silent(self):
        result = sampler.parse_check(BLACK_LOG, [None] * 12, True, True)
        self.assertTrue(result["black"])
        self.assertTrue(result["frozen"])
        self.assertTrue(result["silent"])
        self.assertFalse(result["audible"])
        self.assertEqual(result["mean_db"], -91.0)
        self.assertAlmostEqual(result["cpu"], 0.084)

    def test_normal(self):
        result = sampler.parse_check(NORMAL_LOG, [1] * 12, True, True)
        self.assertFalse(result["black"])
        self.assertFalse(result["frozen"])
        self.assertFalse(result["silent"])
        self.assertTrue(result["audible"])
        self.assertTrue(result["has_audio"])

    def test_a_short_black_spell_isnt_black(self):
        log = "black_start:0 black_end:2 black_duration:2\n" + NORMAL_LOG
        self.assertFalse(sampler.parse_check(log, [1] * 12, True, True)["black"])

    def test_a_freeze_that_ended_isnt_frozen(self):
        log = "lavfi.freezedetect.freeze_start: 0\nlavfi.freezedetect.freeze_end: 5.5\n" + NORMAL_LOG
        self.assertFalse(sampler.parse_check(log, [1] * 12, True, True)["frozen"])

    def test_digital_silence_can_read_minus_infinity(self):
        log = "n_samples: 100\nsilence_start: 0\nmean_volume: -inf dB\n"
        self.assertTrue(sampler.parse_check(log, [1] * 12, True, True)["silent"])

    def test_quiet_speech_isnt_silent(self):
        log = "n_samples: 100\nmean_volume: -55.0 dB\n"
        result = sampler.parse_check(log, [1] * 12, True, True)
        self.assertFalse(result["silent"])
        self.assertTrue(result["audible"])

    def test_silence_with_no_level_reading_isnt_silence(self):
        self.assertFalse(sampler.parse_check("silence_start: 0\n", [1] * 12, True, True)["silent"])

    def test_no_audio_track(self):
        result = sampler.parse_check(NORMAL_LOG.replace("n_samples", "x"), [1] * 12, True, False)
        self.assertFalse(result["has_audio"])
        self.assertTrue(result["audio_known"])

    def test_a_track_with_no_audio_in_it(self):
        self.assertFalse(sampler.parse_check("n_samples: 0\n", [1] * 12, True, True)["has_audio"])

    def test_no_frames_knows_nothing(self):
        result = sampler.parse_check(BLACK_LOG, [], True, True)
        self.assertFalse(result["black"])
        self.assertFalse(result["audio_known"])


class Fingerprints(unittest.TestCase):
    def test_a_flat_frame_has_none(self):
        self.assertIsNone(sampler.frame_hash(bytes([16] * 72)))
        self.assertIsNone(sampler.frame_hash(bytes([16, 20] * 36)))

    def test_a_wrong_size_has_none(self):
        self.assertIsNone(sampler.frame_hash(bytes(10)))

    def test_it_is_64_bits(self):
        value = sampler.frame_hash(bytes((index * 3) % 256 for index in range(72)))
        self.assertIsNotNone(value)
        self.assertLess(value, 2 ** 64)

    def test_brightness_doesnt_change_it(self):
        frame = [(index * 2) % 100 for index in range(72)]
        self.assertEqual(sampler.frame_hash(bytes(frame)), sampler.frame_hash(bytes(item + 40 for item in frame)))

    def test_frames_are_split_up(self):
        raw = bytes((index * 3) % 256 for index in range(72)) * 3
        self.assertEqual(len(sampler.frame_hashes(raw)), 3)

    def test_probe_output(self):
        self.assertEqual(sampler.parse_probe('{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'), (True, True))
        self.assertEqual(sampler.parse_probe('{"streams":[{"codec_type":"audio"}]}'), (False, True))
        self.assertIsNone(sampler.parse_probe("not json"))
        self.assertIsNone(sampler.parse_probe("{}"))


FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class RealVideo(unittest.TestCase):
    """Short clips made here, run through the real ffmpeg. Decoding is what's being tested."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def make(self, name, video, audio):
        path = os.path.join(self.folder, name + ".ts")
        command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        if video:
            command += ["-f", "lavfi", "-i", video]
        if audio:
            command += ["-f", "lavfi", "-i", audio]
        command += ["-t", "10"]
        if video:
            command += ["-c:v", "mpeg2video", "-b:v", "500k"]
        if audio:
            command += ["-c:a", "mp2", "-b:a", "64k"]
        command += ["-f", "mpegts", path]
        subprocess.run(command, check=True)
        with open(path, "rb") as handle:
            return sampler.check_bytes(handle.read(), 6)

    MOVING = "testsrc2=s=320x180:r=25"
    BLACK = "color=black:s=320x180:r=25"
    STILL = "smptebars=s=320x180:r=25"
    SOUND = "sine=f=440:r=48000"
    SILENT = "anullsrc=r=48000:cl=stereo"

    def test_black_and_silent(self):
        result = self.make("black", self.BLACK, self.SILENT)
        self.assertTrue(result["black"])
        self.assertTrue(result["silent"])
        self.assertTrue(all(item is None for item in result["hashes"]), "a flat picture has no fingerprint")

    def test_a_sample_cut_part_way_through_is_still_all_black(self):
        path = os.path.join(self.folder, "longgop.ts")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", self.BLACK,
                        "-f", "lavfi", "-i", self.SILENT, "-t", "24", "-c:v", "mpeg2video", "-g", "75", "-b:v", "500k",
                        "-c:a", "mp2", "-f", "mpegts", "-muxrate", "800k", path], check=True)
        with open(path, "rb") as handle:
            data = handle.read()
        # Start well inside a group of pictures, on a packet boundary.
        start = (len(data) // 5) // 188 * 188
        result = sampler.check_bytes(data[start:start + 188 * 8000], 6)
        self.assertIsNotNone(result)
        self.assertTrue(result["black"], result["seconds"])
        self.assertLess(result["seconds"], 15.5)

    def test_normal(self):
        result = self.make("normal", self.MOVING, self.SOUND)
        self.assertFalse(result["black"])
        self.assertFalse(result["frozen"])
        self.assertFalse(result["silent"])
        self.assertTrue(result["audible"])
        problems, _still = engine.assess_sample(result, engine.Settings())
        self.assertEqual(problems, [])

    def test_frozen_with_sound(self):
        result = self.make("frozen", self.STILL, self.SOUND)
        self.assertTrue(result["frozen"])
        self.assertFalse(result["black"])
        self.assertTrue(result["audible"])
        self.assertIsNotNone(engine.still_hash(result["hashes"]))

    def test_moving_and_silent(self):
        result = self.make("silent", self.MOVING, self.SILENT)
        self.assertTrue(result["silent"])
        self.assertFalse(result["black"])

    def test_no_audio_track(self):
        result = self.make("noaudio", self.MOVING, None)
        self.assertFalse(result["has_audio"])
        self.assertTrue(result["audio_known"])
        self.assertIn("no_audio", engine.assess_sample(result, engine.Settings())[0])

    def test_radio(self):
        result = self.make("radio", None, self.SOUND)
        self.assertFalse(result["has_video"])

    def test_junk_gives_no_answer(self):
        self.assertIsNone(sampler.check_bytes(os.urandom(188 * 2000), 6))

    def test_too_little_gives_no_answer(self):
        path = os.path.join(self.folder, "short.ts")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", self.MOVING,
                        "-t", "1", "-c:v", "mpeg2video", "-f", "mpegts", path], check=True)
        with open(path, "rb") as handle:
            self.assertIsNone(sampler.check_bytes(handle.read(), 6))

    def test_nothing_is_left_behind(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.make("tidy", self.MOVING, self.SOUND)
        left = {name for name in set(os.listdir(tempfile.gettempdir())) - before if name.startswith("stream-demote-")}
        self.assertEqual(left, set())

    def test_a_time_limit_gives_no_answer(self):
        with open(os.path.join(self.folder, "normal.ts") if os.path.exists(os.path.join(self.folder, "normal.ts")) else os.devnull, "rb") as handle:
            data = handle.read()
        if data:
            self.assertIsNone(sampler.check_bytes(data, 6, timeout=0.0001))
