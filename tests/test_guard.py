"""Other tools rewrite a channel's order. Demotions come back, and the owner's own changes stick."""

import unittest

from .harness import World

A, B, C = "a1", "a2", "a3"


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.dp.add_channel(1, 101, [(11, A), (12, A), (13, B), (14, C)])
        self.w.start(1, 11)
        self.w.switch(1, 11, 12, reason="max_retries_exceeded")
        self.w.advance(47)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])
        self.writes = self.w.dp.write_count

    def writes_since(self):
        return self.w.dp.write_count - self.writes


class SortingTools(Base):
    def test_a_tool_rewriting_the_rows_is_put_right(self):
        self.w.tool_rewrites(1, [14, 11, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 13, 12, 11], "the tool's order, with the demoted stream last")
        row = self.w.journal("reapply")[0]
        self.assertEqual(row["cause"], "order_rewritten")
        self.assertEqual(row["actor"], "guard")

    def test_a_tool_sorting_in_place_is_put_right(self):
        self.w.tool_sorts_in_place(1, [11, 14, 12, 13])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 12, 13, 11])

    def test_it_waits_for_the_change_to_settle(self):
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.clock.tick(61)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14], "the first look only notes it")
        self.w.tool_rewrites(1, [11, 14, 13, 12])
        self.w.clock.tick(15)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [11, 14, 13, 12], "still changing, so still waiting")
        self.w.clock.tick(15)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [14, 13, 12, 11])

    def test_no_write_when_a_tool_leaves_it_right(self):
        self.w.tool_rewrites(1, [13, 12, 14, 11])
        self.w.guard_passes()
        self.assertEqual(self.writes_since(), 0)
        self.assertEqual(self.w.journal("reapply"), [])

    def test_nothing_is_written_when_nothing_changed(self):
        self.w.guard_passes(5)
        self.assertEqual(self.writes_since(), 0)

    def test_one_tool_run_means_one_write(self):
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes(5)
        self.assertEqual(self.writes_since(), 1)

    def test_a_tool_that_keeps_rewriting_is_only_answered_so_often(self):
        self.w.dp.settings["order_guard_hourly_limit"] = 3
        for _ in range(12):
            self.w.tool_rewrites(1, [11, 12, 13, 14])
            self.w.guard_passes()
        self.assertEqual(self.writes_since(), 3)
        self.assertEqual(len(self.w.journal("guard_stop")), 1)
        self.assertEqual(len(self.w.dp.notices), 1)

    def test_it_starts_again_when_the_hour_is_up(self):
        self.w.dp.settings["order_guard_hourly_limit"] = 1
        for _ in range(3):
            self.w.tool_rewrites(1, [11, 12, 13, 14])
            self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.w.clock.tick(3601)
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])

    def test_a_moment_with_no_rows_changes_nothing(self):
        # Stream-Mapparr deletes the rows, then writes them, with no transaction around both.
        self.w.tool_rewrites(1, [])
        self.w.guard_passes(1)
        self.assertEqual(self.w.store.demoted(1, 11)["state"], "demoted")
        self.w.tool_rewrites(1, [14, 11, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 13, 12, 11])
        self.assertIsNone(self.w.store.demoted(1, 11)["missing_since"])

    def test_it_goes_back_to_where_the_tool_ranked_it(self):
        self.w.tool_rewrites(1, [14, 11, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.store.demoted(1, 11)["from_position"], 1)
        self.w.advance(60)
        self.w.start(1, 11)
        self.w.advance(601)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [14, 11, 13, 12])

    def test_several_demotions_come_back_in_the_order_they_fell(self):
        self.w.advance(400)
        self.w.start(1, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [13, 14, 11, 12])
        self.w.tool_rewrites(1, [12, 11, 14, 13])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 13, 11, 12])

    def test_a_stream_a_tool_added_goes_above_a_demoted_one(self):
        self.w.dp.streams[15] = {"account_key": A, "is_custom": False}
        self.w.tool_sorts_in_place(1, [12, 13, 14, 11, 15])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 15, 11])

    def test_a_fallback_card_stays_last(self):
        w = World()
        w.dp.add_channel(1, 101, [(11, A), (12, B), (9, None)])
        w.start(1, 11)
        w.switch(1, 11, 12)
        w.advance(47)
        w.stop(1)
        w.tool_rewrites(1, [11, 12, 9])
        w.guard_passes()
        self.assertEqual(w.order(1), [12, 11, 9])

    def test_a_demotion_catches_up_with_a_rewrite_it_hasnt_seen_yet(self):
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.advance(60)
        self.w.start(1, 12)
        self.w.switch(1, 12, 13)
        self.w.advance(47)
        self.w.stop(1)
        self.assertEqual(self.w.order(1), [13, 14, 11, 12])

    def test_a_shared_event_is_not_sent_again(self):
        before = len(self.w.dp.emitted)
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(len(self.w.dp.emitted), before)

    def test_it_doesnt_count_towards_the_demotion_limits(self):
        self.w.dp.settings["global_hourly_limit"] = 1
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])

    def test_it_can_be_turned_off(self):
        self.w.dp.settings["order_guard"] = False
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])

    def test_a_stream_on_a_second_chance_is_left_where_the_tool_puts_it(self):
        self.w.advance(72 * 3600 + 700)
        self.w.service.sweep()
        self.assertEqual(self.w.store.demoted(1, 11)["state"], "parole")
        self.w.tool_rewrites(1, [13, 12, 11, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [13, 12, 11, 14])


class TheOwner(Base):
    def test_a_hand_reorder_of_the_demoted_stream_sticks(self):
        self.w.hand_order(1, [11, 12, 13, 14])
        self.w.guard_passes(4)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertIsNone(self.w.store.demoted(1, 11))
        self.assertEqual(len(self.w.journal("hand_release")), 1)
        self.assertEqual(self.writes_since(), 0)

    def test_it_also_sticks_through_a_later_tool_run(self):
        self.w.hand_order(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.w.tool_rewrites(1, [14, 13, 12, 11])
        self.w.guard_passes()
        self.w.tool_rewrites(1, [11, 14, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 14, 13, 12], "no demotion is left to put back")

    def test_reordering_only_the_others_keeps_the_demotion(self):
        self.w.hand_order(1, [14, 13, 12, 11])
        self.w.guard_passes(3)
        self.assertEqual(self.w.order(1), [14, 13, 12, 11])
        self.assertIsNotNone(self.w.store.demoted(1, 11))
        self.w.tool_rewrites(1, [11, 13, 12, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [13, 12, 14, 11])

    def test_a_real_failure_later_can_still_demote_it(self):
        self.w.hand_order(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.w.advance(100)
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])

    def test_undo_sticks_through_a_tool_run(self):
        self.w.service.undo(channel_id=1)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.w.tool_rewrites(1, [14, 11, 13, 12])
        self.w.guard_passes(3)
        self.assertEqual(self.w.order(1), [14, 11, 13, 12])
        self.assertIsNone(self.w.store.guard(1))

    def test_undo_notices_a_hand_change_first(self):
        self.w.hand_order(1, [12, 11, 13, 14])
        self.assertEqual(self.w.service.undo(limit=1), 0)
        self.assertEqual(self.w.order(1), [12, 11, 13, 14])

    def test_a_rewrite_with_new_rows_beats_a_hand_edit_it_followed(self):
        self.w.hand_order(1, [11, 12, 13, 14])
        self.w.tool_rewrites(1, [14, 11, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 13, 12, 11])

    def test_a_save_that_changes_no_order_doesnt_count(self):
        # Dispatcharr's editor sends the stream list with a rename or a guide change too.
        self.w.hand_order(1, [12, 13, 14, 11])
        self.w.guard_passes()
        self.w.tool_sorts_in_place(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])
        self.assertIsNotNone(self.w.store.demoted(1, 11))

    def test_a_tool_sort_after_a_hand_change_is_still_put_right(self):
        self.w.hand_order(1, [13, 12, 14, 11])
        self.w.tool_sorts_in_place(1, [11, 14, 13, 12])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [14, 13, 12, 11], "the order isn't the one the person left")
        self.assertIsNotNone(self.w.store.demoted(1, 11))

    def test_taking_a_stream_out_and_putting_it_back_on_top_sticks(self):
        self.w.hand_readds(1, 11, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertIsNone(self.w.store.demoted(1, 11))
        self.assertEqual(len(self.w.journal("hand_release")), 1)

    def test_a_mark_from_an_older_version_still_works_for_an_edit_in_place(self):
        self.w.dp.orders[1] = [11, 12, 13, 14]
        self.w.dp.hand_edits[1] = {"at": self.w.clock.now, "order": None}
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertIsNone(self.w.store.demoted(1, 11))

    def test_with_no_way_to_see_hand_edits_an_edit_in_place_is_left_alone(self):
        self.w.dp.hook_live = False
        self.w.tool_sorts_in_place(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertIsNone(self.w.store.demoted(1, 11))

    def test_with_no_way_to_see_hand_edits_new_rows_still_mean_a_tool(self):
        self.w.dp.hook_live = False
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])


