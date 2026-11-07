import unittest

from .harness import World

A, B = "a1", "a2"


class BadStart(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, B)])

    def test_failed_start_demotes_after_the_hold(self):
        self.w.start(1, 11)
        self.w.advance(1)
        self.w.switch(1, 11, 12)
        self.assertEqual(self.w.order(1), [11, 12, 13], "nothing moves during the hold")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 11])
        row = self.w.journal("demote")[0]
        self.assertEqual((row["stream_id"], row["cause"], row["actor"]), (11, "max_retries_exceeded", "event"))
        self.assertEqual((row["from_position"], row["to_position"]), (0, 2))

    def test_dry_run_logs_and_moves_nothing(self):
        self.w.dp.settings["dry_run"] = True
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(len(self.w.journal("would_demote")), 1)
        self.assertEqual(self.w.journal("demote"), [])
        self.assertEqual(self.w.dp.emitted, [])

    def test_late_stall_needs_two(self):
        self.w.start(1, 11)
        self.w.advance(600)
        self.w.switch(1, 11, 12, reason="health_monitor")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.w.stop(1)
        self.w.advance(120)
        self.w.start(1, 11)
        self.w.advance(600)
        self.w.switch(1, 11, 12, reason="health_monitor")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_old_failures_stop_counting(self):
        self.w.start(1, 11)
        self.w.advance(600)
        self.w.switch(1, 11, 12, reason="buffering_timeout")
        self.w.advance(47)
        self.w.stop(1)
        self.w.advance(25 * 3600)
        self.w.start(1, 11)
        self.w.advance(1)
        self.w.switch(1, 11, 12, reason="buffering_timeout")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_manual_switches_are_not_failures(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12, reason="manual")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.w.journal(), [])

    def test_a_second_delivery_of_the_same_event_counts_once(self):
        self.w.dp.settings["demote_score"] = 2
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.service.handle_event("stream_switch", {
            "channel_id": self.w.dp.channels[1]["uuid"], "previous_stream_id": 11, "stream_id": 12,
            "reason": "max_retries_exceeded"})
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_the_sweep_catches_up_if_the_timer_is_lost(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.timers.clear()
        self.w.clock.tick(60)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_a_failure_is_not_judged_from_the_session_it_has_lost(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.timers.clear()
        self.w.clock.tick(60)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [11, 12, 13], "nothing shows that another stream played")
        self.assertEqual(self.w.store.count_recent(0).get("no_proof"), 1)

    def test_only_one_worker_acts_on_a_failure(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.clock.tick(60)
        self.w.service.sweep()
        self.w.service.sweep()
        self.assertEqual(len(self.w.journal("demote")), 1)

    def test_excluded_channel_is_left_alone(self):
        self.w.dp.settings["excluded_channels"] = "101"
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_sends_the_shared_event(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        event, _uuid, details = self.w.dp.emitted[0]
        self.assertEqual(event, "stream_demoted")
        self.assertEqual(details["stream_id"], 11)

    def test_shared_event_can_be_turned_off(self):
        self.w.dp.settings["shared_event"] = False
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.dp.emitted, [])


class NotOverReacting(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def add_channels(self, count=4, account=A):
        for index in range(count):
            first = 100 + index * 10
            self.w.dp.add_channel(index + 1, 200 + index, [(first, account), (first + 1, B)])

    def test_provider_outage_moves_nothing(self):
        self.add_channels(4)
        for channel in range(1, 5):
            self.w.start(channel, 100 + (channel - 1) * 10)
        self.w.advance(1)
        for channel in range(1, 5):
            first = 100 + (channel - 1) * 10
            self.w.switch(channel, first, first + 1)
        self.w.advance(60)
        for channel in range(1, 5):
            first = 100 + (channel - 1) * 10
            self.assertEqual(self.w.order(channel), [first, first + 1])
        self.assertEqual(self.w.journal("demote"), [])
        self.assertEqual(len(self.w.journal("outage")), 1)
        self.assertEqual(self.w.store.paused_accounts(self.w.clock.now), [A])

    def test_failures_after_the_outage_was_seen_stay_held_back(self):
        self.add_channels(5)
        for channel in range(1, 5):
            first = 100 + (channel - 1) * 10
            self.w.start(channel, first)
            self.w.switch(channel, first, first + 1)
        self.w.advance(60)
        self.w.start(5, 140)
        self.w.switch(5, 140, 141)
        self.w.advance(60)
        self.assertEqual(self.w.order(5), [140, 141])

    def test_an_outage_rolls_back_what_was_demoted_just_before_it_showed(self):
        self.add_channels(4)
        self.w.start(1, 100)
        self.w.switch(1, 100, 101)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [101, 100], "one failure looks like one bad stream")
        for channel in (2, 3):
            first = 100 + (channel - 1) * 10
            self.w.start(channel, first)
            self.w.switch(channel, first, first + 1)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [100, 101], "put back once the outage showed")
        self.assertEqual(len(self.w.journal("rollback")), 1)

    def test_an_outage_leaves_a_flag_in_place(self):
        self.add_channels(4)
        token = self.w.make_token()
        self.w.start(4, 130)
        self.assertEqual(self.w.service.flag(f"Bearer {token}", {"channel_number": "203"})[1], {"result": "rotated"})
        self.assertEqual(self.w.order(4), [131, 130])
        self.w.stop(4)
        self.w.advance(40)
        for channel in (1, 2, 3):
            first = 100 + (channel - 1) * 10
            self.w.start(channel, first)
            self.w.switch(channel, first, first + 1)
        self.w.advance(60)
        self.assertEqual(len(self.w.journal("outage")), 1)
        self.assertEqual(self.w.order(4), [131, 130], "a person saw that one")

    def test_strikes_from_an_outage_do_not_count_later(self):
        self.add_channels(4)
        for channel in range(1, 4):
            first = 100 + (channel - 1) * 10
            self.w.start(channel, first)
            self.w.switch(channel, first, first + 1)
        self.w.advance(60)
        self.assertEqual(self.w.store.score(1, 100, 0), 0.0)

    def test_a_playing_stream_proves_the_provider_is_up(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A), (14, A)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [13, 14, 11, 12])

    def test_two_failures_with_no_proof_are_held_back_the_first_time(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, B)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_the_same_failures_again_later_go_ahead(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, B)])
        for _ in range(2):
            self.w.start(1, 11)
            self.w.switch(1, 11, 12)
            self.w.switch(1, 12, 13)
            self.w.advance(47)
            self.w.stop(1)
            self.w.advance(600)
        self.assertEqual(self.w.order(1), [13, 11, 12])

    def test_three_dead_streams_on_one_channel_clear_on_the_repeat(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A), (14, B)])
        for _ in range(2):
            self.w.start(1, 11)
            self.w.switch(1, 11, 12)
            self.w.switch(1, 12, 13)
            self.w.switch(1, 13, 14)
            self.w.advance(47)
            self.w.stop(1)
            self.w.advance(600)
        self.assertEqual(self.w.order(1), [14, 11, 12, 13])
        self.assertEqual(self.w.journal("outage"), [])

    def test_all_streams_failing_leaves_the_order_alone(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, B)])
        self.w.start(1, 11)
        self.w.error(1, 11)
        self.w.switch(1, 11, 12)
        self.w.error(1, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [11, 12])
        self.assertEqual(len(self.w.journal("channel_problem")), 1)

    def test_a_viewer_zapping_good_channels_moves_nothing(self):
        for channel in range(1, 6):
            first = channel * 10
            self.w.dp.add_channel(channel, 100 + channel, [(first, A), (first + 1, A)])
        before = {c: self.w.order(c) for c in range(1, 6)}
        for _round in range(4):
            for channel in range(1, 6):
                self.w.start(channel, channel * 10)
                self.w.advance(3)
                self.w.stop(channel)
                self.w.advance(1)
        self.w.advance(300)
        self.assertEqual({c: self.w.order(c) for c in range(1, 6)}, before)
        self.assertEqual(self.w.journal(), [])

    def test_failure_right_after_a_zap_is_not_counted(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.w.dp.add_channel(2, 102, [(21, A), (22, A)])
        self.w.start(1, 11)
        self.w.advance(100)
        self.w.stop(1)
        self.w.start(2, 21)
        self.w.advance(1)
        self.w.switch(2, 21, 22)
        self.w.advance(47)
        self.assertEqual(self.w.order(2), [21, 22])

    def test_the_zap_guard_goes_by_when_the_attempt_began(self):
        # A provider that takes 5 s to refuse, three times over, reports it 15 s after the zap.
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.w.dp.add_channel(2, 102, [(21, A), (22, B)])
        self.w.start(1, 11)
        self.w.advance(100)
        self.w.stop(1)
        self.w.advance(1)
        self.w.start(2, 21)
        self.w.advance(15)
        self.w.switch(2, 21, 22)
        self.w.advance(47)
        self.assertEqual(self.w.order(2), [21, 22])
        self.assertEqual(self.w.store.count_recent(0).get("zap_guard"), 1)

    def test_a_channel_stopping_just_after_the_attempt_also_counts(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.w.dp.add_channel(2, 102, [(21, A), (22, B)])
        self.w.start(1, 11)
        self.w.advance(100)
        self.w.start(2, 21)
        self.w.advance(1)
        self.w.switch(2, 21, 22)
        self.w.advance(2)
        self.w.stop(1)
        self.w.advance(47)
        self.assertEqual(self.w.order(2), [21, 22], "worked out when the failure is judged")

    def test_a_later_stop_doesnt_hide_the_one_that_explains_it(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.w.dp.add_channel(2, 102, [(21, A), (22, B)])
        self.w.dp.add_channel(3, 103, [(31, A), (32, B)])
        self.w.start(1, 11)
        self.w.advance(100)
        self.w.start(2, 21)
        self.w.advance(1)
        self.w.switch(2, 21, 22)
        self.w.advance(1)
        self.w.stop(1)
        self.w.advance(20)
        self.w.stop(2)
        self.w.start(3, 31)
        self.w.advance(10)
        self.w.stop(3)
        self.w.advance(47)
        self.assertEqual(self.w.order(2), [21, 22])
        self.assertEqual(self.w.store.count_recent(0).get("zap_guard"), 1)

    def test_a_channels_own_stop_excuses_nothing(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, B)])
        self.w.start(1, 11)
        self.w.advance(100)
        self.w.stop(1)
        self.w.advance(1)
        self.w.start(1, 11)
        self.w.advance(1)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 11])

    def test_a_stream_let_off_again_and_again_is_counted_in_the_end(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.w.dp.add_channel(2, 102, [(21, A), (22, B)])
        for _round in range(3):
            self.w.start(1, 11)
            self.w.advance(100)
            self.w.stop(1)
            self.w.advance(1)
            self.w.start(2, 21)
            self.w.advance(1)
            self.w.switch(2, 21, 22)
            self.w.advance(47)
            self.w.stop(2)
            self.w.advance(700)
        self.assertEqual(self.w.order(2), [22, 21])

    def test_failure_right_after_a_stall_on_the_account_is_not_counted(self):
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        self.w.start(1, 11)
        self.w.advance(10)
        self.w.switch(1, 11, 12, reason="health_monitor")
        self.w.advance(5)
        self.w.switch(1, 12, 13)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_channel_limit_stops_a_walk_and_the_rest_wait(self):
        self.w.dp.settings["channel_burst_limit"] = 1
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_global_limit_stops_and_warns_once(self):
        self.w.dp.settings["global_hourly_limit"] = 1
        self.w.dp.add_channel(1, 101, [(11, A), (12, B)])
        self.w.dp.add_channel(2, 102, [(21, "c"), (22, B)])
        self.w.dp.add_channel(3, 103, [(31, "d"), (32, B)])
        for channel, first in ((1, 11), (2, 21), (3, 31)):
            self.w.start(channel, first)
            self.w.switch(channel, first, first + 1)
            self.w.advance(47)
            self.w.stop(channel)
            self.w.advance(200)
        self.assertEqual(self.w.order(1), [12, 11])
        self.assertEqual(self.w.order(2), [21, 22])
        self.assertEqual(self.w.order(3), [31, 32])
        self.assertEqual(len(self.w.dp.notices), 1)

    def test_a_broken_account_stops_being_protected_after_a_day(self):
        self.w.dp.settings["outage_giveup_hours"] = 24
        self.add_channels(4)
        for _day in range(3):
            for channel in range(1, 4):
                first = 100 + (channel - 1) * 10
                self.w.start(channel, first)
                self.w.switch(channel, first, first + 1)
            self.w.advance(60)
            for channel in range(1, 4):
                self.w.stop(channel)
            self.w.advance(13 * 3600)
        self.assertEqual(self.w.order(1), [101, 100])

    def test_custom_stream_failing_is_ignored(self):
        self.w.dp.add_channel(1, 101, [(9, None), (11, A)])
        self.w.start(1, 9)
        self.w.switch(1, 9, 11)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [9, 11])


