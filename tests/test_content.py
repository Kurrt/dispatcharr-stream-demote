"""Content checks, with what ffmpeg found stood in for. test_sampler.py covers the ffmpeg side."""

import json
import unittest

from .harness import World, engine, moving_hashes, sample_result

A, B, C = "a1", "a2", "a3"
STILL = 0x0F0F0F0F0F0F0F0F
BLACK = dict(black=True, frozen=True, silent=True, audible=False, mean_db=-91.0, hashes=[None] * 16)


def still_hashes(picture=STILL, count=16):
    return [picture] * count


class Base(unittest.TestCase):
    settings = {"content_enabled": True, "content_act": True}

    def setUp(self):
        self.w = World(dict(self.settings))
        self.w.dp.add_channel(1, 101, [(11, A), (12, B), (13, C)])
        self.w.dp.samples = {11: b"s11", 12: b"s12", 13: b"s13"}

    def shows(self, stream_id, **result):
        self.w.sample_for[f"s{stream_id}".encode()] = sample_result(**result)

    def watch(self, seconds):
        for _ in range(int(seconds // 15)):
            self.w.tick(15)

    def failures(self):
        return self.w.store._rows("SELECT stream_id, cause, state, outcome FROM plugin_stream_demote_failure ORDER BY id")


class TheSwitch(Base):
    settings = {"content_enabled": False}

    def test_off_means_ffmpeg_never_runs(self):
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.w.sample_calls, 0)

    def test_off_is_the_default(self):
        self.assertFalse(engine.Settings().content_enabled)
        self.assertFalse(engine.Settings().content_act, "and when on, it only reports")


class WhatGetsChecked(Base):
    def test_only_with_a_viewer(self):
        self.w.start(1, 11)
        self.w.dp.clients[self.w.dp.channels[1]["uuid"]] = 0
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_right_after_tuning_in(self):
        self.w.start(1, 11)
        self.w.advance(5)
        self.w.service.tick()
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_when_no_data_is_arriving(self):
        self.w.start(1, 11)
        self.w.dp.no_data = {11}
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_on_a_channel_to_leave_alone(self):
        self.w.dp.settings["excluded_channels"] = "101"
        self.w.start(1, 11)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_with_only_one_stream(self):
        self.w.dp.add_channel(2, 102, [(21, A)])
        self.w.start(2, 21)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_on_a_custom_stream(self):
        self.w.dp.add_channel(2, 102, [(9, None), (21, A)])
        self.w.start(2, 9)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_when_the_machine_is_busy(self):
        self.w.machine_busy = True
        self.w.start(1, 11)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)

    def test_not_without_ffmpeg(self):
        self.w.ffmpeg_here = False
        self.w.start(1, 11)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)
        self.assertIn("ffmpeg wasn't found", self.w.service.status(True)["message"])

    def test_every_interval(self):
        self.w.start(1, 11)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 7, "at 30 s, then every 45")

    def test_one_at_a_time_and_no_more_than_the_limit(self):
        for channel in range(2, 7):
            self.w.dp.add_channel(channel, 100 + channel, [(channel * 10, A), (channel * 10 + 1, B)])
            self.w.start(channel, channel * 10)
        self.w.start(1, 11)
        calls = []
        for _ in range(4):
            before = self.w.sample_calls
            self.w.tick(15)
            calls.append(self.w.sample_calls - before)
        self.assertLessEqual(max(calls), 1)

    def test_a_busy_evening_is_shared_out(self):
        for channel in range(2, 8):
            self.w.dp.add_channel(channel, 100 + channel, [(channel * 10, A), (channel * 10 + 1, B)])
            self.w.dp.samples[channel * 10] = f"s{channel * 10}".encode()
            self.w.start(channel, channel * 10)
        self.watch(600)
        counts = [row["n"] for row in self.w.store._rows(
            "SELECT COUNT(*) AS n FROM plugin_stream_demote_sample GROUP BY channel_id")]
        self.assertEqual(len(counts), 6, "every channel got a turn")
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_two_workers_dont_check_at_once(self):
        self.w.start(1, 11)
        self.w.advance(30)
        self.assertTrue(self.w.store.claim("content_slot:0", self.w.clock.now, 60))
        self.w.service.content.tick(engine.Settings(self.w.dp.settings), self.w.clock.now)
        self.assertEqual(self.w.sample_calls, 0)
        self.w.store.release("content_slot:0")
        self.w.service.content.tick(engine.Settings(self.w.dp.settings), self.w.clock.now)
        self.assertEqual(self.w.sample_calls, 1)


