"""The example button: old-browser syntax, no secrets, and what it does with a fake page."""

import json
import os
import re
import shutil
import subprocess
import unittest

from .harness import ROOT

BUTTON = os.path.join(ROOT, "examples", "jellyfin-button.js")
BEHAVIOUR = os.path.join(ROOT, "tests", "button_behaviour.js")
NODE = os.environ.get("NODE") or shutil.which("node")
# Optional: a path to the acorn package, to parse the file as strict ES2018.
ACORN = os.environ.get("ACORN_PATH")

NEWER_THAN_ES2018 = {
    "optional chaining": r"\?\.[\w(\[]",
    "nullish coalescing": r"\?\?",
    "optional catch binding": r"catch\s*\{",
    "logical assignment": r"(\|\||&&|\?\?)=",
    "numeric separator": r"\d_\d",
    "private field": r"[\s.]#[a-zA-Z]",
    "Array.flat": r"\.flat(Map)?\(",
    "Object.fromEntries": r"fromEntries",
    "String.matchAll": r"matchAll",
    "String.replaceAll": r"replaceAll",
    "String.trimStart": r"trim(Start|End)\(",
    "globalThis": r"globalThis",
    "BigInt": r"BigInt|\d+n\b",
    "Promise.allSettled": r"allSettled",
    "dynamic import": r"\bimport\(",
}


def strip_comments(text):
    return re.sub(r"/\*.*?\*/|^\s*//.*$", "", text, flags=re.S | re.M)


class Syntax(unittest.TestCase):
    def setUp(self):
        with open(BUTTON, encoding="utf-8") as handle:
            self.code = strip_comments(handle.read())

    def test_nothing_newer_than_es2018(self):
        for name, pattern in NEWER_THAN_ES2018.items():
            self.assertIsNone(re.search(pattern, self.code), name)

    def test_holds_no_token_or_admin_credential(self):
        self.assertNotIn("sdf_", self.code)
        self.assertNotIn("ApiKey", self.code)
        self.assertNotIn("X-API-Key", self.code)
        self.assertNotIn("/run/", self.code)

    def test_token_only_goes_in_a_header(self):
        self.assertNotRegex(self.code, r"[?&]token=")

    @unittest.skipUnless(NODE and ACORN, "set NODE and ACORN_PATH to parse with a real ES2018 parser")
    def test_parses_as_es2018(self):
        script = (
            "const acorn=require(process.argv[1]);const fs=require('fs');"
            "acorn.parse(fs.readFileSync(process.argv[2],'utf8'),{ecmaVersion:2018,sourceType:'script'});"
            "console.log('ok')"
        )
        out = subprocess.run([NODE, "-e", script, ACORN, BUTTON], capture_output=True, text=True, check=False)
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


@unittest.skipUnless(NODE, "needs node")
class Behaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([NODE, BEHAVIOUR, BUTTON], capture_output=True, text=True, check=True)
        cls.results = {row["name"]: row for row in map(json.loads, out.stdout.splitlines())}

    def test_sends_only_the_channel_number_with_the_stored_token(self):
        row = self.results["token_saved"]
        self.assertEqual(len(row["sent"]), 1)
        request = row["sent"][0]
        self.assertEqual(request["authorization"], "Bearer sdf_abc")
        self.assertEqual(json.loads(request["body"]), {"channel_number": "101"})
        self.assertEqual(request["credentials"], "omit")
        self.assertEqual(request["method"], "POST")
        self.assertIn("next stream", row["message"])

    def test_asks_once_then_remembers(self):
        row = self.results["prompts_once"]
        self.assertEqual(row["prompts"], 1)
        self.assertEqual(row["stored"], "sdf_new")
        self.assertEqual(row["sent"][0]["authorization"], "Bearer sdf_new")

    def test_cancelling_the_prompt_sends_nothing(self):
        self.assertEqual(self.results["prompt_cancelled"]["sent"], [])

    def test_only_live_tv_sends_anything(self):
        self.assertEqual(self.results["not_live_tv"]["sent"], [])
        self.assertEqual(self.results["nothing_playing"]["sent"], [])

    def test_looks_the_number_up_if_the_session_lacks_it(self):
        self.assertEqual(json.loads(self.results["number_looked_up"]["sent"][0]["body"]), {"channel_number": "7.1"})

    def test_a_refused_token_is_forgotten(self):
        row = self.results["refused_token_is_forgotten"]
        self.assertIsNone(row["stored"])
        self.assertIn("refused", row["message"])

    def test_plain_http_never_gets_a_token(self):
        self.assertEqual(self.results["plain_http_is_refused"]["sent"], [])

    def test_failures_come_back_as_a_message(self):
        for name in ("not_signed_in", "html_reply", "network_failure"):
            self.assertTrue(self.results[name]["message"], name)

    def test_gateway_gets_the_session_and_no_flag_token(self):
        row = self.results["gateway_same_origin"]
        self.assertEqual(row["sent"][0]["authorization"], 'MediaBrowser Token="media-session"')
        self.assertEqual(row["prompts"], 0)

    def test_gateway_on_another_address_never_gets_the_session(self):
        self.assertEqual(self.results["gateway_other_origin_is_refused"]["sent"], [])

    def test_a_limit_says_how_long_it_lasts(self):
        self.assertIn("30 minutes", self.results["limit_says_how_long"]["message"])
        self.assertIn("1 minute.", self.results["limit_a_minute"]["message"])
        self.assertIn("later", self.results["limit_with_no_header"]["message"])

    def test_button_shows_once_on_live_tv(self):
        row = self.results["page_live_tv"]
        self.assertEqual((row["buttons"], row["display"]), (1, ""))
        self.assertEqual(row["title"], "Report a bad stream")

    def test_button_stays_hidden_on_other_videos(self):
        self.assertEqual(self.results["page_movie"]["display"], "none")

    def test_pressing_it_sends_a_flag_and_shows_the_result(self):
        row = self.results["page_live_tv"]
        self.assertEqual(len(row["sent"]), 1)
        self.assertEqual(len(row["toasts"]), 1)
        self.assertIn("next stream", row["toasts"][0])