class EarningItsWayBack(unittest.TestCase):
    def setUp(self):
        self.w = World({"parole_hours": 48})
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_a_long_clean_play_earns_its_place_back(self):
        self.w.advance(60)
        self.w.start(1, 11)
        self.w.advance(601)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(len(self.w.journal("restore_earned")), 1)
        self.assertIsNone(self.w.store.demoted(1, 11))

    def test_a_short_play_earns_nothing(self):
        self.w.advance(60)
        self.w.start(1, 11)
        self.w.advance(120)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_a_running_session_earns_it_without_stopping(self):
        self.w.advance(700)
        self.w.start(1, 11)
        self.w.advance(650)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_second_chance_after_the_wait(self):
        self.w.advance(48 * 3600 + 700)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.w.store.demoted(1, 11)["state"], "parole")

    def test_failing_a_second_chance_demotes_for_twice_as_long(self):
        self.w.advance(48 * 3600 + 700)
        self.w.service.sweep()
        self.w.start(1, 11)
        self.w.switch(1, 11, 12, reason="buffering_timeout")
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 11], "weak evidence is enough on a second chance")
        self.assertEqual(self.w.store.demoted(1, 11)["strikes"], 2)
        self.w.stop(1)
        self.w.advance(48 * 3600 + 700)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [12, 13, 11], "the wait has doubled")
        self.w.advance(48 * 3600 + 700)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_playing_well_on_a_second_chance_clears_its_record(self):
        self.w.advance(48 * 3600 + 700)
        self.w.service.sweep()
        self.w.start(1, 11)
        self.w.advance(601)
        self.w.stop(1)
        self.assertIsNone(self.w.store.demoted(1, 11))

    def test_second_chance_can_be_turned_off(self):
        self.w.dp.settings["parole_hours"] = 0
        self.w.advance(30 * 24 * 3600)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_a_watched_session_that_began_before_demotion_earns_nothing(self):
        world = World({"parole_hours": 0})
        world.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        world.dp.running[world.dp.channels[1]["uuid"]] = 11
        world.start(1, 11)
        world.advance(1200)
        token = world.make_token()
        status, body, _ = world.service.flag(f"Bearer {token}", {"channel_number": "101"})
        self.assertEqual((status, body["result"]), (200, "rotated"))
        world.switch(1, 11, 12, reason="manual")
        self.assertEqual(world.order(1), [12, 13, 11], "the rotation must not read as a good play")


