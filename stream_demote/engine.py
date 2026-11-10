"""The rules. No Django, no database and no network, so each rule can be tested alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Reasons Dispatcharr gives when it moves off a stream because the stream failed.
# "manual" and "unknown" are left out on purpose: they say nothing about the stream.
FAILURE_REASONS = ("max_retries_exceeded", "health_monitor", "buffering_timeout")

FLAG_CAUSE = "flag"
CONTENT_PREFIX = "content_"

DEFAULTS = {
    "dry_run": True,
    "demote_score": 1.0,
    "score_window_hours": 24,
    "weight_hard": 1.0,
    "weight_stall_early": 1.0,
    "weight_stall_late": 0.5,
    "weight_slow": 0.5,
    "early_minutes": 5,
    "hold_seconds": 45,
    "proof_seconds": 10,
    "proof_wait_seconds": 300,
    "healthy_seconds": 30,
    "outage_streams": 3,
    "outage_window_seconds": 120,
    "outage_pause_minutes": 30,
    "outage_giveup_hours": 24,
    "rollback_minutes": 10,
    "chain_seconds": 30,
    "stall_guard_seconds": 35,
    "zap_guard_seconds": 10,
    "guard_repeat_limit": 3,
    "channel_problem_minutes": 60,
    "channel_burst_limit": 3,
    "channel_burst_minutes": 5,
    "channel_daily_limit": 6,
    "global_hourly_limit": 20,
    "earn_back_minutes": 10,
    "parole_hours": 72,
    "excluded_channels": "",
    "content_skip_channels": "",
    "order_guard": True,
    "order_guard_seconds": 60,
    "order_guard_settle_seconds": 10,
    "order_guard_hourly_limit": 6,
    "order_guard_gone_minutes": 10,
    "content_enabled": False,
    "content_black": True,
    "content_frozen": True,
    "content_silence": True,
    "content_no_audio": True,
    "content_placeholder": True,
    "content_looping": True,
    "content_still_with_sound": True,
    "content_act": False,
    "content_rotate": True,
    "content_sample_seconds": 6,
    "content_first_seconds": 20,
    "content_interval_seconds": 45,
    "content_bad_samples": 3,
    "content_bad_span_seconds": 120,
    "content_max_at_once": 1,
    "content_timeout_seconds": 20,
    "content_rotate_hourly": 6,
    "content_shared_channels": 3,
    "content_weight": 1.0,
    "teach_channel": "",
    "flag_rotate": True,
    "flag_channel_seconds": 60,
    "flag_token_hourly": 6,
    "shared_event": True,
    "journal_days": 90,
    "token_label": "",
    "undo_channel": "",
}

PAROLE_CEILING_HOURS = 30 * 24

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


class Settings:
    """Settings with defaults filled in and bad values replaced by the default."""

    def __init__(self, raw=None):
        raw = raw if isinstance(raw, dict) else {}
        for name, default in DEFAULTS.items():
            setattr(self, name, _coerce(raw.get(name, default), default))

    @property
    def effective_hold(self):
        # A session must outlive a failure by this long before it counts as proof
        # that the provider is up, so the hold can't be shorter.
        return max(self.hold_seconds, self.healthy_seconds)

    def excluded_numbers(self):
        return parse_channel_list(self.excluded_channels)[0]

    def content_needed(self):
        """How many bad samples, over how long, before a content failure counts. Never just one."""
        return max(2, int(self.content_bad_samples)), max(30.0, float(self.content_bad_span_seconds))


_RANGE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$")


def parse_channel_list(text):
    """A list such as "101, 9000-9999, Basketball": (numbers, ranges, group names).

    Anything that isn't a number or a range of numbers is a channel group's name, matched
    without regard to case.
    """
    numbers, ranges, groups = set(), [], set()
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        found = _RANGE.match(part)
        if found:
            low, high = sorted((float(found.group(1)), float(found.group(2))))
            ranges.append((low, high))
            continue
        try:
            numbers.add(float(part))
        except ValueError:
            groups.add(part.casefold())
    return numbers, ranges, groups


def in_channel_list(channel, text, group_name=None):
    """Is this channel in the list? group_name is called only when the list names a group."""
    numbers, ranges, groups = parse_channel_list(text)
    number = channel.get("number")
    if number is not None and (number in numbers or any(low <= number <= high for low, high in ranges)):
        return True
    if groups and group_name is not None:
        name = group_name()
        return bool(name) and str(name).strip().casefold() in groups
    return False


def _coerce(value, default):
    if isinstance(default, bool):
        # A value that can't be read keeps the default, so a bad value can't turn dry run off.
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        return default
    if isinstance(default, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number or number < 0 or number == float("inf"):
            return default
        return number
    return "" if value is None else str(value)


def failure_weight(reason, seconds_on_stream, cfg):
    """How much one failure counts towards a demotion.

    After a long good play a failure counts for less, whatever the reason: one blip in six
    hours isn't the same as a stream that won't start.
    """
    late = seconds_on_stream is not None and seconds_on_stream >= cfg.early_minutes * 60
    if reason == "max_retries_exceeded":
        return cfg.weight_stall_late if late else cfg.weight_hard
    if reason == "buffering_timeout":
        return cfg.weight_slow
    if reason == "health_monitor":
        # With no record of when the stream started, assume the milder case.
        if seconds_on_stream is not None and not late:
            return cfg.weight_stall_early
        return cfg.weight_stall_late
    return 0.0


def stall_excuses(attempt_at, failed_at, stall_at, cfg):
    """A stalled connection can stay open for up to 30 s after a failover, so the provider
    can see one connection too many and refuse the next stream.

    What matters is when the attempt began, not when it was given up on: a slow refusal
    can take 20 s to be reported. Strictly before the failure, because a stall failure also
    sets this time and can't excuse itself.
    """
    return bool(stall_at) and stall_at < failed_at and attempt_at - stall_at < cfg.stall_guard_seconds


def zap_window(attempt_at, failed_at, cfg):
    """A channel stopping in this window can explain a refusal on another channel.

    The provider may not have seen the old connection close, even if Dispatcharr has no limit
    set, because the provider may enforce one of its own. It can also stop just after.
    """
    return attempt_at - cfg.zap_guard_seconds, failed_at + cfg.zap_guard_seconds


@dataclass
class Facts:
    """What is known when a held failure is judged."""

    stream_present: bool = True
    excluded: bool = False
    eligible: int = 0
    already_last: bool = False
    all_failed: bool = False
    paused: bool = False
    long_outage: bool = False
    healthy: bool = False
    outage_streams: int = 0
    outage_channels: int = 0
    chained: bool = False
    repeated: bool = False
    proven: bool = True
    score: float = 0.0
    channel_burst: int = 0
    channel_day: int = 0
    global_hour: int = 0


def judge(facts, cfg):
    """Decide what to do with a held failure. Returns "demote" or the reason not to."""
    if not facts.stream_present:
        return "gone"
    if facts.excluded:
        return "excluded"
    if facts.eligible < 2:
        return "only_stream"
    if facts.already_last:
        return "already_last"
    if not facts.healthy and not facts.long_outage:
        if facts.paused:
            return "paused"
        if facts.outage_streams >= cfg.outage_streams and facts.outage_channels >= 2:
            return "outage"
    if facts.all_failed:
        return "channel_problem"
    if not facts.healthy and not facts.long_outage and facts.chained and not facts.repeated:
        return "suspect_outage"
    if facts.score < cfg.demote_score:
        return "below_score"
    # A stream only drops if another on the channel has then shown it can play. Otherwise
    # every stream may be bad, and moving them would only shuffle them.
    if not facts.proven:
        return "no_proof"
    if facts.global_hour >= cfg.global_hourly_limit:
        return "global_stop"
    if facts.channel_burst >= cfg.channel_burst_limit or facts.channel_day >= cfg.channel_daily_limit:
        return "channel_limit"
    return "demote"


def _last_real_index(rows):
    last = -1
    for index, (_stream_id, is_custom) in enumerate(rows):
        if not is_custom:
            last = index
    return last


def demote_order(rows, stream_id):
    """Move a stream to just after the last non-custom stream.

    rows is [(stream_id, is_custom)] in the current order. Custom streams stay where they
    are, so a fallback card kept at the end stays at the end.
    """
    rows = list(rows)
    target = next((row for row in rows if row[0] == stream_id), None)
    if target is None or target[1]:
        return rows
    rows.remove(target)
    rows.insert(_last_real_index(rows) + 1, target)
    return rows


def restore_order(rows, stream_id, position):
    """Put a stream back at an earlier position, but never after a trailing custom stream."""
    rows = list(rows)
    target = next((row for row in rows if row[0] == stream_id), None)
    if target is None or target[1]:
        return rows
    rows.remove(target)
    rows.insert(min(int(position), _last_real_index(rows) + 1), target)
    return rows


def reapply_order(rows, demoted_ids):
    """Put demoted streams back at the bottom after something else rewrote the order.

    The other streams keep the order the other tool gave them. demoted_ids is oldest
    demotion first, so the streams end up in the order they'd have by being demoted again.
    """
    for stream_id in demoted_ids:
        rows = demote_order(rows, stream_id)
    return list(rows)


def still_under_the_rest(ids, custom_ids, demoted_ids, stream_id):
    """Is this demoted stream still below every stream that isn't demoted?"""
    position = ids.index(stream_id)
    return not any(
        item not in demoted_ids and item not in custom_ids and index > position
        for index, item in enumerate(ids)
    )