class AReadThatFails(Base):
    def test_redis_failing_does_nothing(self):
        self.w.start(1, 11)
        self.w.dp.redis_down = True
        self.watch(600)
        self.assertEqual(self.w.sample_calls, 0)
        self.assertEqual(self.failures(), [])

    def test_a_chunk_going_missing_does_nothing(self):
        self.w.start(1, 11)
        self.w.dp.samples[11] = None
        self.watch(600)
        self.assertEqual(self.w.sample_calls, 0)
        self.assertEqual(self.failures(), [])

    def test_ffmpeg_giving_no_answer_does_nothing(self):
        self.w.start(1, 11)
        self.w.sample_for[b"s11"] = None
        self.watch(600)
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_no_answer_between_bad_samples_doesnt_clear_them_or_count(self):
        verdicts = iter([sample_result(**BLACK), None, sample_result(**BLACK), None, sample_result(**BLACK), sample_result(**BLACK)])
        self.w.sample_for[b"s11"] = lambda _n: next(verdicts, None)
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual([row["cause"] for row in self.failures()], ["content_black"])


class Black(Base):
    def test_one_bad_sample_does_nothing(self):
        answers = iter([sample_result(**BLACK)])
        self.w.sample_for[b"s11"] = lambda _n: next(answers, sample_result())
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.w.dp.rotations, [])

    def test_a_short_dark_scene_does_nothing(self):
        # Two bad samples 45 s apart, then a picture again. A real dark scene looks like this.
        answers = iter([sample_result(**BLACK), sample_result(**BLACK)])
        self.w.sample_for[b"s11"] = lambda _n: next(answers, sample_result())
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])

    def test_lasting_black_moves_the_viewer_and_demotes(self):
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        at = self.w.clock.now
        while not self.w.dp.rotations and self.w.clock.now - at < 600:
            self.w.tick(15)
        self.assertLess(self.w.clock.now - at, 240)
        self.assertGreaterEqual(self.w.clock.now - at, 120, "not before the span")
        self.assertEqual(self.w.dp.running[self.w.dp.channels[1]["uuid"]], 12)
        self.w.switch(1, 11, 12, reason="manual")
        self.assertEqual(self.w.order(1), [11, 12, 13], "the next stream has to look right first")
        self.watch(120)
        self.assertEqual(self.w.order(1), [12, 13, 11])
        row = self.w.journal("demote")[0]
        self.assertEqual(row["cause"], "content_black")

    def test_by_default_a_failed_check_is_only_reported(self):
        self.w.dp.settings["content_act"] = False
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.w.journal("content_failed")[0]["cause"], "content_black")

    def test_black_with_sound_takes_twice_as_long(self):
        self.shows(11, black=True, frozen=True, audible=True, hashes=[None] * 16)
        self.w.start(1, 11)
        self.watch(225)
        self.assertEqual(self.w.dp.rotations, [])
        self.watch(300)
        self.assertEqual(len(self.w.dp.rotations), 1)

    def test_a_still_with_sound_can_be_turned_off(self):
        self.w.dp.settings["content_still_with_sound"] = False
        self.shows(11, black=True, frozen=True, audible=True, hashes=[None] * 16)
        self.w.start(1, 11)
        self.watch(900)
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(self.failures(), [])

    def test_it_can_be_turned_off_on_its_own(self):
        self.w.dp.settings["content_black"] = False
        self.w.dp.settings["content_silence"] = False
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [], "a black picture doesn't get counted as frozen instead")


class Silence(Base):
    def test_lasting_silence_counts(self):
        self.shows(11, silent=True, audible=False, mean_db=-91.0)
        self.w.start(1, 11)
        self.watch(240)
        self.assertEqual(self.failures()[0]["cause"], "content_silence")

    def test_a_quiet_moment_does_not(self):
        answers = iter([sample_result(silent=True, audible=False, mean_db=-91.0)])
        self.w.sample_for[b"s11"] = lambda _n: next(answers, sample_result(audible=False, mean_db=-50.0))
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])

    def test_no_audio_track_counts_too(self):
        self.shows(11, has_audio=False, audible=False, mean_db=None)
        self.w.start(1, 11)
        self.watch(240)
        self.assertEqual(self.failures()[0]["cause"], "content_no_audio")

    def test_no_audio_can_be_turned_off(self):
        self.w.dp.settings["content_no_audio"] = False
        self.shows(11, has_audio=False, audible=False, mean_db=None)
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])