class Undo(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A), (14, A)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [13, 14, 11, 12])

    def test_undo_last(self):
        self.assertEqual(self.w.service.undo(limit=1), 1)
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])

    def test_undo_channel_restores_the_exact_order(self):
        self.assertEqual(self.w.service.undo(channel_id=1), 2)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])

    def test_undo_clears_the_strikes(self):
        self.w.service.undo(channel_id=1)
        self.assertEqual(self.w.store.score(1, 11, 0), 0.0)
        self.assertEqual(self.w.store.demoted_rows(), [])

    def test_undo_twice_does_nothing_more(self):
        self.w.service.undo(channel_id=1)
        self.assertEqual(self.w.service.undo(channel_id=1), 0)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])

    def test_undo_keeps_streams_added_since_and_skips_ones_removed(self):
        self.w.dp.streams[15] = {"account_key": A, "is_custom": False, "limited": False}
        self.w.dp.orders[1] = [13, 14, 15, 11]
        self.w.service.undo(channel_id=1)
        self.assertEqual(self.w.order(1), [11, 13, 14, 15])


class Flag(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, B)])
        self.uuid = self.w.dp.channels[1]["uuid"]
        self.w.start(1, 11)
        self.token = self.w.make_token()

    def flag(self, body=None, token=None):
        auth = f"Bearer {token or self.token}"
        return self.w.service.flag(auth, body if body is not None else {"channel_number": "101"})

    def test_flag_demotes_and_rotates_straight_away(self):
        status, body, _ = self.flag()
        self.assertEqual((status, body), (200, {"result": "rotated"}))
        self.assertEqual(self.w.order(1), [12, 13, 11])
        self.assertEqual(self.w.dp.running[self.uuid], 12)
        self.assertEqual(self.w.journal("demote")[0]["actor"], "flag:tv")

    def test_flag_can_leave_the_session_alone(self):
        self.w.dp.settings["flag_rotate"] = False
        _, body, _ = self.flag()
        self.assertEqual(body["result"], "demoted")
        self.assertEqual(self.w.dp.rotations, [])

    def test_rotation_skips_a_full_provider(self):
        self.w.dp.refuse_rotation = {12}
        self.flag()
        self.assertEqual(self.w.dp.running[self.uuid], 13)

    def test_nothing_to_rotate_to_still_demotes(self):
        self.w.dp.refuse_rotation = {12, 13}
        _, body, _ = self.flag()
        self.assertEqual(body["result"], "demoted")
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_dry_run_moves_nothing(self):
        self.w.dp.settings["dry_run"] = True
        _, body, _ = self.flag()
        self.assertEqual(body["result"], "observed")
        self.assertEqual(self.w.order(1), [11, 12, 13])
        self.assertEqual(self.w.dp.rotations, [])

    def test_no_token(self):
        status, body, headers = self.w.service.flag("", {"channel_number": "101"})
        self.assertEqual((status, body), (401, {"result": "unauthorized"}))
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_wrong_token(self):
        self.assertEqual(self.flag(token="sdf_wrong")[0], 401)

    def test_other_schemes_are_refused(self):
        self.assertEqual(self.w.service.flag(f"ApiKey {self.token}", {"channel_number": "101"})[0], 401)
        self.assertEqual(self.w.service.flag("ApiKey an-admin-key", {"channel_number": "101"})[0], 401)
        self.assertEqual(self.w.service.flag("Bearer a.jwt.value", {"channel_number": "101"})[0], 401)

    def test_revoked_token(self):
        self.assertEqual(self.w.service.revoke_token("tv"), 1)
        self.assertEqual(self.flag()[0], 401)

    def test_token_is_stored_hashed(self):
        self.assertNotIn(self.token, self.w.dump_all_tables())
        self.assertNotIn(self.token[4:], self.w.dump_all_tables())

    def test_idle_channel_is_refused(self):
        self.w.stop(1)
        status, body, _ = self.flag()
        self.assertEqual((status, body), (409, {"result": "not_playing"}))
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_unknown_channel_is_refused_the_same_way(self):
        self.assertEqual(self.flag({"channel_number": "999"})[1], {"result": "not_playing"})

    def test_duplicate_number_is_ambiguous(self):
        other = self.w.dp.add_channel(2, 101, [(21, A), (22, A)])
        self.w.dp.running[other] = 21
        status, body, _ = self.flag()
        self.assertEqual((status, body), (409, {"result": "ambiguous"}))

    def test_duplicate_number_with_one_running_is_fine(self):
        self.w.dp.add_channel(2, 101, [(21, A), (22, A)])
        self.assertEqual(self.flag()[1], {"result": "rotated"})

    def test_uuid_is_accepted(self):
        self.assertEqual(self.flag({"channel_uuid": self.uuid})[1], {"result": "rotated"})

    def test_cannot_name_a_stream(self):
        self.flag({"channel_number": "101", "stream_id": 13})
        self.assertEqual(self.w.order(1), [12, 13, 11], "stream_id is ignored, the running stream is used")

    def test_bad_input(self):
        for body in ({}, {"channel_number": "1; DROP"}, {"channel_uuid": "x"}, "text", None, {"channel_number": True}):
            self.assertEqual(self.w.service.flag(f"Bearer {self.token}", body)[0], 400, body)

    def test_channel_limit(self):
        self.assertEqual(self.flag()[0], 200)
        status, body, headers = self.flag()
        self.assertEqual((status, body), (429, {"result": "rate_limited"}))
        self.assertLessEqual(int(headers["Retry-After"]), 61)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_channel_limit_ends(self):
        self.flag()
        self.w.clock.tick(61)
        self.assertEqual(self.flag()[0], 200)

    def test_token_hourly_limit(self):
        self.w.dp.settings["flag_token_hourly"] = 2
        results = [self.flag({"channel_number": "999"})[0] for _ in range(3)]
        self.assertEqual(results, [409, 409, 429])
        self.w.clock.tick(3601)
        self.assertEqual(self.flag({"channel_number": "999"})[0], 409)

    def test_replies_never_name_a_stream_or_provider(self):
        allowed = {"rotated", "demoted", "observed", "noted", "not_playing", "ambiguous", "rate_limited",
                   "paused", "channel_problem", "unauthorized", "bad_request"}
        for body in ({"channel_number": "101"}, {"channel_number": "101"}, {"channel_number": "999"}, {}):
            _, reply, _ = self.flag(body)
            self.assertEqual(set(reply), {"result"})
            self.assertIn(reply["result"], allowed)

    def test_flagging_every_stream_stops(self):
        self.flag()
        self.w.clock.tick(61)
        self.assertEqual(self.flag()[1], {"result": "rotated"})
        self.w.clock.tick(61)
        self.assertEqual(self.flag()[1], {"result": "channel_problem"})
        self.assertEqual(self.w.order(1), [13, 11, 12])

    def test_single_stream_channel_is_only_noted(self):
        other = self.w.dp.add_channel(2, 102, [(21, A)])
        self.w.dp.running[other] = 21
        self.assertEqual(self.flag({"channel_number": "102"})[1], {"result": "noted"})
        self.assertEqual(self.w.order(2), [21])

    def test_rotation_is_never_counted_as_a_failure_of_the_new_stream(self):
        self.flag()
        self.w.switch(1, 11, 12, reason="manual")
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [12, 13, 11])
        self.assertEqual(self.w.store.score(1, 12, 0), 0.0)

    def test_undo_reverses_a_flag(self):
        self.flag()
        self.w.service.undo(limit=1)
        self.assertEqual(self.w.order(1), [11, 12, 13])


