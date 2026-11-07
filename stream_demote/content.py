"""Optional content checks, on streams a viewer already has open.

Nothing here opens a stream. It copies a few seconds of what Dispatcharr has already put in
its channel buffer and runs ffmpeg on that copy. Anything it can't read or decode gives no
answer, and no answer does nothing. One bad sample never counts: a problem has to last.
"""

from __future__ import annotations

import logging

from . import engine, sampler

logger = logging.getLogger(__name__)

SLOT_SECONDS = 60
LOOP_WINDOW_SECONDS = 900
LOOP_MIN_GAP_SECONDS = 20
RECORD_STILL_SAMPLES = 3
RECORD_STILL_AFTER_SECONDS = 60
LOAD_LIMIT_PER_CORE = 3
RECENT_FAILURE_SECONDS = 3600
ROTATE_PER_CHANNEL_HOURLY = 3


def _too_busy():
    import os

    try:
        return os.getloadavg()[0] > LOAD_LIMIT_PER_CORE * (os.cpu_count() or 1)
    except OSError:
        return False


class ContentChecker:
    def __init__(self, service, checker=sampler.check_bytes, ready=sampler.available, busy=_too_busy):
        self.service = service
        self.checker = checker
        self.ready = ready
        self.busy = busy

    @property
    def store(self):
        return self.service.store

    @property
    def dp(self):
        return self.service.dp

    def tick(self, cfg, now):
        """Take the samples that are due, within the limit on how many run at once."""
        if not cfg.content_enabled or not self.ready() or self.busy():
            return
        limit = max(1, int(cfg.content_max_at_once))
        due = []
        for session in self.store.all_sessions():
            last = self.store.last_sample_at(session["channel_id"], session["stream_id"], session["since"])
            if last is not None and now - last < cfg.content_interval_seconds:
                continue
            live = self._sample_worthy(session, cfg, now)
            if live:
                due.append((last or 0.0, session, live))
        # Longest wait first, so a busy evening shares the checks out instead of starving one channel.
        due.sort(key=lambda item: item[0])
        for _last, session, live in due[:limit]:
            self._take_sample(session, live, cfg, now, limit)

    def _sample_worthy(self, session, cfg, now):
        """Returns what's live on this session if it should be sampled now, else None."""
        if now - session["since"] < cfg.content_first_seconds:
            return None
        channel_id, stream_id = session["channel_id"], session["stream_id"]
        if self.store.content_failure_since(channel_id, stream_id, session["since"]):
            return None
        live = self.dp.live_state(session["channel_uuid"])
        if not live or not live["running"] or live["stream_id"] != stream_id:
            return None
        # Only ever on a stream someone is watching, and only while it's actually flowing.
        if live["clients"] <= 0 or live["state"] != "active":
            return None
        last_data = live["last_data"]
        if last_data is None or now - last_data > 10:
            return None
        channel = self.dp.channel_by_id(channel_id)
        if not channel or self.service.excluded(channel, cfg):
            return None
        info = self.dp.stream(stream_id)
        if not info or info["is_custom"]:
            return None
        if len(engine.eligible_ids(self.dp.read_order(channel_id))) < 2:
            return None
        # FFmpeg-style profiles already record the codecs, so a radio channel costs nothing.
        if live.get("audio_codec") and not live.get("video_codec"):
            return None
        samples = self.store.session_samples(channel_id, stream_id, session["since"])
        if samples and samples[0]["radio"]:
            return None
        return live

    def _take_sample(self, session, live, cfg, now, limit):
        slot = None
        for number in range(limit):
            key = f"content_slot:{number}"
            if self.store.claim(key, now, SLOT_SECONDS):
                slot = key
                break
        if slot is None:
            return
        channel_id, stream_id = session["channel_id"], session["stream_id"]
        try:
            if not self.store.claim(f"content_take:{channel_id}", now, cfg.content_interval_seconds * 0.5):
                return
            data = self.dp.read_sample(session["channel_uuid"], stream_id, cfg.content_sample_seconds, session["since"])
            if not data:
                return
            result = self.checker(data, cfg.content_sample_seconds, timeout=cfg.content_timeout_seconds)
            del data
            if not result:
                return
            self._judge_sample(session, live, result, cfg, self.service.clock())
        finally:
            self.store.release(slot)

    def _judge_sample(self, session, live, result, cfg, now):
        channel_id, stream_id = session["channel_id"], session["stream_id"]
        since = session["since"]
        if not result.get("has_video"):
            # Radio has no picture to judge and may well be quiet. Skipped, as IPTV Checker does.
            self.store.add_sample(now, channel_id, stream_id, since, [], True, False, True, None, [], result.get("cpu"))
            return
        earlier = self.store.session_samples(channel_id, stream_id, since)
        known = self.store.known_pictures() if cfg.content_placeholder else ()
        problems, still = engine.assess_sample(result, cfg, known, live.get("container"))
        hashes = result.get("hashes") or []
        looped = cfg.content_looping and self._came_round_again(hashes, earlier, now)
        still_text = f"{still:016x}" if still is not None else None
        if still is not None and cfg.content_placeholder:
            self._note_still(still, earlier, channel_id, now)
        stored = [f"{item:016x}" if item is not None else None for item in hashes]
        self.store.add_sample(now, channel_id, stream_id, since, problems, result.get("audible"), looped, False,
                              still_text, stored, result.get("cpu"))
        samples = self.store.session_samples(channel_id, stream_id, since)
        verdict = engine.content_verdict(samples, cfg, now) or engine.loop_verdict(samples, cfg)
        if verdict:
            self.service.content_failed(session, verdict, cfg, now)

    def _came_round_again(self, hashes, earlier, now):
        """Is this run of pictures one this stream showed a little while ago?"""
        for sample in earlier:
            if now - sample["at"] > LOOP_WINDOW_SECONDS:
                break
            if now - sample["at"] < LOOP_MIN_GAP_SECONDS:
                continue
            if engine.repeats(hashes, [int(item, 16) if item else None for item in sample["hashes"]]):
                return True
        return False

    def _note_still(self, still, earlier, channel_id, now):
        """A picture that held on this channel, sample after sample, counts as one sighting of it.

        A programme cuts to another shot sooner or later, so it rarely holds this long, and
        the same programme carried on two channels won't often be caught doing it.
        """
        run = []
        for sample in earlier:
            if not sample["still"] or engine.hamming(int(sample["still"], 16), still) > engine.STILL_BITS:
                break
            run.append(sample)
        if len(run) >= RECORD_STILL_SAMPLES - 1 and now - run[-1]["at"] >= RECORD_STILL_AFTER_SECONDS:
            self.store.add_picture(still, channel_id, now)

    def teach(self, channel, stream_id, cfg, now):
        """Copy what a channel is showing now, and if it's a still picture, remember it as a placeholder."""
        data = self.dp.read_sample(channel["uuid"], stream_id, cfg.content_sample_seconds)
        if not data:
            return "no_sample"
        return self.teach_from(data, channel["id"], cfg, now)

    def teach_from(self, data, channel_id, cfg, now):
        if not self.ready():
            return "no_ffmpeg"
        result = self.checker(data, cfg.content_sample_seconds, timeout=cfg.content_timeout_seconds)
        if not result:
            return "no_answer"
        still = engine.still_hash(result.get("hashes") or [])
        if still is None:
            return "not_still"
        self.store.add_picture(still, channel_id, now, taught=True)
        self.store.add_journal(now, "placeholder_taught", channel_id, actor="teach")
        return "taught"

    def rotate_after_failure(self, channel, stream_id, cause, cfg, now):
        """Move the viewer on, if they're still there and the limits allow. True if it moved."""
        actor = "content"
        if not cfg.content_rotate:
            return False
        live = self.dp.live_state(channel["uuid"])
        if not live or not live["running"] or live["clients"] <= 0 or live["stream_id"] != stream_id:
            return False
        hits, _ = self.store.hit("content_rotate", now, 3600)
        channel_hits, _ = self.store.hit(f"content_rotate:{channel['id']}", now, 3600)
        if hits > cfg.content_rotate_hourly or channel_hits > ROTATE_PER_CHANNEL_HOURLY:
            self.store.add_journal(now, "rotate_limited", channel["id"], stream_id, cause=cause, actor=actor)
            return False
        rows = self.dp.read_order(channel["id"])
        skip = self.store.failed_streams_on_channel(channel["id"], now - RECENT_FAILURE_SECONDS)
        skip |= {record["stream_id"] for record in self.store.demoted_on_channel(channel["id"])}
        targets = engine.rotate_targets(engine.eligible_ids(rows), stream_id, skip)
        if not targets:
            self.store.add_journal(now, "rotate_none", channel["id"], stream_id, cause=cause, actor=actor)
            return False
        if cfg.dry_run:
            self.store.add_journal(now, "would_rotate", channel["id"], targets[0], cause=cause, actor=actor)
            return False
        try:
            moved = self.dp.rotate(channel["uuid"], targets)
        except Exception:
            logger.warning("Could not switch a channel to its next stream after a content check")
            self.store.add_journal(now, "rotate_failed", channel["id"], stream_id, cause=cause, actor=actor)
            return False
        if moved is None:
            self.store.add_journal(now, "rotate_failed", channel["id"], stream_id, cause=cause, actor=actor)
            return False
        self.store.add_journal(now, "rotate", channel["id"], moved, cause=cause, actor=actor)
        return True