def rotate_targets(order, current, skip):
    """Streams to try next: the walk on from just after the current one, wrapping round.

    order is the eligible streams before anything moved. The stream that just failed, or
    one that's already demoted, is no improvement, so skip holds those.
    """
    if current in order:
        start = order.index(current) + 1
        walk = order[start:] + order[:start]
    else:
        walk = list(order)
    return [item for item in walk if item != current and item not in skip]


def eligible_ids(rows):
    return [stream_id for stream_id, is_custom in rows if not is_custom]


def is_last_eligible(rows, stream_id):
    eligible = eligible_ids(rows)
    return bool(eligible) and eligible[-1] == stream_id


def parole_at(demoted_at, strikes, cfg):
    """When a demoted stream gets another try, or None if parole is off.

    Each demotion doubles the wait, so a stream that keeps failing rarely costs a viewer
    another slow start.
    """
    if cfg.parole_hours <= 0:
        return None
    hours = cfg.parole_hours * (2 ** max(int(strikes) - 1, 0))
    return demoted_at + min(hours, PAROLE_CEILING_HOURS) * 3600


# Content checks. A sample is what one short ffmpeg pass found, as worked out in sampler.py.

STILL_BITS = 4
HASH_MIN_BITS = 8
LOOP_BITS = 6
DISTINCT_BITS = 10
LOOP_MIN_FRAMES = 4
LOOP_MIN_DISTINCT = 3
# A loop has to keep repeating for longer than an advert break lasts.
LOOP_RUN_MIN_HITS = 6
LOOP_RUN_MIN_SECONDS = 300
LOOP_RUN_GAP = 1
FILE_CONTAINERS = ("mov", "matroska", "avi")
# In the order they're reported. A black screen is also a still picture, so black comes first.
PROBLEM_ORDER = ("black", "file", "placeholder", "frozen", "looping", "silence", "no_audio")
NEEDS_LONGER_WITH_SOUND = ("black", "frozen")