class Tokens(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_needs_a_label(self):
        self.assertIsNone(self.w.service.create_token("  ")[0])

    def test_label_must_be_free(self):
        self.w.make_token("tv")
        self.assertIsNone(self.w.service.create_token("tv")[0])
        self.w.service.revoke_token("tv")
        self.assertIsNotNone(self.w.service.create_token("tv")[0])

    def test_revoke_only_the_named_token(self):
        kept = self.w.make_token("kept")
        self.w.make_token("gone")
        self.w.service.revoke_token("gone")
        self.w.dp.add_channel(1, 101, [(11, A), (12, A)])
        self.assertEqual(self.w.service.flag(f"Bearer {kept}", {"channel_number": "999"})[0], 409)


class WhatGetsStored(unittest.TestCase):
    def test_no_urls_or_viewer_details_are_kept(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, A)])
        uuid = w.dp.channels[1]["uuid"]
        secret = "http://provider.example/live/USERNAME/PASSWORD/1.ts"
        noisy = {
            "channel_id": uuid, "stream_id": 11, "stream_url": secret, "channel_url": secret, "url": secret,
            "new_url": secret, "user_agent": "Player/1", "client_ip": "192.0.2.9", "username": "viewer",
            "stream_name": "Secret name", "provider_name": "Secret provider",
        }
        w.service.handle_event("channel_start", noisy)
        w.service.handle_event("channel_error", dict(noisy, error_type="connection_failed"))
        w.service.handle_event("stream_switch", dict(noisy, previous_stream_id=11, stream_id=12, reason="max_retries_exceeded"))
        w.advance(47)
        w.service.handle_event("channel_stop", noisy)
        text = w.dump_all_tables()
        for needle in ("provider.example", "USERNAME", "PASSWORD", "192.0.2.9", "viewer", "Player/1", "Secret"):
            self.assertNotIn(needle, text)

    def test_erase_clears_everything(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, A)])
        w.make_token()
        w.start(1, 11)
        w.switch(1, 11, 12)
        w.advance(47)
        w.store.erase()
        self.assertEqual(w.dump_all_tables().replace("sqlite_sequence", ""), "".join(
            repr(row) + "\n" for row in w.conn.execute("SELECT * FROM sqlite_sequence").fetchall()).rstrip("\n"))
        self.assertEqual(w.order(1), [12, 11], "order isn't touched")

    def test_status_and_journal_export(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, A)])
        w.start(1, 11)
        w.switch(1, 11, 12)
        w.advance(47)
        status = w.service.status(True)
        self.assertEqual(status["demoted_now"], 1)
        self.assertIn("Flag route is live", status["message"])
        path, rows = w.service.write_journal_csv()
        self.assertEqual(len(rows), 1)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("demote", handle.read())