class Radio(Base):
    def test_a_channel_with_no_video_is_skipped_for_good(self):
        self.shows(11, has_video=False, has_audio=True, hashes=[])
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.w.sample_calls, 1, "once it's known to be radio, no more samples")
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.w.dp.rotations, [])

    def test_what_ffmpeg_style_profiles_already_know_costs_nothing(self):
        real = self.w.dp.live_state
        self.w.dp.live_state = lambda uuid: dict(real(uuid), audio_codec="aac", video_codec=None)
        self.w.start(1, 11)
        self.watch(300)
        self.assertEqual(self.w.sample_calls, 0)


class AFile(Base):
    def test_an_mp4_on_an_ffmpeg_profile_counts_as_a_placeholder(self):
        self.w.dp.containers[11] = "mov"
        self.w.start(1, 11)
        self.watch(240)
        self.assertEqual(self.failures()[0]["cause"], "content_file")

    def test_a_live_container_does_not(self):
        self.w.dp.containers[11] = "mpegts"
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])


class Placeholder(Base):
    def setUp(self):
        super().setUp()
        for channel in (2, 3, 4):
            self.w.dp.add_channel(channel, 100 + channel, [(channel * 10, A), (channel * 10 + 1, B)])
            self.w.dp.samples[channel * 10] = f"s{channel * 10}".encode()
            self.w.dp.samples[channel * 10 + 1] = f"s{channel * 10 + 1}".encode()

    def slate(self, stream_id):
        self.shows(stream_id, hashes=still_hashes(), audible=True)

    def known(self):
        return self.w.store.known_pictures()

    def test_a_still_on_one_channel_is_only_noted(self):
        self.slate(20)
        self.w.start(2, 20)
        self.watch(300)
        self.assertEqual(self.failures(), [])
        self.assertEqual(len(self.known()), 1)

    def test_a_still_seen_briefly_isnt_noted(self):
        answers = iter([sample_result(hashes=still_hashes())])
        self.w.sample_for[b"s20"] = lambda _n: next(answers, sample_result())
        self.w.start(2, 20)
        self.watch(300)
        self.assertEqual(self.known(), [])

    def test_a_flat_picture_is_never_noted(self):
        self.shows(20, **BLACK)
        self.w.start(2, 20)
        self.watch(100)
        self.assertEqual(self.known(), [])

    def test_the_same_still_on_three_channels_marks_it(self):
        for channel, stream in ((2, 20), (3, 30), (4, 40)):
            self.slate(stream)
            self.w.start(channel, stream)
        self.w.dp.settings["content_max_at_once"] = 3
        self.watch(600)
        causes = {row["stream_id"]: row["cause"] for row in self.failures()}
        self.assertEqual(set(causes.values()), {"content_placeholder"})
        self.assertGreaterEqual(len(causes), 1)

    def test_a_nearly_identical_still_counts_as_the_same(self):
        near = STILL ^ 0b11
        self.w.store.add_picture(STILL, 2, self.w.clock.now)
        self.w.store.add_picture(STILL, 3, self.w.clock.now)
        self.w.store.add_picture(near, 4, self.w.clock.now)
        self.assertTrue(engine.is_known_placeholder(STILL, self.w.store.known_pictures(), engine.Settings(self.w.dp.settings)))

    def test_a_very_different_still_does_not(self):
        self.w.store.add_picture(STILL, 2, self.w.clock.now)
        self.w.store.add_picture(STILL, 3, self.w.clock.now)
        self.w.store.add_picture(STILL, 4, self.w.clock.now)
        other = STILL ^ 0xFFFFFFFF
        self.assertFalse(engine.is_known_placeholder(other, self.w.store.known_pictures(), engine.Settings(self.w.dp.settings)))

    def test_only_fingerprints_are_kept(self):
        self.slate(20)
        self.w.start(2, 20)
        self.watch(300)
        for row in self.w.store._rows("SELECT * FROM plugin_stream_demote_hash"):
            self.assertRegex(row["picture"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.w.dump_all_tables().count("http"), 0)

    def test_teaching_from_a_button(self):
        self.slate(20)
        self.w.start(2, 20)
        self.w.advance(30)
        result = self.w.service.teach_channel("102", engine.Settings(self.w.dp.settings))
        self.assertEqual(result["result"], "taught")
        self.assertTrue(engine.is_known_placeholder(STILL, self.known(), engine.Settings(self.w.dp.settings)))

    def test_a_taught_picture_counts_from_the_first_sample(self):
        self.w.store.add_picture(STILL, 2, self.w.clock.now, taught=True)
        self.slate(30)
        self.w.start(3, 30)
        self.watch(240)
        self.assertEqual(self.failures()[0]["cause"], "content_placeholder")

    def test_teaching_needs_a_still_picture(self):
        self.w.start(2, 20)
        self.w.advance(30)
        self.assertEqual(self.w.service.teach_channel("102", engine.Settings(self.w.dp.settings))["result"], "not_still")
        self.assertEqual(self.known(), [])

    def test_teaching_needs_a_viewer(self):
        self.assertEqual(self.w.service.teach_channel("102", engine.Settings(self.w.dp.settings))["result"], "not_playing")

    def test_a_flag_never_teaches(self):
        # A flag token does one thing. Teaching is only for the admin button.
        self.slate(20)
        token = self.w.make_token()
        self.w.start(2, 20)
        self.w.advance(30)
        calls = self.w.sample_calls
        self.w.service.flag(f"Bearer {token}", {"channel_number": "102"})
        self.w.advance(1)
        self.assertEqual(self.known(), [])
        self.assertEqual(self.w.sample_calls, calls)

    def test_forgetting_placeholders(self):
        self.w.store.add_picture(STILL, 2, self.w.clock.now, taught=True)
        self.w.store.forget_pictures()
        self.assertEqual(self.known(), [])


class Looping(Base):
    def clip(self, start, length=16, period=24):
        return [((start + i) % period + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF for i in range(length)]

    def test_a_clip_that_keeps_coming_round_counts(self):
        offsets = iter(range(0, 2400, 7))
        self.w.sample_for[b"s11"] = lambda _n: sample_result(hashes=self.clip(next(offsets)))
        self.w.start(1, 11)
        self.watch(420)
        self.assertEqual(self.failures()[0]["cause"], "content_looping")

    def test_programmes_that_keep_changing_do_not(self):
        seeds = iter(range(1, 400))
        self.w.sample_for[b"s11"] = lambda _n: sample_result(hashes=moving_hashes(next(seeds)))
        self.w.start(1, 11)
        self.watch(900)
        self.assertEqual(self.failures(), [])

    def test_a_talking_head_does_not(self):
        # Nearly the same picture for minutes: it matches itself but shows no run of pictures.
        def talking(n):
            base = 0x0123456789ABCDEF
            return sample_result(hashes=[base ^ ((n + i) % 3) for i in range(16)])

        self.w.sample_for[b"s11"] = talking
        self.w.start(1, 11)
        self.watch(900)
        self.assertEqual([row["cause"] for row in self.failures() if row["cause"] == "content_looping"], [])

    def test_a_cut_repeated_once_does_not(self):
        clip = self.clip(0)
        answers = iter([sample_result(hashes=clip), sample_result(hashes=moving_hashes(3)), sample_result(hashes=clip)])
        seeds = iter(range(10, 400))
        self.w.sample_for[b"s11"] = lambda _n: next(answers, sample_result(hashes=moving_hashes(next(seeds))))
        self.w.start(1, 11)
        self.watch(900)
        self.assertEqual(self.failures(), [])

    def test_it_can_be_turned_off(self):
        self.w.dp.settings["content_looping"] = False
        offsets = iter(range(0, 2400, 7))
        self.w.sample_for[b"s11"] = lambda _n: sample_result(hashes=self.clip(next(offsets)))
        self.w.start(1, 11)
        self.watch(600)
        self.assertEqual(self.failures(), [])


class WhenAllStreamsAreBad(Base):
    def test_a_walk_through_a_channel_with_one_slate_moves_nothing(self):
        for stream in (11, 12, 13):
            self.shows(stream, **BLACK)
        self.w.start(1, 11)
        uuid = self.w.dp.channels[1]["uuid"]
        current = 11
        for _ in range(120):
            self.w.tick(15)
            now_on = self.w.dp.running[uuid]
            if now_on != current:
                self.w.switch(1, current, now_on, reason="manual")
                current = now_on
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.w.journal("demote"), [])
        self.assertGreaterEqual(len(self.w.journal("channel_problem")), 1)

    def test_the_viewer_stops_being_moved(self):
        for stream in (11, 12, 13):
            self.shows(stream, **BLACK)
        self.w.start(1, 11)
        uuid = self.w.dp.channels[1]["uuid"]
        current = 11
        for _ in range(240):
            self.w.tick(15)
            now_on = self.w.dp.running[uuid]
            if now_on != current:
                self.w.switch(1, current, now_on, reason="manual")
                current = now_on
        self.assertEqual(len(self.w.dp.rotations), 2)


class TheLimits(Base):
    def test_a_viewer_who_left_isnt_moved(self):
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        uuid = self.w.dp.channels[1]["uuid"]
        cfg = engine.Settings(self.w.dp.settings)
        self.w.dp.clients[uuid] = 0
        moved = self.w.service.content.rotate_after_failure(self.w.dp.channels[1], 11, "content_black", cfg, self.w.clock.now)
        self.assertFalse(moved)
        self.assertEqual(self.w.dp.rotations, [])

    def test_no_move_over_the_hourly_limit(self):
        self.w.dp.settings["content_rotate_hourly"] = 0
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(240)
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(len(self.w.journal("rotate_limited")), 1)

    def test_moves_can_be_turned_off_and_the_stream_still_drops(self):
        self.w.dp.settings["content_rotate"] = False
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(360)
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_refused_moves_mean_the_stream_still_drops(self):
        self.w.dp.refuse_rotation = {12, 13}
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(360)
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_only_one_verdict_per_session(self):
        self.w.dp.settings["content_rotate"] = False
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(900)
        self.assertEqual(len(self.failures()), 1)

    def test_the_demotion_limits_still_apply(self):
        self.w.dp.settings["global_hourly_limit"] = 0
        self.w.dp.settings["content_rotate"] = False
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(360)
        self.assertEqual(self.w.order(1), [11, 12, 13])


class DryRun(Base):
    def test_nothing_moves_and_nobody_is_switched(self):
        self.w.dp.settings["dry_run"] = True
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(360)
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.w.dp.rotations, [])
        self.assertEqual(len(self.w.journal("would_rotate")), 1)
        self.assertEqual(len(self.w.journal("would_demote")), 1)
        self.assertEqual(self.w.journal("would_demote")[0]["cause"], "content_black")


