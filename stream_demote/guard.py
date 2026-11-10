"""Keeps demotions in place when another tool rewrites a channel's order.

Other tools can replace or sort a channel's streams at any time. There's no event for that,
so the channels with active demotions are compared with how they were left, about once a
minute. If the order changed and stayed changed, this works out who did it:

- The rows were deleted and made again: that's a tool. Stream-Mapparr's Sort and Match, and
  Lineuparr's stream match, do this.
- Someone changed the order through Dispatcharr's API, and the channel is still in the order
  they left it: that's a person. The order stays, and a demoted stream they moved up is let go
  for good. A save that didn't change the order, such as a rename, doesn't count.
- Otherwise, with the same rows: that's a tool working in place, such as Lineuparr's Re-sort.

For a tool, demoted streams go back to the bottom and everything else keeps the tool's order.
"""

from __future__ import annotations

import json
import logging

from . import engine
from .dispatcharr import OrderBusy

logger = logging.getLogger(__name__)


class OrderGuard:
    def __init__(self, service):
        self.service = service

    @property
    def store(self):
        return self.service.store

    @property
    def dp(self):
        return self.service.dp

    def tick(self, cfg, now):
        """Compare every channel with an active demotion, in one query."""
        if not cfg.order_guard:
            return
        for channel_id in self.store.orphan_guards():
            self.store.delete_guard(channel_id)
        records = {}
        for row in self.store.demoted_rows("demoted"):
            records.setdefault(row["channel_id"], []).append(row)
        if not records:
            return
        links = self.dp.read_links(list(records))
        guards = {row["channel_id"]: row for row in self.store.all_guards()}
        for channel_id, rows in records.items():
            current = links.get(channel_id, [])
            # Nearly every channel is as it was left, and this costs no more queries to see.
            if not self._unchanged(guards.get(channel_id), rows, current):
                self._check(channel_id, rows, current, cfg, now)

    def _unchanged(self, guard, records, links):
        if guard is None or guard["pending"] or any(record["missing_since"] for record in records):
            return False
        ids = [item[0] for item in links]
        if ids != json.loads(guard["expected"]):
            return False
        return {str(item[0]): item[1] for item in links} == json.loads(guard["links"])

    def check_channel(self, channel_id, cfg, now, settle=True):
        """Catch up on one channel, before something else writes to it."""
        if not cfg.order_guard:
            return
        rows = self.store.demoted_on_channel(channel_id)
        if rows:
            links = self.dp.read_links([channel_id]).get(channel_id, [])
            self._check(channel_id, rows, links, cfg, now, settle)

    def after_write(self, channel_id, now):
        """We just changed this channel, so this is now how it should look."""
        if not self.store.demoted_on_channel(channel_id):
            self.store.delete_guard(channel_id)
            return
        self._baseline(channel_id, self.dp.read_links([channel_id]).get(channel_id, []), now)

    def _baseline(self, channel_id, links, now):
        self.store.put_guard(channel_id, [item[0] for item in links], {str(item[0]): item[1] for item in links}, now)

    def _check(self, channel_id, records, links, cfg, now, settle=True):
        channel = self.dp.channel_by_id(channel_id)
        if channel is None:
            self._forget_all(channel_id, records, now)
            return
        if self.service.excluded(channel, cfg):
            return
        guard = self.store.guard(channel_id)
        if guard is None:
            self._baseline(channel_id, links, now)
            return
        ids = [item[0] for item in links]
        records = self._note_missing(channel_id, records, set(ids), cfg, now)
        # A tool can leave a channel empty for a moment between deleting and writing.
        if not ids or not records:
            return
        old_links = json.loads(guard["links"])
        new_links = {str(item[0]): item[1] for item in links}
        if ids == json.loads(guard["expected"]):
            if new_links != old_links or guard["pending"]:
                self._baseline(channel_id, links, now)
            return
        seen = json.dumps(ids)
        if guard["pending"] != seen:
            self.store.set_guard_pending(channel_id, seen, now)
            if settle:
                return
        elif settle and now - (guard["pending_at"] or now) < cfg.order_guard_settle_seconds:
            return
        rows_replaced = any(key in old_links and old_links[key] != value for key, value in new_links.items())
        if self._set_by_hand(channel_id, guard, ids, rows_replaced):
            self._accept_hand_order(channel_id, records, links, now)
        else:
            self._reapply(channel, records, links, cfg, now)

    def _set_by_hand(self, channel_id, guard, ids, rows_replaced):
        if not self.dp.can_see_hand_edits():
            # Can't tell, so new rows mean a tool and anything in place is left to the owner.
            return not rows_replaced
        mark = self.dp.hand_edit(channel_id)
        if mark is None or mark["at"] < guard["updated_at"]:
            return False
        if mark["order"] is None:
            return not rows_replaced
        # The order a person saved is still what's there. That holds even when they took a
        # stream out and put it back, which gives it a new row.
        return mark["order"] == ids

    def _note_missing(self, channel_id, records, present, cfg, now):
        """A demoted stream that left the channel is forgotten once it has stayed gone for a while."""
        keep = []
        for record in records:
            stream_id = record["stream_id"]
            if stream_id in present:
                if record["missing_since"]:
                    self.store.set_missing(channel_id, stream_id, None)
                keep.append(record)
            elif not record["missing_since"]:
                self.store.set_missing(channel_id, stream_id, now)
            elif now - record["missing_since"] >= cfg.order_guard_gone_minutes * 60:
                self._forget(channel_id, stream_id, "demotion_gone", record.get("account_key"), now)
        return keep

    def _forget(self, channel_id, stream_id, action, account_key, now, actor="guard"):
        record_since = None
        if action == "hand_release":
            # A person lifting it means it shouldn't carry the record of that demotion.
            record = self.store.demoted(channel_id, stream_id)
            if record:
                record_since = record["state_at"] - self.service.settings().score_window_hours * 3600
        self.store.forget_demoted(channel_id, stream_id)
        self.store.close_demote_rows(channel_id, stream_id, now)
        self.store.void_strikes(channel_id, stream_id, action, record_since=record_since)
        self.store.add_journal(now, action, channel_id, stream_id, account_key, actor=actor)
        logger.info("Stream %s on channel %s is no longer treated as demoted (%s)", stream_id, channel_id, action)

    def _forget_all(self, channel_id, records, now):
        for record in records:
            self._forget(channel_id, record["stream_id"], "demotion_gone", record.get("account_key"), now)
        self.store.delete_guard(channel_id)

    def _accept_hand_order(self, channel_id, records, links, now):
        """A person's order stands. A demoted stream they lifted stays lifted."""
        ids = [item[0] for item in links]
        custom = {item[0] for item in links if item[2]}
        demoted = {record["stream_id"] for record in records}
        for record in records:
            stream_id = record["stream_id"]
            if not engine.still_under_the_rest(ids, custom, demoted, stream_id):
                self._forget(channel_id, stream_id, "hand_release", record.get("account_key"), now, actor="hand")
        self.after_write(channel_id, now)

    def _reapply(self, channel, records, links, cfg, now):
        channel_id = channel["id"]
        rows = [(item[0], item[2]) for item in links]
        kept = {record["stream_id"] for record in records}
        order = [stream_id for stream_id in self.service.bottom_order(channel_id, now) if stream_id in kept]
        before = [item[0] for item in rows]
        after = [item[0] for item in engine.reapply_order(rows, order)]
        if after == before:
            self._baseline(channel_id, links, now)
            return
        if cfg.dry_run:
            # The baseline stays, so this is still waiting if dry run goes off.
            if self.store.once_every(f"dry_guard:{channel_id}", now, 3600):
                self.store.add_journal(now, "would_reapply", channel_id, cause="order_rewritten", actor="guard",
                                       before=before, after=after)
            return
        hits, _wait = self.store.hit(f"guard:{channel_id}", now, 3600)
        if hits > cfg.order_guard_hourly_limit:
            if self.store.once_every(f"guard_stop:{channel_id}", now, 3600):
                self.store.add_journal(now, "guard_stop", channel_id, cause="order_rewritten", actor="guard")
                self.service.warn(
                    f"guard-{channel_id}",
                    "Stream Demote stopped re-applying",
                    f"Something rewrote channel {channel['number']:g} more than "
                    f"{int(cfg.order_guard_hourly_limit)} times in an hour, so demotions there wait until the hour is up.",
                )
            return
        try:
            before, after = self.dp.apply_order(channel_id, lambda current: engine.reapply_order(current, order))
        except OrderBusy:
            # Something is still writing here. The next look will catch it.
            return
        for stream_id in order:
            if stream_id in before:
                self.store.set_position(channel_id, stream_id, before.index(stream_id))
        self.store.add_journal(now, "reapply", channel_id, cause="order_rewritten", actor="guard", before=before, after=after)
        self.after_write(channel_id, now)
        logger.info("Channel %s was rewritten by something else, so %s demoted streams went back down", channel["number"], len(order))