class AnOffAirChannel(unittest.TestCase):
    """Every stream delivers nothing, so the last one never produces a failure of its own."""

    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "b"), (3, "c")])
        self.w.dp.no_data = {1, 2, 3}

    def walk(self):
        self.w.start(1, 1)
        self.w.advance(10)
        self.w.switch(1, 1, 2, reason="health_monitor")
        self.w.advance(10)
        self.w.switch(1, 2, 3, reason="health_monitor")

    def test_nothing_moves_while_nothing_plays(self):
        self.walk()
        self.w.advance(120)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.assertEqual(self.w.journal("demote"), [])

    def test_nothing_moves_when_the_viewer_gives_up(self):
        self.walk()
        self.w.advance(30)
        self.w.stop(1)
        self.w.advance(400)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.assertEqual(self.w.store.count_recent(0).get("no_proof"), 2)

    def test_it_gives_up_waiting_in_the_end(self):
        self.walk()
        for _ in range(30):
            self.w.tick(15)
        self.assertEqual(self.w.store.count_recent(0).get("no_proof"), 2, "settled, not waiting for ever")

    def test_a_late_recovery_of_the_last_stream_proves_it_after_all(self):
        self.walk()
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.w.dp.no_data = set()
        for _ in range(3):
            self.w.tick(15)
        self.assertEqual(self.w.order(1), [3, 1, 2])


class ARecoveredStream(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])
        self.w.start(1, 1)
        self.w.advance(2)
        self.w.switch(1, 1, 2)
        self.w.advance(5)
        self.w.stop(1)
        self.w.advance(5)

    def test_it_isnt_demoted_while_playing(self):
        self.w.start(1, 1)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [1, 2])
        self.assertEqual(self.w.journal("demote"), [])
        self.assertEqual(self.w.store.count_recent(0).get("recovered"), 1)
        self.assertEqual(self.w.store.score(1, 1, 0), 0.0)

    def test_it_can_fail_again_and_then_it_counts(self):
        self.w.start(1, 1)
        self.w.advance(60)
        self.w.switch(1, 1, 2)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [2, 1])


