"""The only module that touches Dispatcharr's own code.

Everything here reads the database and Redis, writes ChannelStream.order, or asks the
channel's own worker to move its existing connection. Reading costs no provider connection.
The one thing here that can open one is rotate: Dispatcharr then closes the running stream
and opens the next, for a viewer who's still there.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import uuid as uuid_module

logger = logging.getLogger(__name__)

# Dispatcharr's channel buffer, read only. Each chunk is about 256 KB and lasts a minute.
MAX_SAMPLE_BYTES = 32 * 1024 * 1024
MAX_SAMPLE_CHUNKS = 150
EXTRA_SECONDS = 4
HAND_KEY = "stream_demote:hand:{}"
HAND_KEY_SECONDS = 7 * 86400
HOOK_MARK = "_stream_demote_hook"
LOCK_TRIES = 6
LOCK_WAIT_SECONDS = 0.2
_PROBE_MARKER_SECONDS = 600
_PROBE_MARKERS = (
    ("/data/iptv_checker_progress.json", lambda data: data.get("status") == "running"),
    ("/data/stream_mapparr_operation.lock", lambda data: "probe" in str(data.get("action", "")).lower()),
)


class OrderBusy(Exception):
    """Another writer kept hold of a channel's rows for longer than it's worth waiting."""


class Dispatcharr:
    def __init__(self, plugin_key):
        self.plugin_key = plugin_key

    def plugin_state(self):
        """Returns (enabled, settings) from the plugin's saved config."""
        from apps.plugins.models import PluginConfig

        row = PluginConfig.objects.filter(key=self.plugin_key).values("enabled", "settings").first()
        if not row:
            return False, {}
        return bool(row["enabled"]), row["settings"] or {}

    def install_id(self):
        """Dispatcharr deletes this row when the plugin is removed, so a new id means a new install."""
        from apps.plugins.models import PluginConfig

        return PluginConfig.objects.filter(key=self.plugin_key).values_list("id", flat=True).first()

    def channel(self, channel_uuid):
        """Returns {id, uuid, number, name}, or None."""
        from apps.channels.models import Channel

        try:
            value = uuid_module.UUID(str(channel_uuid))
        except ValueError:
            return None
        row = Channel.objects.with_effective_values().filter(uuid=value).first()
        return _channel_dict(row) if row else None

    def channel_by_id(self, channel_id):
        from apps.channels.models import Channel

        row = Channel.objects.with_effective_values().filter(pk=channel_id).first()
        return _channel_dict(row) if row else None

    def group_name(self, group_id):
        """A channel group's name, or None. Only read when a list in the settings names a group."""
        if group_id is None:
            return None
        from apps.channels.models import ChannelGroup

        return ChannelGroup.objects.filter(pk=group_id).values_list("name", flat=True).first()

    def channels_by_number(self, number):
        from apps.channels.models import Channel

        rows = Channel.objects.with_effective_values().filter(effective_channel_number=float(number))
        return [_channel_dict(row) for row in rows]

    def stream(self, stream_id):
        """Returns {account_key, is_custom}, or None. No URLs."""
        from apps.channels.models import Stream

        row = Stream.objects.select_related("m3u_account").filter(pk=stream_id).first()
        if not row:
            return None
        account = row.m3u_account
        if not account:
            return {"account_key": None, "is_custom": bool(row.is_custom)}
        group_id = getattr(account, "server_group_id", None)
        return {
            "account_key": f"g{group_id}" if group_id else f"a{account.id}",
            "is_custom": bool(row.is_custom),
        }

    def read_order(self, channel_id):
        """Returns [(stream_id, is_custom)]. Nothing breaks ties in order, so id does here."""
        from apps.channels.models import ChannelStream

        return list(
            ChannelStream.objects.filter(channel_id=channel_id)
            .order_by("order", "id")
            .values_list("stream_id", "stream__is_custom")
        )

    def read_links(self, channel_ids):
        """Returns {channel_id: [(stream_id, row id, is_custom)]} in order, in one query.

        A tool that deletes and recreates a channel's rows gives them new row ids. An edit
        made in place keeps them. Dispatcharr's own editor edits in place.
        """
        from apps.channels.models import ChannelStream

        result = {channel_id: [] for channel_id in channel_ids}
        rows = (
            ChannelStream.objects.filter(channel_id__in=list(channel_ids))
            .order_by("channel_id", "order", "id")
            .values_list("channel_id", "stream_id", "id", "stream__is_custom")
        )
        for channel_id, stream_id, link_id, is_custom in rows:
            result[channel_id].append((stream_id, link_id, bool(is_custom)))
        return result

    def apply_order(self, channel_id, change):
        """Rewrite a channel's order in one transaction. Returns (before ids, after ids).

        Dispatcharr's channel editor updates these rows in no fixed order, so waiting for a
        lock here could deadlock with it, and one of the two would fail. So this never waits:
        if a row is locked it lets go of everything and tries again in a moment.
        """
        from django.db import OperationalError

        for attempt in range(LOCK_TRIES):
            try:
                return self._apply_order_once(channel_id, change)
            except OperationalError as error:
                if attempt == LOCK_TRIES - 1:
                    raise OrderBusy() from error
                time.sleep(LOCK_WAIT_SECONDS * (attempt + 1))
        raise OrderBusy()

    def _apply_order_once(self, channel_id, change):
        from apps.channels.models import ChannelStream
        from django.db import transaction

        with transaction.atomic():
            locked = list(
                ChannelStream.objects.select_for_update(nowait=True)
                .filter(channel_id=channel_id)
                .order_by("id")
            )
            links = sorted(locked, key=lambda link: (link.order, link.id))
            custom = dict(
                ChannelStream.objects.filter(channel_id=channel_id).values_list("stream_id", "stream__is_custom")
            )
            rows = [(link.stream_id, custom.get(link.stream_id, False)) for link in links]
            before = [stream_id for stream_id, _ in rows]
            after = [stream_id for stream_id, _ in change(rows)]
            if after == before:
                return before, after
            by_stream = {link.stream_id: link for link in links}
            for position, stream_id in enumerate(after):
                by_stream[stream_id].order = position
            ChannelStream.objects.bulk_update(links, ["order"])
        return before, after

    def current_stream(self, channel_uuid):
        """The stream a running session is on right now, or None if it isn't running."""
        live = self.live_state(channel_uuid)
        return live["stream_id"] if live and live["running"] else None

    def live_state(self, channel_uuid):
        """What's happening on a channel right now, from Redis. None if Redis can't be read.

        Other workers write these keys, and reading them changes nothing. A failed read
        must never look like an answer, so callers do nothing when this returns None.
        """
        from core.utils import RedisClient

        try:
            client = RedisClient.get_client()
            if not client:
                return None
            pipe = client.pipeline(transaction=False)
            pipe.hmget(f"live:channel:{channel_uuid}:metadata", ["stream_id", "state", "stream_type", "video_codec", "audio_codec"])
            pipe.scard(f"live:channel:{channel_uuid}:clients")
            pipe.get(f"live:channel:{channel_uuid}:last_data")
            fields, clients, last_data = pipe.execute()
        except Exception:
            logger.debug("Could not read a channel's state from Redis", exc_info=True)
            return None
        stream_id = _as_int(fields[0])
        return {
            "running": stream_id is not None,
            "stream_id": stream_id,
            "state": fields[1],
            "container": fields[2],
            "video_codec": fields[3],
            "audio_codec": fields[4],
            "clients": int(clients or 0),
            "last_data": _as_float(last_data),
        }

    def read_sample(self, channel_uuid, stream_id, seconds, not_before=0.0):
        """Copies the last few seconds of a channel's buffer. Returns bytes, or None for no answer.

        This only reads what Dispatcharr already holds, so no provider connection is involved.
        A chunk can expire or the stream can change while reading: either one gives no answer.
        """
        from core.utils import RedisClient

        base = f"live:channel:{channel_uuid}"
        try:
            client = RedisClient.get_buffer()
            if not client:
                return None
            stamps = client.zrevrangebyscore(
                f"{base}:input:buffer:chunk_timestamps", "+inf", not_before, start=0, num=MAX_SAMPLE_CHUNKS, withscores=True
            )
            if not stamps:
                return None
            wanted = seconds + EXTRA_SECONDS
            newest = float(stamps[0][1])
            cuts = client.zrange(f"{base}:input:buffer:discontinuities", 0, -1)
            last_cut = max((_as_int(item) or 0 for item in cuts), default=0)
            indexes = []
            for member, stamp in stamps:
                index = _as_int(member)
                if index is None or index < last_cut:
                    break
                indexes.append(index)
                if newest - float(stamp) >= wanted:
                    break
            if len(indexes) != indexes[0] - indexes[-1] + 1:
                return None
            pipe = client.pipeline(transaction=False)
            for index in reversed(indexes):
                pipe.get(f"{base}:input:buffer:chunk:{index}")
            chunks = pipe.execute()
            if any(chunk is None for chunk in chunks):
                return None
            now_on = _as_int(client.hget(f"{base}:metadata", "stream_id"))
            if now_on != stream_id:
                return None
            data = b"".join(chunks)
            # Keep the newest part of anything too big. TS packets are 188 bytes.
            if len(data) > MAX_SAMPLE_BYTES:
                keep = MAX_SAMPLE_BYTES - (MAX_SAMPLE_BYTES % 188)
                data = data[-keep:]
            return data
        except Exception:
            logger.debug("Could not read a channel's buffer from Redis", exc_info=True)
            return None

    def rotate(self, channel_uuid, stream_ids):
        """Move a running session to the first stream that can take it. Returns its id or None.

        The session keeps its one connection: Dispatcharr closes the old one and opens the
        new one, so a viewer already holding a seat keeps holding one seat. The exception is
        a stalled stream, where the old connection can stay open for up to 30 seconds more.
        """
        from apps.proxy.live_proxy.services.channel_service import ChannelService
        from apps.proxy.live_proxy.url_utils import get_stream_info_for_switch

        for stream_id in stream_ids:
            info = get_stream_info_for_switch(str(channel_uuid), stream_id)
            if "error" in info:
                continue
            result = ChannelService.change_stream_url(
                str(channel_uuid),
                info["url"],
                info["user_agent"],
                stream_id,
                info.get("m3u_profile_id"),
                stream_name=info.get("stream_name"),
            )
            if result.get("success"):
                return stream_id
            # The channel's worker may still act on this request, so don't send it a second one.
            if result.get("confirmed") is False:
                return None
        return None

    def emit(self, event_type, channel_uuid, channel_name, **details):
        """Write a row other plugins can read. Dispatcharr doesn't pass this name to event hooks."""
        from core.utils import log_system_event

        log_system_event(event_type, channel_id=channel_uuid, channel_name=channel_name, **details)

    def notify(self, key, title, message):
        """A notice admins see in Dispatcharr's own notification list, and as a pop-up if it's open."""
        from core.models import SystemNotification
        from core.utils import send_websocket_notification

        notice, _ = SystemNotification.objects.update_or_create(
            notification_key=f"stream-demote-{key}",
            defaults={
                "notification_type": SystemNotification.NotificationType.WARNING,
                "priority": SystemNotification.Priority.HIGH,
                "title": title,
                "message": message,
                "is_active": True,
                "admin_only": True,
            },
        )
        send_websocket_notification(notice)

    def probing_tool_running(self):
        """Is another plugin opening provider connections of its own right now?

        IPTV Checker and Stream-Mapparr's throughput probe connect to providers without
        Dispatcharr counting it, so a viewer tuning in meanwhile can be refused through no
        fault of the stream. They leave these files while they work. If the files change or
        go away this just says no, and such a refusal is judged like any other.
        """
        now = time.time()
        for path, is_probing in _PROBE_MARKERS:
            try:
                if now - os.path.getmtime(path) > _PROBE_MARKER_SECONDS:
                    continue
                with open(path, encoding="utf-8") as handle:
                    if is_probing(json.load(handle)):
                        return True
            except (OSError, ValueError, AttributeError, TypeError):
                continue
        return False

    # Seeing the owner's own edits

    def hand_edit(self, channel_id):
        """The last time someone changed this channel's order through Dispatcharr's API, if known.

        Returns {"at": time, "order": [stream ids as they left them]}, or None. An entry from an
        older version has no order.
        """
        from core.utils import RedisClient

        try:
            client = RedisClient.get_client()
            raw = client.get(HAND_KEY.format(channel_id)) if client else None
        except Exception:
            return None
        return _parse_hand_edit(raw)

    def can_see_hand_edits(self):
        """False if a Dispatcharr release moved what the hook wraps. Edits in place then count as by hand."""
        try:
            from apps.channels.serializers import ChannelSerializer

            return bool(getattr(ChannelSerializer.update, HOOK_MARK, False))
        except Exception:
            return False