class LeftAlone(Base):
    def test_channels_to_leave_alone_are_never_put_right(self):
        self.w.dp.settings["excluded_channels"] = "101"
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes(4)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertEqual(self.writes_since(), 0)

    def test_dry_run_only_writes_it_down(self):
        self.w.dp.settings["dry_run"] = True
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes(5)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])
        self.assertEqual(len(self.w.journal("would_reapply")), 1)
        self.assertEqual(self.writes_since(), 0)

    def test_turning_dry_run_off_catches_up(self):
        self.w.dp.settings["dry_run"] = True
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.w.dp.settings["dry_run"] = False
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])


class StreamsAndChannelsGoing(Base):
    def test_a_stream_removed_for_good_is_forgotten(self):
        # A changed URL on an account hashing by URL ends up here, when the old row is deleted.
        self.w.tool_rewrites(1, [12, 13, 14])
        self.w.clock.tick(61)
        self.w.service.sweep()
        self.assertIsNotNone(self.w.store.demoted(1, 11), "not after one look")
        self.w.clock.tick(601)
        self.w.service.sweep()
        self.assertIsNone(self.w.store.demoted(1, 11))
        self.assertEqual(len(self.w.journal("demotion_gone")), 1)
        self.assertEqual(self.w.service.undo(channel_id=1), 0)

    def test_a_stream_that_comes_back_in_time_stays_demoted(self):
        self.w.tool_rewrites(1, [12, 13, 14])
        self.w.guard_passes(1)
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.guard_passes()
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])
        self.assertIsNone(self.w.store.demoted(1, 11)["missing_since"])

    def test_a_new_stream_id_starts_clean(self):
        # Same stream, new row: nothing links the two, so it isn't demoted.
        self.w.dp.streams[111] = {"account_key": A, "is_custom": False}
        self.w.tool_rewrites(1, [111, 12, 13, 14])
        self.w.clock.tick(61)
        self.w.service.sweep()
        self.w.clock.tick(661)
        self.w.service.sweep()
        self.assertEqual(self.w.order(1), [111, 12, 13, 14])
        self.assertIsNone(self.w.store.demoted(1, 111))

    def test_a_deleted_channel_is_forgotten(self):
        del self.w.dp.channels[1]
        del self.w.dp.orders[1]
        self.w.guard_passes(1)
        self.assertIsNone(self.w.store.demoted(1, 11))
        self.assertIsNone(self.w.store.guard(1))