class AFlagOnAJustFrozenStream(unittest.TestCase):
    def test_a_refusal_right_after_is_nobodys_fault_even_with_data_seconds_ago(self):
        w = World()
        w.dp.add_channel(1, 101, [(1, "a"), (2, "a"), (3, "b")])
        w.start(1, 1)
        token = w.make_token()
        w.advance(30)
        self.assertEqual(w.service.flag(f"Bearer {token}", {"channel_number": "101"})[1]["result"], "rotated")
        w.switch(1, 1, 2, reason="manual")
        w.advance(2)
        w.switch(1, 2, 3)
        w.advance(60)
        self.assertEqual(w.store.count_recent(0).get("stall_guard"), 1)
        self.assertEqual(w.order(1), [2, 3, 1])


class AFlagOnAFrozenStream(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "a"), (3, "b")])
        self.w.start(1, 1)
        self.token = self.w.make_token()
        self.w.advance(30)

    def flag(self):
        return self.w.service.flag(f"Bearer {self.token}", {"channel_number": "101"})[1]["result"]

    def test_it_goes_to_another_account_first(self):
        self.w.dp.no_data = {1}
        self.assertEqual(self.flag(), "rotated")
        self.assertEqual(self.w.dp.rotations[-1][1], 3)

    def test_with_no_stall_it_takes_the_next_in_order(self):
        self.assertEqual(self.flag(), "rotated")
        self.assertEqual(self.w.dp.rotations[-1][1], 2)

    def test_a_refusal_after_it_is_nobodys_fault(self):
        # One seat, and the frozen connection still holds it for a while.
        self.w.dp.no_data = {1}
        self.w.dp.refuse_rotation = {3}
        self.assertEqual(self.flag(), "rotated")
        self.w.switch(1, 1, 2, reason="manual")
        self.w.advance(1)
        self.w.switch(1, 2, 3, reason="max_retries_exceeded")
        self.w.advance(60)
        self.assertEqual(self.w.store.count_recent(0).get("stall_guard"), 1)
        self.assertEqual(self.w.order(1), [2, 3, 1], "the stream that was refused keeps its place")


class WhereAFlagGoes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "a"), (3, "a")])
        self.token = self.w.make_token()

    def flag(self):
        return self.w.service.flag(f"Bearer {self.token}", {"channel_number": "101"})

    def test_it_doesnt_go_back_to_what_just_failed(self):
        self.w.start(1, 1)
        self.w.advance(1)
        self.w.switch(1, 1, 2)
        self.w.advance(10)
        self.assertEqual(self.flag()[1]["result"], "rotated")
        self.assertEqual(self.w.dp.rotations, [(self.w.dp.channels[1]["uuid"], 3)])

    def test_nowhere_left_to_go_still_demotes(self):
        self.w.dp.add_channel(2, 102, [(4, "a"), (5, "a")])
        self.w.start(2, 4)
        self.w.advance(1)
        self.w.switch(2, 4, 5)
        self.w.advance(10)
        self.assertEqual(self.w.service.flag(f"Bearer {self.token}", {"channel_number": "102"})[1]["result"], "demoted")
        self.assertEqual(self.w.dp.rotations, [])

    def test_nobody_watching_means_nothing_happens(self):
        # The channel is still running for its shutdown delay, but the viewer has gone.
        self.w.start(1, 1)
        self.w.dp.clients[self.w.dp.channels[1]["uuid"]] = 0
        status, body, _ = self.flag()
        self.assertEqual((status, body), (409, {"result": "not_playing"}))
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.assertEqual(self.w.dp.rotations, [])

    def test_redis_failing_means_nothing_happens(self):
        self.w.start(1, 1)
        self.w.dp.redis_down = True
        self.assertEqual(self.flag()[1], {"result": "not_playing"})
        self.assertEqual(self.w.order(1), [1, 2, 3])

    def test_a_viewer_leaving_mid_flag_gets_no_switch(self):
        self.w.start(1, 1)
        uuid = self.w.dp.channels[1]["uuid"]
        real = self.w.dp.apply_order

        def leaves(channel_id, change):
            self.w.dp.clients[uuid] = 0
            return real(channel_id, change)

        self.w.dp.apply_order = leaves
        self.assertEqual(self.flag()[1], {"result": "demoted"})
        self.assertEqual(self.w.dp.rotations, [])

    def test_a_broken_rotate_says_demoted_not_error(self):
        self.w.start(1, 1)
        self.w.dp.broken_rotate = True
        status, body, _ = self.flag()
        self.assertEqual((status, body), (200, {"result": "demoted"}))
        self.assertEqual(self.w.order(1), [2, 3, 1])
        self.assertEqual(len(self.w.journal("rotate_failed")), 1)
        self.assertEqual(self.w.service.status(True)["errors_last_hour"], 1)


class ALongGoodPlay(unittest.TestCase):
    def test_one_failure_after_hours_of_play_does_not_demote(self):
        w = World()
        w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])
        w.start(1, 1)
        w.advance(6 * 3600)
        w.switch(1, 1, 2)
        w.advance(60)
        self.assertEqual(w.order(1), [1, 2])

    def test_two_do(self):
        w = World()
        w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])
        for _ in range(2):
            w.start(1, 1)
            w.advance(6 * 3600)
            w.switch(1, 1, 2)
            w.advance(60)
            w.stop(1)
            w.advance(60)
        self.assertEqual(w.order(1), [2, 1])