def hamming(left, right):
    return bin(left ^ right).count("1")


def still_hash(hashes):
    """The one picture a sample kept showing, or None if it moved or had nothing to go on.

    hashes holds None for a flat frame, such as plain black, which says nothing about what
    the picture is.
    """
    known = [item for item in hashes if item is not None]
    if len(known) < 3 or len(known) < 0.8 * len(hashes):
        return None
    best, best_count = None, 0
    for candidate in sorted(set(known)):
        count = sum(1 for item in known if hamming(candidate, item) <= STILL_BITS)
        if count > best_count:
            best, best_count = candidate, count
    if best is None or best_count < 0.8 * len(known):
        return None
    # A nearly blank frame with a small logo has very few bits set, so any two of them look alike.
    return best if HASH_MIN_BITS <= bin(best).count("1") <= 64 - HASH_MIN_BITS else None


def distinct_pictures(hashes):
    """How many visibly different pictures are in a list of hashes."""
    groups = []
    for item in hashes:
        if item is not None and not any(hamming(item, other) <= DISTINCT_BITS for other in groups):
            groups.append(item)
    return len(groups)


def repeats(new, old):
    """Does the moving run of pictures in new also appear, in the same order, in old?

    A talking head or a slide barely changes, so it can't count: the frames that match have
    to show several different pictures. A real programme doesn't repeat itself that way
    within minutes, and a looping clip does.
    """
    best = 0
    for shift in range(-len(new) + 1, len(old)):
        matched = []
        for index, item in enumerate(new):
            other = index + shift
            if item is None or not 0 <= other < len(old) or old[other] is None:
                continue
            if hamming(item, old[other]) <= LOOP_BITS:
                matched.append(item)
        if len(matched) >= LOOP_MIN_FRAMES and distinct_pictures(matched) >= LOOP_MIN_DISTINCT:
            best = max(best, len(matched))
    return best > 0