class TheLoop(Base):
    def test_a_tick_runs_the_check_with_no_events_at_all(self):
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.tick(61)
        self.w.tick(15)
        self.assertEqual(self.w.order(1), [12, 13, 14, 11])

    def test_only_one_worker_acts_in_a_tick(self):
        calls = []
        real = self.w.service.sweep
        self.w.service.sweep = lambda cfg=None: calls.append(1) or real(cfg)
        self.w.tick(15)
        self.w.service.tick()
        self.assertEqual(len(calls), 1)

    def test_a_tick_does_nothing_when_disabled(self):
        self.w.dp.enabled = False
        self.w.tool_rewrites(1, [11, 12, 13, 14])
        self.w.tick(61)
        self.w.tick(15)
        self.assertEqual(self.w.order(1), [11, 12, 13, 14])

    def test_a_tick_says_stop_when_the_plugin_is_gone(self):
        self.w.dp.enabled = False
        self.w.dp.config_id = None
        self.assertIs(self.w.tick(15), False)


class TheHandEditMark(unittest.TestCase):
    def test_reading_what_the_hook_wrote(self):
        from stream_demote.dispatcharr import _parse_hand_edit

        self.assertEqual(_parse_hand_edit(b'{"at": 5.5, "order": [3, 1, 2]}'), {"at": 5.5, "order": [3, 1, 2]})
        self.assertEqual(_parse_hand_edit(b"1700000000.25"), {"at": 1700000000.25, "order": None}, "an older version's")
        for bad in (None, b"", b"nonsense", b'{"at": "x"}', b'{"at": 1}', b"[1, 2]"):
            self.assertIsNone(_parse_hand_edit(bad), bad)