class DryRunMeansNothingMoves(unittest.TestCase):
    def demoted_world(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        w.start(1, 11)
        w.switch(1, 11, 12)
        w.advance(47)
        w.stop(1)
        self.assertEqual(w.order(1), [12, 13, 11])
        return w

    def test_second_chance(self):
        w = self.demoted_world()
        w.dp.settings["dry_run"] = True
        w.advance(73 * 3600)
        w.service.sweep()
        self.assertEqual(w.order(1), [12, 13, 11])
        self.assertEqual(w.store.demoted(1, 11)["state"], "demoted")
        self.assertEqual(len(w.journal("would_restore_parole")), 1)

    def test_second_chance_is_written_down_once_a_day(self):
        w = self.demoted_world()
        w.dp.settings["dry_run"] = True
        for _ in range(5):
            w.advance(73 * 360)
            w.advance(0)
            w.store.release("sweep")
            w.service.sweep()
        w.advance(73 * 3600)
        w.store.release("sweep")
        w.service.sweep()
        self.assertLessEqual(len(w.journal("would_restore_parole")), 2)

    def test_earning_back(self):
        w = self.demoted_world()
        w.dp.settings["dry_run"] = True
        w.advance(60)
        w.start(1, 11)
        w.advance(700)
        w.stop(1)
        self.assertEqual(w.order(1), [12, 13, 11])
        self.assertEqual(len(w.journal("would_restore_earned")), 1)
        self.assertIsNotNone(w.store.demoted(1, 11))

    def test_outage_rollback(self):
        w = World()
        for i in (1, 2, 3, 4):
            w.dp.add_channel(i, 100 + i, [(i * 10, "a"), (i * 10 + 1, "b")])
        w.start(1, 10)
        w.advance(1)
        w.switch(1, 10, 11)
        w.advance(50)
        self.assertEqual(w.order(1), [11, 10])
        w.dp.settings["dry_run"] = True
        for i in (2, 3, 4):
            w.start(i, i * 10)
            w.advance(1)
            w.switch(i, i * 10, i * 10 + 1)
            w.advance(1)
        w.advance(60)
        self.assertEqual(w.order(1), [11, 10])
        self.assertEqual(w.journal("rollback"), [])
        self.assertIn("would_roll_back=1", w.journal("outage")[0]["cause"])

    def test_undo_is_a_person_asking_so_it_still_works(self):
        w = self.demoted_world()
        w.dp.settings["dry_run"] = True
        self.assertEqual(w.service.undo(channel_id=1), 1)
        self.assertEqual(w.order(1), [11, 12, 13])


class ChannelsToLeaveAlone(unittest.TestCase):
    def demoted_world(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, A), (13, A)])
        w.start(1, 11)
        w.switch(1, 11, 12)
        w.advance(47)
        w.stop(1)
        w.dp.settings["excluded_channels"] = "101"
        return w

    def test_second_chance_leaves_it(self):
        w = self.demoted_world()
        w.advance(73 * 3600)
        w.service.sweep()
        self.assertEqual(w.order(1), [12, 13, 11])

    def test_earning_back_leaves_it(self):
        w = self.demoted_world()
        w.advance(60)
        w.start(1, 11)
        w.advance(700)
        w.stop(1)
        self.assertEqual(w.order(1), [12, 13, 11])

    def test_outage_rollback_leaves_it(self):
        w = self.demoted_world()
        w.dp.add_channel(2, 102, [(21, A), (22, B)])
        w.dp.add_channel(3, 103, [(31, A), (32, B)])
        w.dp.add_channel(4, 104, [(41, A), (42, B)])
        w.advance(15)
        for channel in (2, 3, 4):
            w.start(channel, channel * 10 + 1)
            w.switch(channel, channel * 10 + 1, channel * 10 + 2)
        w.advance(60)
        self.assertEqual(len(w.journal("outage")), 1)
        self.assertEqual(w.order(1), [12, 13, 11])


class UndoAfterAStreamHasComeBack(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "a"), (3, "b")])
        self.w.start(1, 1)
        self.w.advance(60)
        self.w.switch(1, 1, 2)
        self.w.advance(50)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [2, 3, 1])

    def test_a_stream_that_earned_its_place_back_is_left_alone(self):
        self.w.advance(5)
        self.w.start(1, 1)
        self.w.advance(660)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.w.hand_order(1, [2, 3, 1])
        self.assertEqual(self.w.service.undo(limit=1), 0)
        self.assertEqual(self.w.order(1), [2, 3, 1])

    def test_a_stream_on_a_second_chance_is_left_alone(self):
        self.w.advance(73 * 3600)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.w.hand_order(1, [2, 3, 1])
        self.assertEqual(self.w.service.undo(), 0)
        self.assertEqual(self.w.order(1), [2, 3, 1])

    def test_a_second_chance_failed_can_be_undone_once(self):
        self.w.advance(73 * 3600)
        self.w.service.sweep()
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(47)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [2, 3, 1])
        self.assertEqual(self.w.service.undo(), 1)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.assertEqual(self.w.service.undo(), 0)


