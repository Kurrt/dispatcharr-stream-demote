import unittest

from .harness import engine

Settings = engine.Settings


class OrderMaths(unittest.TestCase):
    def test_moves_to_last(self):
        rows = [(1, False), (2, False), (3, False)]
        self.assertEqual([r[0] for r in engine.demote_order(rows, 1)], [2, 3, 1])

    def test_stays_ahead_of_a_trailing_fallback_card(self):
        rows = [(1, False), (2, False), (9, True)]
        self.assertEqual([r[0] for r in engine.demote_order(rows, 1)], [2, 1, 9])

    def test_leaves_a_leading_custom_stream_in_place(self):
        rows = [(9, True), (1, False), (2, False)]
        self.assertEqual([r[0] for r in engine.demote_order(rows, 1)], [9, 2, 1])

    def test_custom_stream_is_never_moved(self):
        rows = [(9, True), (1, False)]
        self.assertEqual(engine.demote_order(rows, 9), rows)

    def test_unknown_stream_changes_nothing(self):
        rows = [(1, False), (2, False)]
        self.assertEqual(engine.demote_order(rows, 7), rows)

    def test_restore_reverses_demotion(self):
        rows = [(1, False), (2, False), (3, False), (9, True)]
        down = engine.demote_order(rows, 2)
        self.assertEqual(engine.restore_order(down, 2, 1), rows)

    def test_restore_is_capped_before_a_fallback_card(self):
        rows = [(1, False), (2, False), (9, True)]
        self.assertEqual([r[0] for r in engine.restore_order(rows, 1, 5)], [2, 1, 9])

    def test_reversing_two_demotions_newest_first_is_exact(self):
        start = [(1, False), (2, False), (3, False), (4, False)]
        first = engine.demote_order(start, 1)
        second = engine.demote_order(first, 3)
        self.assertEqual(engine.restore_order(engine.restore_order(second, 3, first.index((3, False))), 1, 0), start)


class PuttingBack(unittest.TestCase):
    def test_keeps_the_other_tools_order(self):
        rows = [(3, False), (1, False), (2, False), (4, False)]
        self.assertEqual([r[0] for r in engine.reapply_order(rows, [1])], [3, 2, 4, 1])

    def test_several_stay_in_the_order_they_fell(self):
        rows = [(2, False), (9, False), (1, False), (3, False)]
        self.assertEqual([r[0] for r in engine.reapply_order(rows, [1, 2])], [9, 3, 1, 2])

    def test_a_fallback_card_stays_last(self):
        rows = [(1, False), (2, False), (9, True)]
        self.assertEqual([r[0] for r in engine.reapply_order(rows, [1])], [2, 1, 9])

    def test_changes_nothing_when_already_right(self):
        rows = [(2, False), (3, False), (1, False)]
        self.assertEqual(engine.reapply_order(rows, [1]), rows)

    def test_a_stream_no_longer_there_is_ignored(self):
        rows = [(2, False), (3, False)]
        self.assertEqual(engine.reapply_order(rows, [7]), rows)

    def test_still_under_the_rest(self):
        self.assertTrue(engine.still_under_the_rest([2, 3, 1, 9], {9}, {1}, 1))
        self.assertFalse(engine.still_under_the_rest([1, 2, 3], set(), {1}, 1))
        self.assertTrue(engine.still_under_the_rest([2, 1, 4], set(), {1, 4}, 1), "under the other demoted one is fine")


class NextStream(unittest.TestCase):
    def test_walks_on_from_the_current_one(self):
        self.assertEqual(engine.rotate_targets([1, 2, 3, 4], 2, set()), [3, 4, 1])

    def test_skips_what_just_failed(self):
        self.assertEqual(engine.rotate_targets([1, 2, 3], 2, {1}), [3])

    def test_can_come_up_empty(self):
        self.assertEqual(engine.rotate_targets([1, 2], 2, {1}), [])


