"""What the tests share: a real SQLite database, a fake clock and a fake Dispatcharr.

Nothing here needs Django, Redis, a network or a running server.
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stream_demote import engine  # noqa: E402
from stream_demote.content import ContentChecker  # noqa: E402
from stream_demote.dispatcharr import OrderBusy  # noqa: E402
from stream_demote.service import Service  # noqa: E402
from stream_demote.store import Store  # noqa: E402


class _Cursor:
    def __init__(self, conn):
        self._cur = conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._cur.close()
        return False

    def execute(self, sql, params=()):
        return self._cur.execute(sql.replace("%s", "?"), tuple(params))

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class FakeDispatcharr:
    """Holds channels and order in memory, and records anything that would touch a session."""

    def __init__(self, clock=None):
        self.clock = clock or Clock()
        self.enabled = True
        self.config_id = 1
        self.settings = {"dry_run": False}
        self.channels = {}
        self.streams = {}
        self.orders = {}
        self.links = {}
        self.next_link = 1000
        self.running = {}
        self.clients = {}
        self.no_data = set()
        self.data_since = {}
        self.redis_down = False
        self.broken_rotate = False
        self.hand_edits = {}
        self.hook_live = True
        self.samples = {}
        self.containers = {}
        self.emitted = []
        self.notices = []
        self.rotations = []
        self.refuse_rotation = set()
        self.write_count = 0
        self.tool_probing = False
        self.busy_writes = 0
        self.groups = {}
        self.group_reads = 0

    def add_channel(self, channel_id, number, streams, group=None):
        """streams is a list of (stream_id, account key). Account None means a custom stream."""
        uuid = f"00000000-0000-0000-0000-{channel_id:012d}"
        group_id = None
        if group is not None:
            group_id = self.groups.setdefault(group, len(self.groups) + 1)
        self.channels[channel_id] = {"id": channel_id, "uuid": uuid, "number": float(number),
                                     "name": f"Channel {number}", "group_id": group_id}
        order = []
        for stream_id, account in streams:
            self.streams.setdefault(stream_id, {"account_key": account, "is_custom": account is None})
            order.append(stream_id)
        self.orders[channel_id] = order
        self.links[channel_id] = {stream_id: self._new_link() for stream_id in order}
        return uuid

    def _new_link(self):
        self.next_link += 1
        return self.next_link

    def plugin_state(self):
        return self.enabled, self.settings

    def install_id(self):
        return self.config_id

    def channel(self, channel_uuid):
        return next((c for c in self.channels.values() if c["uuid"] == str(channel_uuid)), None)

    def channel_by_id(self, channel_id):
        return self.channels.get(channel_id)

    def group_name(self, group_id):
        self.group_reads += 1
        return next((name for name, key in self.groups.items() if key == group_id), None)

    def channels_by_number(self, number):
        return [c for c in self.channels.values() if c["number"] == float(number)]

    def stream(self, stream_id):
        return self.streams.get(stream_id)

    def read_order(self, channel_id):
        return [(s, self.streams[s]["is_custom"]) for s in self.orders[channel_id]]

    def read_links(self, channel_ids):
        result = {}
        for channel_id in channel_ids:
            links = self.links.get(channel_id, {})
            result[channel_id] = [
                (s, links.setdefault(s, self._new_link()), self.streams[s]["is_custom"])
                for s in self.orders.get(channel_id, [])
            ]
        return result

    def apply_order(self, channel_id, change):
        if self.busy_writes:
            self.busy_writes -= 1
            raise OrderBusy()
        rows = self.read_order(channel_id)
        before = [s for s, _ in rows]
        after = [s for s, _ in change(rows)]
        if after != before:
            self.write_count += 1
        self.orders[channel_id] = after
        return before, after

    def current_stream(self, channel_uuid):
        return self.running.get(str(channel_uuid))

    def live_state(self, channel_uuid):
        if self.redis_down:
            return None
        uuid = str(channel_uuid)
        stream_id = self.running.get(uuid)
        last = None
        if stream_id and stream_id not in self.no_data:
            last = self.clock()
        return {
            "running": stream_id is not None,
            "stream_id": stream_id,
            "state": "active",
            "container": self.containers.get(stream_id),
            "video_codec": None,
            "audio_codec": None,
            "clients": self.clients.get(uuid, 1 if stream_id else 0),
            "last_data": last,
        }

    def read_sample(self, channel_uuid, stream_id, seconds, not_before=0.0):
        if self.redis_down or self.running.get(str(channel_uuid)) != stream_id:
            return None
        return self.samples.get(stream_id, b"sample")

    def probing_tool_running(self):
        return self.tool_probing

    def hand_edit(self, channel_id):
        return self.hand_edits.get(channel_id)

    def can_see_hand_edits(self):
        return self.hook_live

    def rotate(self, channel_uuid, stream_ids):
        if self.broken_rotate:
            raise RuntimeError("rotate is broken")
        for stream_id in stream_ids:
            if stream_id in self.refuse_rotation:
                continue
            self.rotations.append((channel_uuid, stream_id))
            self.running[str(channel_uuid)] = stream_id
            return stream_id
        return None

    def emit(self, event_type, channel_uuid, channel_name, **details):
        self.emitted.append((event_type, channel_uuid, details))

    def notify(self, key, title, message):
        self.notices.append(message)


def sample_result(**changes):
    """What a clean sample looks like. Tests change the part they care about."""
    result = {
        "has_video": True, "has_audio": True, "audio_known": True, "seconds": 8.0, "black": False,
        "frozen": False, "silent": False, "audible": True, "mean_db": -20.0, "cpu": 0.1,
        "hashes": "moving",
    }
    result.update(changes)
    return result


def moving_hashes(seed=0):
    """Frames that all look different, and different again for each seed."""
    return [((seed * 1000 + index + 1) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF for index in range(16)]


class World:
    """One test's plugin, with a helper for each event Dispatcharr can send."""

    def __init__(self, settings=None):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.clock = Clock()
        self.dp = FakeDispatcharr(self.clock)
        self.dp.settings.update(settings or {})
        self.timers = []
        self.store = Store(lambda: _Cursor(self.conn), vendor="sqlite")
        self.sample_for = {}
        self.sample_calls = 0
        self.ffmpeg_here = True
        self.machine_busy = False
        self.service = Service(
            self.store,
            self.dp,
            clock=self.clock,
            timer=lambda delay, callback: self.timers.append((self.clock.now + delay, callback)),
            report_dir=os.path.join(ROOT, "tests", "__pycache__", "reports"),
            checker=lambda service: ContentChecker(
                service, checker=self._fake_check, ready=lambda: self.ffmpeg_here, busy=lambda: self.machine_busy
            ),
        )
        self.store.ensure()

    def _fake_check(self, data, seconds, timeout=20):
        self.sample_calls += 1
        result = self.sample_for.get(data, sample_result())
        result = result(self.sample_calls) if callable(result) else result
        # A programme keeps changing, so each sample shows something new unless a test says not.
        if result and result["hashes"] == "moving":
            result = dict(result, hashes=moving_hashes(self.sample_calls))
        return result

    def tick(self, seconds=15):
        """The background loop's beat."""
        self.advance(seconds)
        return self.service.tick()

    def tool_rewrites(self, channel_id, order):
        """As Stream-Mapparr's sort or Lineuparr's match: delete the rows and make new ones."""
        self.dp.orders[channel_id] = list(order)
        self.dp.links[channel_id] = {stream_id: self.dp._new_link() for stream_id in order}

    def tool_sorts_in_place(self, channel_id, order):
        """As Lineuparr's Re-sort: the same rows, with new order numbers, and no API call."""
        self.dp.orders[channel_id] = list(order)

    def hand_order(self, channel_id, order):
        """As dragging in Dispatcharr's channel editor: the same rows, through the API."""
        if list(order) != self.dp.orders[channel_id]:
            self.dp.hand_edits[channel_id] = {"at": self.clock.now, "order": list(order)}
        self.dp.orders[channel_id] = list(order)

    def hand_readds(self, channel_id, stream_id, order):
        """Through the API: take a stream out, then put it back in this order. It comes back as a new row."""
        self.dp.links[channel_id][stream_id] = self.dp._new_link()
        self.dp.orders[channel_id] = list(order)
        self.dp.hand_edits[channel_id] = {"at": self.clock.now, "order": list(order)}

    def guard_passes(self, count=2):
        """Enough time for a changed order to be seen, settle, and be acted on."""
        for _ in range(count):
            self.clock.tick(61)
            self.service.sweep()

    def __del__(self):
        self.conn.close()

    def advance(self, seconds):
        """Move time on, running any timer that falls due, as the real timers would."""
        target = self.clock.now + seconds
        while True:
            due = [item for item in self.timers if item[0] <= target]
            if not due:
                break
            item = min(due, key=lambda entry: entry[0])
            self.timers.remove(item)
            self.clock.now = max(self.clock.now, item[0])
            item[1]()
        self.clock.now = target

    def start(self, channel_id, stream_id):
        uuid = self.dp.channels[channel_id]["uuid"]
        self.dp.running[uuid] = stream_id
        self.service.handle_event("channel_start", {"channel_id": uuid, "stream_id": stream_id})

    def switch(self, channel_id, previous, new, reason="max_retries_exceeded", **extra):
        uuid = self.dp.channels[channel_id]["uuid"]
        self.dp.running[uuid] = new
        payload = {"channel_id": uuid, "previous_stream_id": previous, "stream_id": new, "reason": reason}
        payload.update(extra)
        self.service.handle_event("stream_switch", payload)

    def error(self, channel_id, stream_id):
        uuid = self.dp.channels[channel_id]["uuid"]
        self.service.handle_event("channel_error", {"channel_id": uuid, "stream_id": stream_id, "error_type": "connection_failed"})

    def stop(self, channel_id):
        uuid = self.dp.channels[channel_id]["uuid"]
        self.dp.running.pop(uuid, None)
        self.service.handle_event("channel_stop", {"channel_id": uuid, "runtime": 1.0})

    def order(self, channel_id):
        return list(self.dp.orders[channel_id])

    def journal(self, *actions):
        rows = self.store.journal_recent(1000)
        if actions:
            rows = [row for row in rows if row["action"] in actions]
        return rows

    def make_token(self, label="tv"):
        token, problem = self.service.create_token(label)
        assert problem is None
        return token

    def dump_all_tables(self):
        text = []
        for table, in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in self.conn.execute(f"SELECT * FROM {table}").fetchall():
                text.append(repr(row))
        return "\n".join(text)


__all__ = ["World", "engine", "ROOT", "sample_result", "moving_hashes"]