def install_hand_edit_hook():
    """Notes when Dispatcharr's API edits a channel's stream list.

    Tools such as Stream-Mapparr write the table directly and never pass through here, so
    this is how a person's change is told from a tool's. It isn't a supported plugin
    feature. If it breaks, the only effect is that edits in place count as by hand.
    """
    from apps.channels import api_views
    from apps.channels.serializers import ChannelSerializer

    if not getattr(ChannelSerializer.update, HOOK_MARK, False):
        original = ChannelSerializer.update

        @functools.wraps(original)
        def update(self, instance, validated_data):
            # The editor sends the stream list with every save, even a rename, so only a
            # changed order counts.
            before = _orders([instance.pk]) if "streams" in validated_data else None
            result = original(self, instance, validated_data)
            if before is not None:
                _note_if_changed(before)
            return result

        setattr(update, HOOK_MARK, True)
        ChannelSerializer.update = update

    view = api_views.ChannelViewSet
    if not getattr(view.edit_bulk, HOOK_MARK, False):
        original_bulk = view.edit_bulk

        @functools.wraps(original_bulk)
        def edit_bulk(self, request, *args, **kwargs):
            data = getattr(request, "data", None)
            ids = []
            if isinstance(data, list):
                ids = [_as_int(item.get("id")) for item in data if isinstance(item, dict) and "streams" in item]
            before = _orders([channel_id for channel_id in ids if channel_id])
            result = original_bulk(self, request, *args, **kwargs)
            _note_if_changed(before)
            return result

        setattr(edit_bulk, HOOK_MARK, True)
        view.edit_bulk = edit_bulk