class PuttingThePluginBack(unittest.TestCase):
    def test_a_new_install_clears_old_tokens_and_demotions(self):
        w = World()
        w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])
        token = w.make_token()
        w.start(1, 1)
        w.switch(1, 1, 2)
        w.advance(47)
        w.stop(1)
        w.service.ready()
        self.assertIsNotNone(w.store.demoted(1, 1))
        # Dispatcharr drops the plugin's config row when it's removed, so the id changes.
        w.dp.config_id = 2
        w.service._install_checked = False
        w.dp.running[w.dp.channels[1]["uuid"]] = 2
        self.assertEqual(w.service.flag(f"Bearer {token}", {"channel_number": "101"})[0], 401)
        self.assertIsNone(w.store.demoted(1, 1))
        self.assertEqual(w.service.undo(), 0)
        self.assertEqual(w.order(1), [2, 1], "stream order isn't touched")
        self.assertEqual(len(w.journal("reinstalled")), 1)

    def test_restarting_changes_nothing(self):
        w = World()
        token = w.make_token()
        w.service.ready()
        w.service._install_checked = False
        w.service.ready()
        self.assertIsNotNone(w.store.find_token(__import__("stream_demote.service", fromlist=["hash_token"]).hash_token(token)))


class Reporting(unittest.TestCase):
    def test_status_says_when_events_are_failing(self):
        w = World()
        w.dp.channel = lambda _uuid: (_ for _ in ()).throw(RuntimeError("moved"))
        for _ in range(4):
            w.service.handle_event("channel_start", {"channel_id": "x", "stream_id": 1})
        status = w.service.status(True)
        self.assertEqual(status["errors_last_hour"], 4)
        self.assertIn("Something is wrong", status["message"])
        self.assertNotIn("Live", status["message"])

    def test_a_failing_event_never_reaches_the_proxy(self):
        w = World()
        w.dp.channel = lambda _uuid: (_ for _ in ()).throw(RuntimeError("moved"))
        w.service.handle_event("channel_start", {"channel_id": "x", "stream_id": 1})

    def test_status_says_when_the_loop_has_not_run(self):
        w = World()
        self.assertIn("background check hasn't run", w.service.status(True)["message"])
        w.tick(15)
        self.assertNotIn("background check hasn't run", w.service.status(True)["message"])

    def test_a_label_starting_with_equals_cant_be_a_formula(self):
        w = World()
        w.make_token("=HYPERLINK(1)")
        path, _rows = w.service.write_journal_csv()
        with open(path, encoding="utf-8") as handle:
            self.assertIn("admin:=HYPERLINK(1)", handle.read())
        w.store.add_journal(w.clock.now, "x", actor="=SUM(1)")
        path, _rows = w.service.write_journal_csv()
        with open(path, encoding="utf-8") as handle:
            self.assertIn("'=SUM(1)", handle.read())

    def test_a_global_stop_notice_goes_through_dispatcharrs_notifications(self):
        w = World({"global_hourly_limit": 0})
        w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])
        w.start(1, 1)
        w.switch(1, 1, 2)
        w.advance(47)
        self.assertEqual(len(w.dp.notices), 1)


class WithAToolThatProbes(unittest.TestCase):
    """IPTV Checker opens its own provider connections while it runs."""

    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "b")])

    def test_a_refusal_during_a_check_is_not_held_against_the_stream(self):
        self.w.dp.tool_probing = True
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [1, 2])
        self.assertEqual(self.w.store.count_recent(0).get("tool_guard"), 1)

    def test_it_counts_again_once_the_check_is_over(self):
        self.w.dp.tool_probing = True
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(60)
        self.w.stop(1)
        self.w.dp.tool_probing = False
        self.w.advance(60)
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(60)
        self.assertEqual(self.w.order(1), [2, 1])

    def test_dead_results_from_a_checker_mean_nothing_here(self):
        # A check can rename, regroup or delete channels. None of that touches the order.
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(60)
        self.w.stop(1)
        self.w.dp.channels[1]["name"] = "Channel [DEAD]"
        self.w.guard_passes(3)
        self.assertEqual(self.w.order(1), [2, 1])
        self.assertIsNotNone(self.w.store.demoted(1, 1))


class WhenAChannelIsBusy(unittest.TestCase):
    """Another writer holds the rows. Nothing waits on them, and nothing is lost."""

    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(1, "a"), (2, "b"), (3, "c")])

    def test_a_failure_is_judged_again_soon(self):
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.dp.busy_writes = 1
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.w.tick(16)
        self.assertEqual(self.w.order(1), [2, 3, 1])

    def test_a_flag_changes_nothing_and_says_so(self):
        token = self.w.make_token()
        self.w.start(1, 1)
        self.w.dp.busy_writes = 1
        self.assertEqual(self.w.service.flag(f"Bearer {token}", {"channel_number": "101"})[:2], (200, {"result": "paused"}))
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.assertEqual(self.w.dp.rotations, [])

    def test_undo_keeps_the_demotion_if_it_cant_move_it(self):
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(47)
        self.w.stop(1)
        self.w.dp.busy_writes = 1
        with self.assertRaises(Exception):
            self.w.service.undo()
        self.assertIsNotNone(self.w.store.demoted(1, 1))
        self.assertEqual(self.w.service.undo(), 1)
        self.assertEqual(self.w.order(1), [1, 2, 3])

    def test_putting_back_tries_again_next_time(self):
        self.w.start(1, 1)
        self.w.switch(1, 1, 2)
        self.w.advance(47)
        self.w.stop(1)
        self.w.tool_rewrites(1, [1, 2, 3])
        self.w.clock.tick(61)
        self.w.service.sweep()
        self.w.dp.busy_writes = 1
        self.w.clock.tick(15)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [1, 2, 3])
        self.w.clock.tick(15)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [2, 3, 1])
