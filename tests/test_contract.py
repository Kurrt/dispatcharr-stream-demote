"""Promises that must hold however the code changes."""

import ast
import json
import os
import unittest

from .harness import ROOT, engine
from stream_demote import sampler

SHIPPED = ["plugin.py"] + [
    os.path.join("stream_demote", name)
    for name in sorted(os.listdir(os.path.join(ROOT, "stream_demote")))
    if name.endswith(".py")
]

# Each of these can open a connection to a provider, or to anything else.
BANNED_MODULES = {"requests", "urllib", "urllib2", "urllib3", "http", "httpx", "aiohttp", "socket", "subprocess", "ftplib", "pycurl"}
BANNED_NAMES = {"get_stream", "validate_stream_url", "ffprobe", "ffmpeg", "urlopen", "Popen", "system"}
# The one file that may start a program. Tests below hold it to a local file and no network.
SAMPLER = os.path.join("stream_demote", "sampler.py")

EVENTS_DISPATCHARR_DELIVERS = {
    "channel_start", "channel_stop", "channel_reconnect", "channel_error", "channel_buffering", "channel_failover",
    "stream_switch", "client_connect", "client_disconnect", "recording_start", "recording_end", "epg_refresh",
    "epg_error", "m3u_refresh", "m3u_error", "login_failed", "epg_blocked", "m3u_blocked", "vod_start", "vod_stop",
}


def manifest():
    with open(os.path.join(ROOT, "plugin.json"), encoding="utf-8") as handle:
        return json.load(handle)


def parse(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


class NeverOpensAStream(unittest.TestCase):
    def banned_in(self, path, modules, names):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], modules, path)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], modules, path)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, names, f"{path}:{node.lineno}")
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, names, f"{path}:{node.lineno}")

    def test_no_network_or_probe_code_in_any_shipped_file(self):
        for path in SHIPPED:
            if path != SAMPLER:
                self.banned_in(path, BANNED_MODULES, BANNED_NAMES)

    def test_the_sampler_has_no_network_code_and_no_way_to_learn_a_url(self):
        self.banned_in(SAMPLER, BANNED_MODULES - {"subprocess"}, BANNED_NAMES - {"Popen"})
        with open(os.path.join(ROOT, SAMPLER), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("http", text)
        self.assertNotIn("stream_url", text)

    def test_only_the_sampler_starts_a_program(self):
        starts = [path for path in SHIPPED if "subprocess" in {
            alias.name for node in ast.walk(parse(path)) if isinstance(node, ast.Import) for alias in node.names
        }]
        self.assertEqual(starts, [SAMPLER])

    def test_ffmpeg_only_ever_gets_a_local_file(self):
        for command in (sampler.probe_command("/tmp/x/sample.ts"),
                        sampler.check_command("/tmp/x/sample.ts", "/tmp/x/frames", 6, True, True)):
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(command[command.index("-f") + 1], "mpegts", "no guessing what the bytes are")
            self.assertNotIn("http", " ".join(command))
            self.assertIn("/tmp/x/sample.ts", command)

    def test_the_sampler_is_gentle(self):
        command = sampler.check_command("/tmp/x/sample.ts", "/tmp/x/frames", 6, True, True)
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertIn("-nostdin", command)

    def test_rotate_always_names_its_target_stream(self):
        # With no target, get_stream_info_for_switch would reserve a stream itself.
        tree = parse(os.path.join("stream_demote", "dispatcharr.py"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "get_stream_info_for_switch"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].args), 2)


class Manifest(unittest.TestCase):
    def test_required_store_fields(self):
        data = manifest()
        for name in ("name", "version", "description", "author", "license"):
            self.assertTrue(data.get(name), name)
        self.assertEqual(data["license"], "MIT")
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")

    def test_field_defaults_match_the_code(self):
        fields = {f["id"]: f for f in manifest()["fields"] if f["type"] != "info"}
        self.assertEqual(set(fields), set(engine.DEFAULTS))
        for name, default in engine.DEFAULTS.items():
            self.assertEqual(fields[name]["default"], default, name)

    def test_only_real_events_are_subscribed(self):
        for action in manifest()["actions"]:
            for event in action.get("events", []):
                self.assertIn(event, EVENTS_DISPATCHARR_DELIVERS)

    def test_dry_run_is_on_by_default(self):
        self.assertIs(engine.DEFAULTS["dry_run"], True)

    def test_content_checks_are_off_by_default(self):
        self.assertIs(engine.DEFAULTS["content_enabled"], False)
        field = next(f for f in manifest()["fields"] if f["id"] == "content_enabled")
        self.assertIs(field["default"], False)


class PublicText(unittest.TestCase):
    def test_no_url_field_of_an_event_is_read(self):
        for path in SHIPPED:
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, ("stream_url", "channel_url", "new_url"), path)
                    if not path.endswith("dispatcharr.py"):
                        self.assertNotEqual(node.value, "url", path)