def _orders(channel_ids):
    """{channel_id: [stream ids in order]} for these channels. Empty if it can't be read."""
    if not channel_ids:
        return {}
    try:
        from apps.channels.models import ChannelStream

        result = {channel_id: [] for channel_id in channel_ids}
        rows = (
            ChannelStream.objects.filter(channel_id__in=list(channel_ids))
            .order_by("channel_id", "order", "id")
            .values_list("channel_id", "stream_id")
        )
        for channel_id, stream_id in rows:
            result[channel_id].append(stream_id)
        return result
    except Exception:
        logger.debug("Could not read an order around a hand edit", exc_info=True)
        return {}


def _note_if_changed(before):
    after = _orders(list(before))
    for channel_id, order in after.items():
        if order != before.get(channel_id):
            _note_hand_edit(channel_id, order)


def _note_hand_edit(channel_id, order):
    from core.utils import RedisClient

    try:
        client = RedisClient.get_client()
        if client:
            value = json.dumps({"at": time.time(), "order": list(order)})
            client.set(HAND_KEY.format(int(channel_id)), value, ex=HAND_KEY_SECONDS)
    except Exception:
        logger.debug("Could not note a hand edit", exc_info=True)


def _parse_hand_edit(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if raw in (None, ""):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if isinstance(value, (int, float)):
        return {"at": float(value), "order": None}
    if isinstance(value, dict) and isinstance(value.get("at"), (int, float)) and isinstance(value.get("order"), list):
        return {"at": float(value["at"]), "order": [int(item) for item in value["order"]]}
    return None


def _as_int(value):
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _channel_dict(row):
    number = getattr(row, "effective_channel_number", row.channel_number)
    return {
        "id": row.id,
        "uuid": str(row.uuid),
        "number": float(number) if number is not None else None,
        "name": getattr(row, "effective_name", row.name),
        "group_id": getattr(row, "effective_channel_group_id", getattr(row, "channel_group_id", None)),
    }
