"""The background loop: it must outlive failures, and stop when it's no longer wanted."""

import unittest
from unittest import mock

from .harness import ROOT  # noqa: F401
from stream_demote import ticker


class FakeService:
    def __init__(self, answers):
        self.answers = list(answers)
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TheLoop(unittest.TestCase):
    def run_loop(self, service, tidy=None):
        holder = ticker._holder()
        holder.generation += 1
        with mock.patch.object(ticker.time, "sleep", lambda _seconds: None):
            ticker._loop(lambda: service, tidy, holder.generation)

    def test_it_ends_when_the_plugin_has_gone(self):
        service = FakeService([True, True, False])
        self.run_loop(service)
        self.assertEqual(service.ticks, 3)

    def test_a_failure_doesnt_end_it(self):
        service = FakeService([RuntimeError("database away"), True, False])
        with self.assertLogs(ticker.logger, level="ERROR"):
            self.run_loop(service)
        self.assertEqual(service.ticks, 3)

    def test_it_tidies_up_each_time(self):
        calls = []
        self.run_loop(FakeService([True, False]), lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_a_failure_tidying_up_doesnt_end_it(self):
        service = FakeService([True, False])
        self.run_loop(service, lambda: (_ for _ in ()).throw(RuntimeError("no")))
        self.assertEqual(service.ticks, 2)

    def test_an_old_loop_ends_when_a_new_one_starts(self):
        service = FakeService([True])
        holder = ticker._holder()
        holder.generation += 1
        mine = holder.generation

        def replaced(_seconds):
            holder.generation = mine + 1

        with mock.patch.object(ticker.time, "sleep", replaced):
            ticker._loop(lambda: service, None, mine)
        self.assertEqual(service.ticks, 0)

    def test_it_only_runs_where_streams_are_served(self):
        self.assertFalse(ticker.should_run(), "no Dispatcharr here, so no")