class EarningBack(Base):
    def test_a_stream_dropped_for_its_picture_needs_good_samples_to_come_back(self):
        self.w.dp.settings["content_rotate"] = False
        self.w.dp.settings["parole_hours"] = 0
        self.shows(11, **BLACK)
        self.w.start(1, 11)
        self.watch(360)
        self.assertEqual(self.w.order(1), [12, 13, 11])
        self.w.stop(1)
        self.w.advance(60)
        self.w.start(1, 11)
        self.watch(780)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [12, 13, 11], "ten minutes of black earns nothing")
        self.shows(11)
        self.w.advance(60)
        self.w.start(1, 11)
        self.watch(780)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [11, 12, 13])


class Storage(Base):
    def test_samples_hold_no_pictures_or_addresses(self):
        self.w.start(1, 11)
        self.watch(120)
        rows = self.w.store._rows("SELECT * FROM plugin_stream_demote_sample")
        self.assertGreater(len(rows), 0)
        for row in rows:
            for item in json.loads(row["hashes"]):
                self.assertRegex(item, r"^[0-9a-f]{16}$")

    def test_old_samples_are_cleared(self):
        self.w.start(1, 11)
        self.watch(120)
        self.w.stop(1)
        self.w.store.prune(self.w.clock.now + 90000, 90)
        self.assertEqual(self.w.store._rows("SELECT * FROM plugin_stream_demote_sample"), [])

    def test_erase_clears_them_too(self):
        self.w.start(1, 11)
        self.watch(120)
        self.w.store.add_picture(STILL, 1, self.w.clock.now, taught=True)
        self.w.store.erase()
        self.assertEqual(self.w.store.known_pictures(), [])
        self.assertEqual(self.w.store._rows("SELECT * FROM plugin_stream_demote_sample"), [])

    def test_status_reports_on_them(self):
        self.w.start(1, 11)
        self.watch(120)
        content = self.w.service.status(True)["content"]
        self.assertTrue(content["enabled"])
        self.assertGreater(content["samples_24h"], 0)
        self.assertEqual(content["average_cpu_seconds"], 0.1)