def is_known_placeholder(picture, known, cfg):
    """known is [(hash, channel_id, taught)]. Near matches count, because re-encoding shifts a bit or two."""
    channels = set()
    for other, channel_id, taught in known:
        if hamming(picture, other) <= STILL_BITS:
            if taught:
                return True
            channels.add(channel_id)
    return len(channels) >= max(2, cfg.content_shared_channels)


def assess_sample(result, cfg, known_hashes=(), container=None):
    """What's wrong with one sample, before any rule about repeats.

    Returns (problems, still picture hash). A sample that can't answer a question leaves it
    out: having no measurement never counts against a stream.
    """
    problems = []
    if result.get("black") and cfg.content_black:
        problems.append("black")
    if cfg.content_placeholder and container in FILE_CONTAINERS:
        problems.append("file")
    still = still_hash(result.get("hashes") or [])
    if still is not None and cfg.content_placeholder and is_known_placeholder(still, known_hashes, cfg):
        problems.append("placeholder")
    if result.get("frozen") and not result.get("black") and cfg.content_frozen:
        problems.append("frozen")
    if result.get("has_audio"):
        if result.get("silent") and cfg.content_silence:
            problems.append("silence")
    elif cfg.content_no_audio and result.get("audio_known"):
        problems.append("no_audio")
    return problems, still


def content_verdict(samples, cfg, now):
    """Is one problem still there across enough samples, over enough time?

    samples is newest first, each {"at", "problems", "audible"}. A picture problem with
    sound still playing may be a radio channel with a card, or a dark film scene, so it has
    to last twice as long, and doesn't count at all if that setting is off.
    Returns the problem to act on, or None.
    """
    needed, span = cfg.content_needed()
    for problem in PROBLEM_ORDER:
        run = []
        for sample in samples:
            if problem not in sample["problems"]:
                break
            run.append(sample)
        if not run:
            continue
        want, wait = needed, span
        if problem in NEEDS_LONGER_WITH_SOUND and any(sample.get("audible") for sample in run):
            if not cfg.content_still_with_sound:
                continue
            want, wait = needed * 2, span * 2
        if len(run) >= want and run[0]["at"] - run[-1]["at"] >= wait:
            return problem
    return None


def loop_verdict(samples, cfg):
    """A clip going round shows nothing new for minutes on end. Adverts don't.

    The same advert in every break is seen again too, but the programme between breaks is
    new each time, so repeats only count while they keep coming: a run that ends with the
    newest sample, with at most LOOP_RUN_GAP new samples in a row inside it (a sample can
    land on frames of the clip not seen yet). The run needs twice the hits and twice the
    time of other problems, and at least LOOP_RUN_MIN_HITS over LOOP_RUN_MIN_SECONDS, so it
    outlasts an ordinary advert break.
    samples is newest first, each {"at", "looped"}.
    """
    if not cfg.content_looping or not samples or not samples[0].get("looped"):
        return None
    needed, span = cfg.content_needed()
    want, wait = max(LOOP_RUN_MIN_HITS, needed * 2), max(LOOP_RUN_MIN_SECONDS, span * 2)
    hits, misses = [], 0
    for sample in samples:
        if sample.get("looped"):
            hits.append(sample)
            misses = 0
            continue
        misses += 1
        if misses > LOOP_RUN_GAP:
            break
    if len(hits) >= want and hits[0]["at"] - hits[-1]["at"] >= wait:
        return "looping"
    return None
