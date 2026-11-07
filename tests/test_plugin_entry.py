"""The buttons, through the same entry point Dispatcharr calls."""

import importlib.util
import os
import unittest

from .harness import ROOT, World


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("plugin_under_test", os.path.join(ROOT, "plugin.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Buttons(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_module()
        self.w = World()
        self.w.dp.add_channel(1, 101, [(11, "a"), (12, "a"), (13, "a")])
        self.module._service = self.w.service
        self.plugin = self.module.Plugin()

    def run_action(self, action, params=None, **settings):
        return self.plugin.run(action, params or {}, {"settings": settings})

    def demote_one(self):
        self.w.start(1, 11)
        self.w.switch(1, 11, 12)
        self.w.advance(47)
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_event_action_with_no_event_does_nothing(self):
        self.assertEqual(self.run_action("on_event"), {"status": "ok"})
        self.assertEqual(self.w.journal(), [])

    def test_event_action_passes_events_through(self):
        uuid = self.w.dp.channels[1]["uuid"]
        self.run_action("on_event", {"event": "channel_start", "payload": {"channel_id": uuid, "stream_id": 11}})
        self.assertEqual(self.w.store.get_session(uuid)["stream_id"], 11)

    def test_status(self):
        self.assertIn("message", self.run_action("status"))

    def test_recent_lists_the_journal(self):
        self.demote_one()
        result = self.run_action("recent")
        self.assertIn("demote", result["message"])
        self.assertTrue(result["file"].endswith("journal.csv"))
        self.assertEqual(result["rows"][0]["stream_id"], 11)

    def test_undo_last(self):
        self.demote_one()
        self.assertEqual(self.run_action("undo_last")["moved"], 1)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_undo_channel(self):
        self.demote_one()
        self.assertEqual(self.run_action("undo_channel", undo_channel="101")["moved"], 1)
        self.assertEqual(self.w.order(1), [11, 12, 13])

    def test_undo_channel_needs_a_number(self):
        self.assertEqual(self.run_action("undo_channel", undo_channel="")["status"], "error")

    def test_undo_all(self):
        self.demote_one()
        self.assertEqual(self.run_action("undo_all")["moved"], 1)

    def test_token_buttons(self):
        made = self.run_action("create_token", token_label="tv")
        self.assertTrue(made["token"].startswith("sdf_"))
        self.assertEqual(made["file"], made["token"], "the UI keeps file on screen")
        self.assertIn("tv", self.run_action("list_tokens")["message"])
        self.assertEqual(self.run_action("revoke_token", token_label="tv")["revoked"], 1)
        self.assertIn("none", self.run_action("list_tokens")["message"])

    def test_token_needs_a_label(self):
        self.assertEqual(self.run_action("create_token")["status"], "error")

    def test_list_never_shows_a_token_or_its_hash(self):
        made = self.run_action("create_token", token_label="tv")
        rows = self.run_action("list_tokens")["tokens"]
        self.assertNotIn(made["token"], repr(rows))
        self.assertNotIn("token_hash", rows[0])

    def test_erase(self):
        self.demote_one()
        self.run_action("erase_data")
        self.assertEqual(self.w.journal(), [])
        self.assertEqual(self.w.order(1), [12, 13, 11])

    def test_disabling_leaves_the_background_loop_to_idle(self):
        holder = self.module.ticker._holder()
        before = holder.generation
        self.plugin.stop({"reason": "disable"})
        self.assertEqual(holder.generation, before, "enabling again doesn't reload the file, so it must keep going")

    def test_deleting_ends_the_background_loop(self):
        holder = self.module.ticker._holder()
        before = holder.generation
        self.plugin.stop({"reason": "delete"})
        self.assertGreater(holder.generation, before)

    def test_teach_button(self):
        result = self.run_action("teach_placeholder", teach_channel="nope")
        self.assertEqual(result["status"], "error")

    def test_forget_button(self):
        self.w.store.add_picture(5, 1, self.w.clock.now, taught=True)
        self.run_action("forget_placeholders")
        self.assertEqual(self.w.store.known_pictures(), [])

    def test_unknown_action(self):
        self.assertEqual(self.run_action("nope")["status"], "error")

    def test_every_button_in_the_manifest_is_handled(self):
        import json

        with open(os.path.join(ROOT, "plugin.json"), encoding="utf-8") as handle:
            actions = [item["id"] for item in json.load(handle)["actions"]]
        for action in actions:
            result = self.run_action(action, token_label="x", undo_channel="101")
            self.assertNotIn("Unknown action", str(result.get("message")), action)
