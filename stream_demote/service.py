"""Turns Dispatcharr's events into order changes, by way of the rules in engine.py."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import secrets
import threading
import time
import uuid as uuid_module

from . import engine
from .content import ContentChecker
from .dispatcharr import OrderBusy
from .guard import OrderGuard

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "sdf_"
TICK_SECONDS = 14
RUN_SWEEP_SECONDS = 600
EXPIRE_SECONDS = 1800
DUPLICATE_SECONDS = 5
FRESH_DATA_SECONDS = 10
STALLED_SECONDS = 2
RECHECK_SECONDS = 15
GUARD_BUCKET_SECONDS = 600
RECENT_FAILURE_SECONDS = 300
LOG_TRACE_EVERY_SECONDS = 600
REPORT_DIR = "/data/plugin_reports/stream-demote"
NUMBER_PATTERN = re.compile(r"^\d{1,6}(\.\d{1,3})?$")


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _default_timer(delay, callback):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


def _safe_cell(value):
    # A label starting with one of these would run as a formula in a spreadsheet.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


class Service:
    def __init__(self, store, dispatcharr, clock=time.time, timer=_default_timer, report_dir=REPORT_DIR, after_timer=None, checker=None):
        self.store = store
        self.dp = dispatcharr
        self.clock = clock
        self.timer = timer
        self.report_dir = report_dir
        self.after_timer = after_timer
        self.guard = OrderGuard(self)
        self.content = checker(self) if checker else ContentChecker(self)
        self._install_checked = False
        self._last_trace = {}

    def settings(self):
        return engine.Settings(self.dp.plugin_state()[1])

    def ready(self):
        """Make the tables, and notice if this is a new install on top of old data."""
        self.store.ensure()
        if self._install_checked:
            return
        self._install_checked = True
        current = self.dp.install_id()
        if current is None:
            return
        saved = self.store.meta("install_id")
        if saved is not None and saved != str(current):
            now = self.clock()
            self.store.forget_install(now)
            self.store.add_journal(now, "reinstalled", actor="install")
            logger.warning("Stream Demote was removed and put back: old tokens and demotions were cleared")
        self.store.set_meta("install_id", current)

    def excluded(self, channel, cfg):
        return engine.in_channel_list(channel, cfg.excluded_channels, lambda: self._group_name(channel))

    def content_skipped(self, channel, cfg):
        """Left alone, or listed under Skip content checks on, such as event channels with slates."""
        return self.excluded(channel, cfg) or engine.in_channel_list(
            channel, cfg.content_skip_channels, lambda: self._group_name(channel))

    def _group_name(self, channel):
        try:
            return self.dp.group_name(channel.get("group_id"))
        except Exception as error:
            self.note_problem("group lookup", error)
            return None

    def warn(self, key, title, message):
        logger.warning(message)
        try:
            self.dp.notify(key, title, message)
        except Exception:
            logger.debug("Could not send a notice", exc_info=True)

    def note_problem(self, kind, error):
        """Log a failure without flooding the log, and count it so Status can say so."""
        now = self.clock()
        if now - self._last_trace.get(kind, 0) > LOG_TRACE_EVERY_SECONDS:
            self._last_trace[kind] = now
            logger.error("Stream Demote %s failed", kind, exc_info=error)
        else:
            logger.warning("Stream Demote %s failed again: %s", kind, type(error).__name__)
        try:
            self.store.hit(f"errors:{kind}", now, 3600)
        except Exception:
            logger.debug("Could not count a problem", exc_info=True)

    # Events

    def handle_event(self, event, payload):
        try:
            self._handle_event(event, payload)
        except Exception as error:
            # Failures here must never reach the proxy that sent the event.
            self.note_problem("events", error)

    def _handle_event(self, event, payload):
        self.ready()
        cfg = self.settings()
        channel_uuid = str(payload.get("channel_id") or "")
        if event == "channel_start":
            self._on_start(channel_uuid, payload)
        elif event == "stream_switch":
            self._on_switch(channel_uuid, payload, cfg)
        elif event == "channel_error":
            self._on_error(channel_uuid, payload, cfg)
        elif event == "channel_stop":
            self._on_stop(channel_uuid, cfg)
        self.sweep(cfg)

    def _on_start(self, channel_uuid, payload):
        stream_id = _as_int(payload.get("stream_id"))
        channel = self.dp.channel(channel_uuid)
        if channel and stream_id:
            info = self.dp.stream(stream_id) or {}
            self.store.put_session(channel_uuid, channel["id"], stream_id, info.get("account_key"), self.clock())

    def _on_switch(self, channel_uuid, payload, cfg):
        now = self.clock()
        channel = self.dp.channel(channel_uuid)
        if not channel:
            return
        previous = _as_int(payload.get("previous_stream_id"))
        new = _as_int(payload.get("stream_id"))
        reason = payload.get("reason") or "unknown"
        session = self.store.get_session(channel_uuid)
        played = None
        attempt_at = now
        if session and (previous is None or session["stream_id"] == previous):
            previous = session["stream_id"]
            played = now - session["since"]
            attempt_at = session["since"]
        if new:
            info = self.dp.stream(new) or {}
            self.store.put_session(channel_uuid, channel["id"], new, info.get("account_key"), now)
        if not previous or previous == new:
            return
        if reason in engine.FAILURE_REASONS:
            self._record_failure(channel, previous, reason, played, attempt_at, cfg, now)
            # Soon after, see whether the stream it moved to is actually delivering.
            self.timer(cfg.proof_seconds + 1, lambda: self._timer_proof(channel["id"], channel_uuid))
        elif session and session["stream_id"] == previous:
            self._credit_play(channel, previous, session["since"], now, cfg)

    def _on_error(self, channel_uuid, payload, cfg):
        # An error on its own isn't a strike: it also fires just before a normal failover.
        # It's noted so that "every stream on this channel failed" can be seen.
        now = self.clock()
        stream_id = _as_int(payload.get("stream_id"))
        channel = self.dp.channel(channel_uuid)
        if not channel or not stream_id or self.excluded(channel, cfg):
            return
        cause = "error"
        if self.store.failure_seen(channel["id"], stream_id, cause, now - DUPLICATE_SECONDS):
            return
        info = self.dp.stream(stream_id) or {}
        self.store.add_failure(now, channel_uuid, channel["id"], stream_id, info.get("account_key"),
                               cause, 0.0, "noted", outcome="noted")

    def _on_stop(self, channel_uuid, cfg):
        now = self.clock()
        session = self.store.get_session(channel_uuid)
        if not session:
            return
        self.store.delete_session(channel_uuid)
        if session["account_key"]:
            self.store.add_stop(session["account_key"], session["channel_id"], now)
        channel = self.dp.channel(channel_uuid)
        if channel:
            self._credit_play(channel, session["stream_id"], session["since"], now, cfg)

    def _record_failure(self, channel, stream_id, reason, played, attempt_at, cfg, now):
        info = self.dp.stream(stream_id)
        if not info or info["is_custom"] or self.excluded(channel, cfg):
            return
        if self.store.failure_seen(channel["id"], stream_id, reason, now - DUPLICATE_SECONDS):
            return
        key = info["account_key"]
        weight = engine.failure_weight(reason, played, cfg)
        guard = self._guarded(key, channel["id"], attempt_at, now, cfg)
        if guard and self._let_off_too_often(channel["id"], stream_id, cfg, now):
            guard = None
        if not guard and self.dp.probing_tool_running():
            guard = "tool_guard"
        if guard:
            self.store.add_failure(now, channel["uuid"], channel["id"], stream_id, key, reason, 0.0, "guarded",
                                   outcome=guard, attempt_at=attempt_at)
            logger.info("Stream %s on channel %s failed (%s) but was not counted: %s", stream_id, channel["number"], reason, guard)
        else:
            record = self.store.demoted(channel["id"], stream_id)
            if record and record["state"] == "parole":
                # It had its second chance and failed it.
                weight = max(weight, cfg.demote_score)
            hold = cfg.effective_hold
            self.store.add_failure(now, channel["uuid"], channel["id"], stream_id, key, reason, weight, "pending",
                                   due_at=now + hold, attempt_at=attempt_at)
            self.timer(hold + 1, self._timer_sweep)
        if reason == "health_monitor" and key:
            self.store.set_stall(key, now)

    def _guarded(self, key, channel_id, attempt_at, failed_at, cfg):
        """Why a failure shouldn't be held against its stream, or None."""
        if not key:
            return None
        if engine.stall_excuses(attempt_at, failed_at, self.store.account(key).get("stall_at"), cfg):
            return "stall_guard"
        # A channel stopping only explains a refusal on a different channel.
        start, end = engine.zap_window(attempt_at, failed_at, cfg)
        return "zap_guard" if self.store.other_channel_stopped(key, channel_id, start, end) else None

    def _let_off_too_often(self, channel_id, stream_id, cfg, now):
        """A dead stream that's only ever tried straight after a zap would be let off for good."""
        if cfg.guard_repeat_limit <= 0:
            return False
        since = now - cfg.score_window_hours * 3600
        return self.store.guarded_buckets(channel_id, stream_id, since, GUARD_BUCKET_SECONDS) + 1 >= cfg.guard_repeat_limit

    def _credit_play(self, channel, stream_id, since, now, cfg):
        """A demoted stream that played well since it was demoted earns its place back."""
        if now - since < cfg.earn_back_minutes * 60:
            return
        record = self.store.demoted(channel["id"], stream_id)
        if not record or since < record["state_at"]:
            return
        if self.excluded(channel, cfg):
            return
        # Playing isn't enough if it was demoted for what it showed: the picture must be good too.
        if cfg.content_enabled and str(record.get("cause") or "").startswith(engine.CONTENT_PREFIX):
            if self.store.clean_sample_count(channel["id"], stream_id, since) < 2:
                return
        if cfg.dry_run:
            self._dry_note("would_restore_earned", channel, stream_id, record, now)
            return
        self.guard.check_channel(channel["id"], cfg, now, settle=False)
        record = self.store.demoted(channel["id"], stream_id)
        if not record:
            return
        if record["state"] == "demoted":
            self._move_back(channel, stream_id, record, "restore_earned", "play", now, cfg)
        self.store.forget_demoted(channel["id"], stream_id)
        self.store.close_demote_rows(channel["id"], stream_id, now)
        self.store.void_strikes(channel["id"], stream_id, "earned_back")
        self.guard.after_write(channel["id"], now)

    def _dry_note(self, action, channel, stream_id, record, now):
        """In dry run a restore only ever gets written down, and only once a day."""
        if self.store.once_every(f"{action}:{channel['id']}:{stream_id}", now, 86400):
            self.store.add_journal(now, action, channel["id"], stream_id, record.get("account_key"), record.get("cause"), "dry_run")

    # The background loop

    def tick(self):
        """Run by the background loop. Returns False if the plugin has gone."""
        enabled, raw = self.dp.plugin_state()
        if not enabled:
            return self.dp.install_id() is not None
        self.ready()
        now = self.clock()
        if not self.store.once_every("tick", now, TICK_SECONDS):
            return True
        cfg = engine.Settings(raw)
        self.sweep(cfg)
        self.content.tick(cfg, self.clock())
        return True

    def _timer_sweep(self):
        try:
            enabled, raw = self.dp.plugin_state()
            if enabled:
                self.ready()
                self.sweep(engine.Settings(raw))
        except Exception as error:
            self.note_problem("timers", error)
        finally:
            if self.after_timer:
                self.after_timer()

    def _timer_proof(self, channel_id, channel_uuid):
        try:
            enabled, raw = self.dp.plugin_state()
            if enabled:
                self.ready()
                self._note_proof(channel_id, channel_uuid, engine.Settings(raw), self.clock())
        except Exception as error:
            self.note_problem("timers", error)
        finally:
            if self.after_timer:
                self.after_timer()

    def _note_proof(self, channel_id, channel_uuid, cfg, now):
        """If the channel has moved on and data is arriving, the streams before it are answered for."""
        live = self.dp.live_state(channel_uuid)
        session = self.store.get_session(channel_uuid)
        if not live or not live["running"] or not session or session["stream_id"] != live["stream_id"]:
            return
        if now - session["since"] < cfg.proof_seconds or not self._flowing(live, session, now):
            return
        self.store.mark_proof(channel_id, live["stream_id"], now)

    def _flowing(self, live, session, now):
        last = live.get("last_data")
        return last is not None and last > session["since"] and now - last <= FRESH_DATA_SECONDS

    # Judging held failures

    def sweep(self, cfg=None):
        cfg = cfg or self.settings()
        now = self.clock()
        for row in self.store.waiting_channels():
            self._note_proof(row["channel_id"], row["channel_uuid"], cfg, now)
        for row in self.store.due_failures(now):
            if self.store.claim_failure(row["id"]):
                self._judge(row, cfg, now)
        if self.store.guards_with_pending() or self.store.once_every("guard", now, cfg.order_guard_seconds):
            self.guard.tick(cfg, now)
        if self.store.once_every("sweep", now, RUN_SWEEP_SECONDS):
            self._sweep_paroles(cfg, now)
            self._sweep_running_plays(cfg, now)
            self.store.prune(now, cfg.journal_days)

    def _facts(self, row, rows, channel, cfg, now):
        key = row["account_key"]
        stream_id = row["stream_id"]
        channel_id = row["channel_id"]
        eligible = engine.eligible_ids(rows)
        failed = self.store.failed_streams_on_channel(channel_id, now - cfg.channel_problem_minutes * 60)
        account = self.store.account(key) if key else {}
        streams, channels = (
            self.store.outage_spread(key, row["at"] - cfg.outage_window_seconds) if key else (0, 0)
        )
        giveup = cfg.outage_giveup_hours * 3600
        since = account.get("outage_since")
        return engine.Facts(
            stream_present=stream_id in [item[0] for item in rows],
            excluded=self.excluded(channel, cfg),
            eligible=len(eligible),
            already_last=engine.is_last_eligible(rows, stream_id),
            all_failed=bool(eligible) and set(eligible) <= failed,
            paused=(account.get("paused_until") or 0) > now,
            long_outage=bool(giveup and since and now - since >= giveup),
            healthy=self._account_healthy(key, stream_id, cfg, now),
            outage_streams=streams,
            outage_channels=channels,
            chained=bool(key) and self.store.other_failure_near(
                key, stream_id, row["at"] - cfg.chain_seconds, row["at"] + cfg.chain_seconds),
            repeated=self.store.failed_earlier(
                channel_id, stream_id, now - cfg.score_window_hours * 3600, row["at"] - cfg.outage_window_seconds),
            proven=self._is_proven(row, channel_id, stream_id),
            score=self.store.score(channel_id, stream_id, now - cfg.score_window_hours * 3600),
            channel_burst=self.store.demote_count(now - cfg.channel_burst_minutes * 60, channel_id),
            channel_day=self.store.demote_count(now - 86400, channel_id),
            global_hour=self.store.demote_count(now - 3600),
        )

    def _is_proven(self, row, channel_id, stream_id):
        if row.get("proof_at"):
            return True
        # After a content failure, the next stream has to look right, not just deliver bytes.
        if str(row["cause"]).startswith(engine.CONTENT_PREFIX):
            return self.store.clean_sample_since(channel_id, stream_id, row["at"])
        return False

    def _account_healthy(self, key, failed_stream, cfg, now):
        """Has some other stream on this account kept playing since the failure?"""
        if not key:
            return False
        for session in self.store.sessions_for_account(key, now - cfg.healthy_seconds):
            if session["stream_id"] != failed_stream and (
                self.dp.current_stream(session["channel_uuid"]) == session["stream_id"]
            ):
                return True
        return False

    def _judge(self, row, cfg, now):
        channel = self.dp.channel_by_id(row["channel_id"])
        if not channel:
            self.store.settle_failure(row["id"], "gone", "void")
            return
        if now - row["at"] > EXPIRE_SECONDS:
            self.store.settle_failure(row["id"], "expired", "void")
            return
        live = self.dp.live_state(channel["uuid"])
        if live is None:
            # Redis can't be read, so nothing is known. Look again soon.
            self.store.wait_longer(row["id"], now + RECHECK_SECONDS)
            return
        self._note_proof(row["channel_id"], channel["uuid"], cfg, now)
        row = dict(row, proof_at=row.get("proof_at") or self.store.failure_proof(row["id"]))
        is_content = str(row["cause"]).startswith(engine.CONTENT_PREFIX)
        if not is_content and live["running"] and live["stream_id"] == row["stream_id"]:
            # It came back and it's playing, so this failure is out of date.
            session = self.store.get_session(channel["uuid"])
            if session and session["since"] >= row["at"] - 1 and self._flowing(live, session, now):
                self.store.settle_failure(row["id"], "recovered", "void")
                logger.info("Stream %s on channel %s failed but is now playing, so it stays", row["stream_id"], channel["number"])
                return
            if now - row["at"] < cfg.proof_wait_seconds:
                self.store.wait_longer(row["id"], now + RECHECK_SECONDS)
                return
        if not is_content:
            # The failure may turn out to have a better explanation now more is known.
            guard = self._guarded(row["account_key"], row["channel_id"], row.get("attempt_at") or row["at"], row["at"], cfg)
            if guard and not self._let_off_too_often(row["channel_id"], row["stream_id"], cfg, now):
                self.store.settle_failure(row["id"], guard, "guarded")
                return
        rows = self.dp.read_order(row["channel_id"])
        facts = self._facts(row, rows, channel, cfg, now)
        if facts.healthy and row["account_key"]:
            self.store.clear_outage(row["account_key"])
        verdict = engine.judge(facts, cfg)
        if verdict == "demote":
            try:
                result = self._demote(channel, row["stream_id"], row["account_key"], row["cause"], "event", cfg, now)
            except OrderBusy:
                self.store.wait_longer(row["id"], now + RECHECK_SECONDS)
                return
            self.store.settle_failure(row["id"], result)
            return
        logger.info("Stream %s on channel %s not demoted: %s", row["stream_id"], channel["number"], verdict)
        if verdict == "no_proof":
            if live["running"] and now - row["at"] < cfg.proof_wait_seconds:
                self.store.wait_longer(row["id"], now + RECHECK_SECONDS)
            else:
                # The strike stays. If it fails again with proof, it goes.
                self.store.settle_failure(row["id"], verdict)
        elif verdict == "outage":
            self._declare_outage(row["account_key"], cfg, now)
            self.store.settle_failure(row["id"], "outage", "void")
        elif verdict in ("paused", "channel_problem", "gone", "excluded", "only_stream"):
            self.store.settle_failure(row["id"], verdict, "void")
            if verdict == "channel_problem":
                self.store.void_strikes(row["channel_id"], None, "channel_problem")
                self.store.add_journal(now, "channel_problem", row["channel_id"], cause=row["cause"], actor="event")
        elif verdict == "already_last":
            self.store.settle_failure(row["id"], verdict, "spent")
        else:
            # Below the score, suspected outage, or a limit: the strike still counts later.
            self.store.settle_failure(row["id"], verdict)
            if verdict == "global_stop":
                self._warn_global_stop(cfg, now)

    def _warn_global_stop(self, cfg, now):
        if self.store.once_every("global_stop_notice", now, 3600):
            self.store.add_journal(now, "global_stop", actor="event")
            day = time.strftime("%Y%m%d", time.gmtime(now))
            self.warn(f"global-stop-{day}", "Stream Demote stopped demoting",
                      f"Over {int(cfg.global_hourly_limit)} demotions in an hour, so it has stopped for now. Check the journal.")

    def _declare_outage(self, key, cfg, now):
        giveup = cfg.outage_giveup_hours * 3600
        self.store.record_outage(key, now, now + cfg.outage_pause_minutes * 60, giveup or float("inf"))
        self.store.void_account_strikes(key, now - cfg.score_window_hours * 3600, "outage")
        since = now - cfg.rollback_minutes * 60
        if cfg.dry_run:
            # Dry run moved nothing and moves nothing now, so this is only written down.
            waiting = self.store.undoable(account_key=key, since=since, skip_flags=True)
            self.store.add_journal(now, "outage", account_key=key, actor="event", cause=f"would_roll_back={len(waiting)}")
            logger.warning("Provider outage seen on %s: demotions paused. Dry run, so nothing moved", key)
            return
        # A flag stays: a person saw that stream was bad, whatever the provider did next.
        undone = self.undo(account_key=key, since=since, action="rollback", actor="outage", skip_flags=True, automatic=True)
        self.store.add_journal(now, "outage", account_key=key, actor="event", cause=f"rolled_back={undone}")
        logger.warning("Provider outage seen on %s: demotions paused, %s recent ones rolled back", key, undone)

    # Moving streams

    def _demote(self, channel, stream_id, key, cause, actor, cfg, now):
        """Returns "demoted", "observed" or "no_change"."""
        channel_id = channel["id"]
        if cfg.dry_run:
            rows = self.dp.read_order(channel_id)
            before = [item[0] for item in rows]
            after = [item[0] for item in engine.demote_order(rows, stream_id)]
            if before == after:
                self.store.spend_strikes(channel_id, stream_id)
                return "no_change"
            self.store.add_journal(now, "would_demote", channel_id, stream_id, key, cause, actor,
                                   before.index(stream_id), after.index(stream_id), before, after)
            self.store.spend_strikes(channel_id, stream_id)
            logger.info("Dry run: would demote stream %s on channel %s (%s)", stream_id, channel["number"], cause)
            return "observed"
        # If another tool has rewritten this channel, catch up first, or that gets lost here.
        self.guard.check_channel(channel_id, cfg, now, settle=False)
        before, after = self.dp.apply_order(channel_id, lambda rows: engine.demote_order(rows, stream_id))
        if before == after:
            self.store.spend_strikes(channel_id, stream_id)
            return "no_change"
        origin = before.index(stream_id)
        target = after.index(stream_id)
        self.store.add_journal(now, "demote", channel_id, stream_id, key, cause, actor, origin, target, before, after)
        self.store.mark_demoted(channel_id, stream_id, key, now, origin, cause)
        self.store.spend_strikes(channel_id, stream_id)
        self.guard.after_write(channel_id, now)
        logger.info("Demoted stream %s on channel %s to place %s (%s)", stream_id, channel["number"], target + 1, cause)
        self._share("stream_demoted", channel, stream_id, key, cause, origin, target, cfg)
        return "demoted"

    def _move_back(self, channel, stream_id, record, action, actor, now, cfg):
        position = record["from_position"]
        before, after = self.dp.apply_order(
            channel["id"], lambda rows: engine.restore_order(rows, stream_id, position)
        )
        if before == after:
            return False
        self.store.add_journal(now, action, channel["id"], stream_id, record.get("account_key"), record.get("cause"),
                               actor, before.index(stream_id), after.index(stream_id), before, after)
        self._share("stream_restored", channel, stream_id, record.get("account_key"), action,
                    before.index(stream_id), after.index(stream_id), cfg)
        return True

    def _share(self, event_type, channel, stream_id, key, cause, origin, target, cfg):
        if not cfg.shared_event:
            return
        try:
            self.dp.emit(event_type, channel["uuid"], channel["name"], stream_id=stream_id, account_key=key,
                         cause=cause, from_position=origin, to_position=target)
        except Exception:
            logger.debug("Could not write the shared event", exc_info=True)

    def _sweep_paroles(self, cfg, now):
        for record in self.store.demoted_rows("demoted"):
            due = engine.parole_at(record["state_at"], record["strikes"], cfg)
            if due is None or now < due:
                continue
            channel = self.dp.channel_by_id(record["channel_id"])
            if not channel:
                self.store.forget_demoted(record["channel_id"], record["stream_id"])
                continue
            if self.excluded(channel, cfg):
                continue
            if cfg.dry_run:
                self._dry_note("would_restore_parole", channel, record["stream_id"], record, now)
                continue
            # A hand change seen here lets the stream go, so it isn't moved over the owner's order.
            self.guard.check_channel(record["channel_id"], cfg, now, settle=False)
            record = self.store.demoted(record["channel_id"], record["stream_id"])
            if not record:
                continue
            self._move_back(channel, record["stream_id"], record, "restore_parole", "parole", now, cfg)
            self.store.mark_on_parole(record["channel_id"], record["stream_id"], now)
            self.store.close_demote_rows(record["channel_id"], record["stream_id"], now)
            self.guard.after_write(record["channel_id"], now)

    def _sweep_running_plays(self, cfg, now):
        for session in self.store.sessions_on_demoted(now - cfg.earn_back_minutes * 60):
            if self.dp.current_stream(session["channel_uuid"]) != session["stream_id"]:
                continue
            channel = self.dp.channel_by_id(session["channel_id"])
            if channel:
                self._credit_play(channel, session["stream_id"], session["since"], now, cfg)

    # Content checks

    def content_failed(self, session, cause_name, cfg, now):
        """A content check found a problem that lasted. It counts like any other failure."""
        channel = self.dp.channel_by_id(session["channel_id"])
        stream_id = session["stream_id"]
        if not channel or self.excluded(channel, cfg):
            return
        key = session["account_key"]
        cause = f"{engine.CONTENT_PREFIX}{cause_name}"
        self.store.add_journal(now, "content_failed", channel["id"], stream_id, key, cause, "content")
        logger.info("Stream %s on channel %s failed a content check: %s", stream_id, channel["number"], cause_name)
        if not cfg.content_act:
            # Tests found good streams failing these checks, so by default they only report.
            return
        moved = self.content.rotate_after_failure(channel, stream_id, cause, cfg, now)
        hold = cfg.effective_hold
        # If the viewer moved, the next stream has to look right before this one drops. If
        # they couldn't move, there's nothing to compare with, so it goes on this evidence.
        proof_at = None if moved else now
        self.store.add_failure(now, channel["uuid"], channel["id"], stream_id, key, cause, cfg.content_weight, "pending",
                               due_at=now + hold, attempt_at=session["since"], proof_at=proof_at)
        self.timer(hold + 1, self._timer_sweep)

    # Undo

    def undo(self, channel_id=None, account_key=None, since=None, limit=None, action="undo", actor="admin",
             skip_flags=False, automatic=False):
        """Reverse demotions, newest first. Returns how many streams moved back."""
        now = self.clock()
        cfg = self.settings()
        moved = 0
        seen = set()
        for row in self.store.undoable(channel_id, account_key, since, limit, skip_flags):
            pair = (row["channel_id"], row["stream_id"])
            if pair in seen:
                continue
            seen.add(pair)
            channel = self.dp.channel_by_id(row["channel_id"])
            if channel and automatic and self.excluded(channel, cfg):
                continue
            stream_id = row["stream_id"]
            if channel:
                # A hand change seen here lets the stream go, and there's nothing to undo.
                self.guard.check_channel(row["channel_id"], cfg, now, settle=False)
            record = self.store.demoted(row["channel_id"], stream_id)
            if not record:
                continue
            if channel:
                record = dict(record, from_position=row["from_position"], account_key=row["account_key"], cause=row["cause"])
                if self._move_back(channel, stream_id, record, action, actor, now, cfg):
                    moved += 1
            self.store.close_demote_rows(row["channel_id"], stream_id, now)
            self.store.forget_demoted(row["channel_id"], stream_id)
            self.store.void_strikes(row["channel_id"], stream_id, action)
            if channel:
                self.guard.after_write(row["channel_id"], now)
        return moved

    # The flag API

    def flag(self, authorization, body):
        """Handle one flag request. Returns (http status, body dict, extra headers).

        Replies name no stream, provider or URL, so a stolen token learns nothing.
        """
        self.ready()
        now = self.clock()
        scheme, _, presented = (authorization or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented.startswith(TOKEN_PREFIX):
            return 401, {"result": "unauthorized"}, {"WWW-Authenticate": "Bearer"}
        token = self.store.find_token(hash_token(presented))
        if not token:
            return 401, {"result": "unauthorized"}, {"WWW-Authenticate": "Bearer"}
        self.store.touch_token(token["id"], now)
        cfg = self.settings()

        hits, wait = self.store.hit(f"token:{token['id']}", now, 3600)
        if hits > cfg.flag_token_hourly:
            return 429, {"result": "rate_limited"}, {"Retry-After": str(int(wait) + 1)}

        channel, problem = self._resolve_channel(body)
        if problem:
            return (400 if problem == "bad_request" else 409), {"result": problem}, {}

        hits, wait = self.store.hit(f"channel:{channel['id']}", now, cfg.flag_channel_seconds)
        if hits > 1:
            return 429, {"result": "rate_limited"}, {"Retry-After": str(int(wait) + 1)}

        self.sweep(cfg)
        result = self._apply_flag(channel, token["label"], cfg, now)
        return (409 if result == "not_playing" else 200), {"result": result}, {}

    def _watched(self, channel):
        """What a channel's playing, if a viewer is there. Anything unreadable counts as nobody."""
        live = self.dp.live_state(channel["uuid"])
        if live and live["running"] and live["clients"] > 0:
            return live
        return None

    def _resolve_channel(self, body):
        """The caller only names a channel. The server works out the stream."""
        if not isinstance(body, dict):
            return None, "bad_request"
        wanted = body.get("channel_uuid")
        if wanted:
            try:
                uuid_module.UUID(str(wanted))
            except ValueError:
                return None, "bad_request"
            channel = self.dp.channel(wanted)
            if channel and self._watched(channel):
                return channel, None
            return None, "not_playing"
        number = str(body.get("channel_number") or "").strip()
        if not NUMBER_PATTERN.match(number):
            return None, "bad_request"
        running = [item for item in self.dp.channels_by_number(number) if self._watched(item)]
        if not running:
            return None, "not_playing"
        if len(running) > 1:
            return None, "ambiguous"
        return running[0], None

    def _apply_flag(self, channel, label, cfg, now):
        # Checked last thing: a stream left running for a shutdown delay has nobody to help.
        live = self._watched(channel)
        if not live:
            return "not_playing"
        stream_id = live["stream_id"]
        info = self.dp.stream(stream_id)
        actor = f"flag:{label}"
        if not info or info["is_custom"] or self.excluded(channel, cfg):
            self.store.add_journal(now, "flag_ignored", channel["id"], stream_id, actor=actor, cause=engine.FLAG_CAUSE)
            return "noted"
        rows = self.dp.read_order(channel["id"])
        eligible = engine.eligible_ids(rows)
        if len(eligible) < 2:
            self.store.add_journal(now, "flag_ignored", channel["id"], stream_id, info["account_key"], engine.FLAG_CAUSE, actor)
            return "noted"
        flagged = self.store.flagged_streams(channel["id"], now - 3600) | {stream_id}
        if set(eligible) <= flagged:
            # Every stream flagged in an hour is more likely the programme than the streams.
            self.store.add_journal(now, "channel_problem", channel["id"], stream_id, info["account_key"], engine.FLAG_CAUSE, actor)
            return "channel_problem"
        if self.store.demote_count(now - 3600) >= cfg.global_hourly_limit:
            self._warn_global_stop(cfg, now)
            return "paused"
        if self.store.demote_count(now - 86400, channel["id"]) >= cfg.channel_daily_limit:
            return "paused"
        stalled = live["last_data"] is None or now - live["last_data"] > STALLED_SECONDS
        try:
            self._demote(channel, stream_id, info["account_key"], engine.FLAG_CAUSE, actor, cfg, now)
        except OrderBusy:
            # Something else is rewriting this channel right now, so nothing was changed.
            return "paused"
        if cfg.dry_run:
            return "observed"
        if not cfg.flag_rotate:
            return "demoted"
        return self._rotate_after_flag(channel, stream_id, info["account_key"], eligible, stalled, actor, now)

    def _rotate_after_flag(self, channel, stream_id, key, order_before, stalled, actor, now):
        skip = self.store.failed_streams_on_channel(channel["id"], now - RECENT_FAILURE_SECONDS)
        skip |= {record["stream_id"] for record in self.store.demoted_on_channel(channel["id"])}
        targets = engine.rotate_targets(order_before, stream_id, skip)
        # If it has stalled, the old connection can hold its seat for up to 30 seconds more, so
        # a stream on another account has the best chance. A flag is often about a picture
        # that has only just frozen, so a refusal right after one is nobody's fault either way.
        if stalled:
            targets.sort(key=lambda item: (self.dp.stream(item) or {}).get("account_key") == key)
        if key:
            self.store.set_stall(key, now)
        try:
            # The viewer may have gone while this was being worked out.
            moved_to = self.dp.rotate(channel["uuid"], targets) if targets and self._watched(channel) else None
        except Exception as error:
            # The demotion stands, so tell the caller that, not that everything failed.
            self.note_problem("rotate", error)
            self.store.add_journal(now, "rotate_failed", channel["id"], stream_id, cause=engine.FLAG_CAUSE, actor=actor)
            return "demoted"
        if moved_to is None:
            return "demoted"
        self.store.add_journal(now, "rotate", channel["id"], moved_to, cause=engine.FLAG_CAUSE, actor=actor)
        return "rotated"

    # Admin actions

    def create_token(self, label):
        self.ready()
        label = (label or "").strip()[:60]
        if not label:
            return None, "Type a label in the Token label setting first, such as living-room-tv."
        if self.store.label_in_use(label):
            return None, f"A live token already uses the label {label}. Revoke it or pick another."
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.store.add_token(hash_token(token), label, self.clock())
        self.store.add_journal(self.clock(), "token_created", actor=f"admin:{label}")
        return token, None

    def revoke_token(self, label):
        self.ready()
        label = (label or "").strip()
        count = self.store.revoke_token(label, self.clock()) if label else 0
        if count:
            self.store.add_journal(self.clock(), "token_revoked", actor=f"admin:{label}")
        return count

    def teach_channel(self, number, cfg):
        """The Teach a placeholder button."""
        self.ready()
        now = self.clock()
        try:
            channels = self.dp.channels_by_number(float(str(number).strip()))
        except ValueError:
            return {"status": "error", "message": "Type a channel number in the Teach from channel setting first."}
        for channel in channels:
            live = self._watched(channel)
            if live:
                result = self.content.teach(channel, live["stream_id"], cfg, now)
                messages = {
                    "taught": "Remembered what that channel is showing as a placeholder.",
                    "not_still": "That channel's picture is moving or blank, so there's nothing to remember.",
                    "no_ffmpeg": "ffmpeg wasn't found in the container.",
                }
                return {"status": "ok", "result": result,
                        "message": messages.get(result, "Couldn't read a sample. Nothing changed. Try again in a moment.")}
        return {"status": "ok", "result": "not_playing", "message": "Nobody is watching that channel, so there's nothing to look at."}

    def status(self, route_live):
        self.ready()
        now = self.clock()
        cfg = self.settings()
        self.sweep(cfg)
        day = now - 86400
        counts = self.store.journal_counts(day)
        outcomes = self.store.count_recent(day)
        demoted = self.store.demoted_rows()
        tokens = [row for row in self.store.list_tokens() if not row["revoked_at"]]
        paused = self.store.paused_accounts(now)
        errors = sum(self.store.hits_now(f"errors:{kind}", now, 3600) for kind in ("events", "timers", "rotate", "flag"))
        tick_at = self.store.started_at("tick")
        loop_ok = tick_at is not None and now - tick_at < 120
        samples = self.store.sample_totals(day)
        pictures = self.store.picture_counts()
        ffmpeg_found = bool(self.content.ready())
        summary = {
            "dry_run": cfg.dry_run,
            "route_live_on_this_worker": bool(route_live),
            "hand_edits_visible": bool(self.dp.can_see_hand_edits()),
            "background_loop_ok": loop_ok,
            "errors_last_hour": errors,
            "last_24h": {"journal": counts, "failure_outcomes": outcomes},
            "demoted_now": sum(1 for row in demoted if row["state"] == "demoted"),
            "on_parole": sum(1 for row in demoted if row["state"] == "parole"),
            "live_tokens": len(tokens),
            "paused_accounts": len(paused),
            "content": {
                "enabled": cfg.content_enabled,
                "acting": cfg.content_act,
                "ffmpeg_found": ffmpeg_found,
                "samples_24h": int(samples.get("n") or 0),
                "average_cpu_seconds": round(float(samples["cpu"]), 3) if samples.get("cpu") is not None else None,
                "radio_skipped": int(samples.get("radio") or 0),
                "pictures_kept": pictures["kept"],
                "pictures_taught": pictures["taught"],
            },
        }
        if errors:
            head = f"Something is wrong: {errors} failures in the last hour. Check Dispatcharr's log."
        elif cfg.dry_run:
            head = "Dry run is ON, nothing is being moved."
        else:
            head = "Live: failed streams are being moved."
        parts = [
            head,
            "Flag route is live." if route_live else "Flag route is NOT live on this worker. Restart Dispatcharr.",
            f"Last 24 h: {counts.get('demote', 0)} demoted, {counts.get('would_demote', 0)} would demote,"
            f" {outcomes.get('outage', 0) + outcomes.get('paused', 0)} held back as provider outage,"
            f" {counts.get('undo', 0) + counts.get('rollback', 0)} undone,"
            f" {counts.get('reapply', 0)} put back after another tool rewrote the order.",
            f"{summary['demoted_now']} streams demoted now, {summary['on_parole']} on parole,"
            f" {summary['live_tokens']} live tokens.",
        ]
        if not loop_ok:
            parts.append("The background check hasn't run lately. Restart Dispatcharr if this stays.")
        if not summary["hand_edits_visible"]:
            parts.append("Hand edits can't be seen directly on this worker, so an edit in place counts as by hand.")
        if cfg.content_enabled:
            parts.append(
                f"Content checks on ({'acting' if cfg.content_act else 'report only'}): "
                f"{summary['content']['samples_24h']} samples in 24 h."
                if ffmpeg_found else "Content checks are on but ffmpeg wasn't found, so nothing is checked."
            )
        return {"status": "ok", "message": " ".join(parts), **summary}

    def write_journal_csv(self, limit=1000):
        self.ready()
        rows = self.store.journal_recent(limit)
        columns = ["at", "action", "channel_id", "stream_id", "account_key", "cause", "actor",
                   "from_position", "to_position", "before_order", "after_order", "undone_at"]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["time_utc"] + columns[1:])
        for row in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["at"]))
            writer.writerow([stamp] + [_safe_cell(row.get(name)) for name in columns[1:]])
        os.makedirs(self.report_dir, exist_ok=True)
        path = os.path.join(self.report_dir, "journal.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(buffer.getvalue())
        return path, rows