class Pictures(unittest.TestCase):
    PICTURE = 0x0F0F0F0F0F0F0F0F

    def test_the_same_picture_throughout_is_a_still(self):
        self.assertEqual(engine.still_hash([self.PICTURE] * 12), self.PICTURE)

    def test_small_movement_is_still_a_still(self):
        hashes = [self.PICTURE ^ (index % 3) for index in range(12)]
        self.assertIsNotNone(engine.still_hash(hashes))

    def test_a_moving_picture_is_not(self):
        hashes = [(index + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF for index in range(12)]
        self.assertIsNone(engine.still_hash(hashes))

    def test_a_flat_picture_is_not(self):
        self.assertIsNone(engine.still_hash([None] * 12))

    def test_a_nearly_blank_picture_is_not(self):
        self.assertIsNone(engine.still_hash([0b111] * 12), "too few bits set to tell one from another")

    def test_too_little_to_go_on_is_not(self):
        self.assertIsNone(engine.still_hash([self.PICTURE] * 2))

    def test_a_run_shown_again_counts(self):
        run = [(index + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF for index in range(16)]
        self.assertTrue(engine.repeats(run[4:12], run))

    def test_a_small_overlap_does_not(self):
        run = [(index + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF for index in range(16)]
        self.assertFalse(engine.repeats(run[:2] + [None] * 6, run))

    def test_the_same_still_again_does_not(self):
        self.assertFalse(engine.repeats([self.PICTURE] * 12, [self.PICTURE] * 12))

    def test_verdict_needs_samples_and_time(self):
        cfg = Settings()
        bad = [{"at": 1000 - index * 45, "problems": ["silence"], "audible": False} for index in range(3)]
        self.assertIsNone(engine.content_verdict(bad, cfg, 1000), "90 s isn't 120")
        more = [{"at": 1000 - index * 45, "problems": ["silence"], "audible": False} for index in range(4)]
        self.assertEqual(engine.content_verdict(more, cfg, 1000), "silence")

    def test_a_good_sample_clears_the_run(self):
        cfg = Settings()
        samples = [{"at": 1000 - index * 45, "problems": ["silence"] if index != 1 else [], "audible": False} for index in range(6)]
        self.assertIsNone(engine.content_verdict(samples, cfg, 1000))

    def test_black_comes_before_frozen_and_silence(self):
        cfg = Settings()
        samples = [{"at": 1000 - index * 60, "problems": ["black", "silence"], "audible": False} for index in range(4)]
        self.assertEqual(engine.content_verdict(samples, cfg, 1000), "black")

    def test_loops_can_have_good_samples_between(self):
        cfg = Settings()
        samples = [{"at": 1000 - index * 45, "looped": index % 2 == 0} for index in range(7)]
        self.assertEqual(engine.loop_verdict(samples, cfg), "looping")

    def test_one_repeat_is_not_a_loop(self):
        cfg = Settings()
        samples = [{"at": 1000 - index * 45, "looped": index == 2} for index in range(7)]
        self.assertIsNone(engine.loop_verdict(samples, cfg))


class Weights(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def test_hard_failure_counts_fully(self):
        self.assertEqual(engine.failure_weight("max_retries_exceeded", 1, self.cfg), 1.0)

    def test_early_stall_counts_fully(self):
        self.assertEqual(engine.failure_weight("health_monitor", 20, self.cfg), 1.0)

    def test_late_stall_counts_half(self):
        self.assertEqual(engine.failure_weight("health_monitor", 900, self.cfg), 0.5)

    def test_unknown_age_stall_counts_half(self):
        self.assertEqual(engine.failure_weight("health_monitor", None, self.cfg), 0.5)

    def test_slow_counts_half(self):
        self.assertEqual(engine.failure_weight("buffering_timeout", 5, self.cfg), 0.5)

    def test_manual_counts_nothing(self):
        self.assertEqual(engine.failure_weight("manual", 5, self.cfg), 0.0)

    def test_a_hard_failure_after_a_long_good_play_counts_half(self):
        self.assertEqual(engine.failure_weight("max_retries_exceeded", 6 * 3600, self.cfg), 0.5)

    def test_a_hard_failure_of_unknown_age_counts_fully(self):
        self.assertEqual(engine.failure_weight("max_retries_exceeded", None, self.cfg), 1.0)


class Judging(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def facts(self, **changes):
        base = dict(eligible=3, score=1.0, healthy=True)
        base.update(changes)
        return engine.Facts(**base)

    def test_a_scored_failure_on_a_healthy_provider_demotes(self):
        self.assertEqual(engine.judge(self.facts(), self.cfg), "demote")

    def test_one_stream_is_left_alone(self):
        self.assertEqual(engine.judge(self.facts(eligible=1), self.cfg), "only_stream")

    def test_already_last_is_left_alone(self):
        self.assertEqual(engine.judge(self.facts(already_last=True), self.cfg), "already_last")

    def test_all_streams_failed_is_left_alone(self):
        self.assertEqual(engine.judge(self.facts(all_failed=True), self.cfg), "channel_problem")

    def test_outage_needs_streams_and_channels(self):
        facts = self.facts(healthy=False, outage_streams=3, outage_channels=2)
        self.assertEqual(engine.judge(facts, self.cfg), "outage")

    def test_three_dead_streams_on_one_channel_are_not_an_outage(self):
        facts = self.facts(healthy=False, outage_streams=3, outage_channels=1, chained=True, repeated=True)
        self.assertEqual(engine.judge(facts, self.cfg), "demote")

    def test_proof_of_health_beats_failure_counts(self):
        facts = self.facts(healthy=True, outage_streams=9, outage_channels=5, chained=True)
        self.assertEqual(engine.judge(facts, self.cfg), "demote")

    def test_follow_on_failure_holds_back_the_first_time(self):
        facts = self.facts(healthy=False, chained=True)
        self.assertEqual(engine.judge(facts, self.cfg), "suspect_outage")

    def test_follow_on_failure_goes_ahead_when_it_repeats(self):
        facts = self.facts(healthy=False, chained=True, repeated=True)
        self.assertEqual(engine.judge(facts, self.cfg), "demote")

    def test_paused_account_is_left_alone(self):
        facts = self.facts(healthy=False, paused=True)
        self.assertEqual(engine.judge(facts, self.cfg), "paused")

    def test_a_long_outage_stops_protecting_the_account(self):
        facts = self.facts(healthy=False, paused=True, long_outage=True, outage_streams=9, outage_channels=9, chained=True)
        self.assertEqual(engine.judge(facts, self.cfg), "demote")

    def test_no_proof_waits(self):
        self.assertEqual(engine.judge(self.facts(proven=False), self.cfg), "no_proof")

    def test_an_outage_is_still_an_outage_without_proof(self):
        facts = self.facts(healthy=False, proven=False, outage_streams=3, outage_channels=2)
        self.assertEqual(engine.judge(facts, self.cfg), "outage")

    def test_every_stream_failing_is_still_seen_without_proof(self):
        self.assertEqual(engine.judge(self.facts(proven=False, all_failed=True), self.cfg), "channel_problem")

    def test_below_score_waits(self):
        self.assertEqual(engine.judge(self.facts(score=0.5), self.cfg), "below_score")

    def test_global_limit_stops(self):
        self.assertEqual(engine.judge(self.facts(global_hour=20), self.cfg), "global_stop")

    def test_channel_limits_stop(self):
        self.assertEqual(engine.judge(self.facts(channel_burst=3), self.cfg), "channel_limit")
        self.assertEqual(engine.judge(self.facts(channel_day=6), self.cfg), "channel_limit")


class Guards(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def test_a_stall_excuses_a_later_attempt(self):
        self.assertTrue(engine.stall_excuses(1005, 1006, 1000, self.cfg))

    def test_a_stall_cannot_excuse_itself(self):
        self.assertFalse(engine.stall_excuses(900, 1000, 1000, self.cfg))

    def test_a_later_stall_excuses_nothing(self):
        self.assertFalse(engine.stall_excuses(900, 1000, 1010, self.cfg))

    def test_an_old_stall_excuses_nothing(self):
        self.assertFalse(engine.stall_excuses(1100, 1101, 1000, self.cfg))

    def test_no_stall_excuses_nothing(self):
        self.assertFalse(engine.stall_excuses(1100, 1101, None, self.cfg))

    def test_the_zap_window_runs_from_before_the_attempt_to_after_the_failure(self):
        self.assertEqual(engine.zap_window(1000, 1020, self.cfg), (990, 1030))


class Parole(unittest.TestCase):
    def test_doubles_each_time(self):
        cfg = Settings({"parole_hours": 10})
        self.assertEqual(engine.parole_at(0, 1, cfg), 10 * 3600)
        self.assertEqual(engine.parole_at(0, 2, cfg), 20 * 3600)
        self.assertEqual(engine.parole_at(0, 3, cfg), 40 * 3600)

    def test_has_a_ceiling(self):
        cfg = Settings({"parole_hours": 72})
        self.assertEqual(engine.parole_at(0, 50, cfg), 30 * 24 * 3600)

    def test_zero_turns_it_off(self):
        self.assertIsNone(engine.parole_at(0, 1, Settings({"parole_hours": 0})))


class SettingsParsing(unittest.TestCase):
    def test_defaults(self):
        cfg = Settings()
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.hold_seconds, 45)

    def test_bad_values_fall_back(self):
        cfg = Settings({"hold_seconds": "soon", "demote_score": -1, "journal_days": float("nan")})
        self.assertEqual(cfg.hold_seconds, 45)
        self.assertEqual(cfg.demote_score, 1.0)
        self.assertEqual(cfg.journal_days, 90)

    def test_hold_is_never_shorter_than_the_proof(self):
        self.assertEqual(Settings({"hold_seconds": 1, "healthy_seconds": 30}).effective_hold, 30)

    def test_a_setting_that_cant_be_read_keeps_dry_run_on(self):
        for value in (None, "maybe", "", [], {}, 7):
            self.assertTrue(Settings({"dry_run": value}).dry_run, value)

    def test_real_values_turn_dry_run_off(self):
        for value in (False, 0, "false", "off", "no", "0", " False "):
            self.assertFalse(Settings({"dry_run": value}).dry_run, value)

    def test_content_checks_are_off_by_default(self):
        self.assertFalse(Settings().content_enabled)
        self.assertFalse(Settings({"content_enabled": "maybe"}).content_enabled)

    def test_one_sample_can_never_be_enough(self):
        self.assertEqual(Settings({"content_bad_samples": 1, "content_bad_span_seconds": 0}).content_needed(), (2, 30.0))

    def test_infinity_is_not_a_number_here(self):
        self.assertEqual(Settings({"hold_seconds": "inf"}).hold_seconds, 45)

    def test_excluded_channels(self):
        self.assertEqual(Settings({"excluded_channels": "101, 7.5,x,"}).excluded_numbers(), {101.0, 7.5})
