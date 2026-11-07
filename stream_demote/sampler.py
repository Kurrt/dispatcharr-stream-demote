"""Runs ffmpeg on bytes Dispatcharr already holds. The only module that starts a program.

ffmpeg only ever gets a local file with the demuxer forced to MPEG-TS and only the file
protocol allowed, so a stream can't make it open anything else. Nothing here knows a URL.
The sample is deleted as soon as the check ends, and only numbers come back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

SILENCE_DB = -70.0
AUDIBLE_DB = -60.0
HASH_WIDTH = 9
HASH_HEIGHT = 8
FRAMES_PER_SECOND = 2
FLAT_RANGE = 12
MIN_COVERED = 0.6
BLACK_SHARE = 0.9
OUTPUT_LIMIT = 256 * 1024


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _prefix():
    nice = shutil.which("nice")
    return [nice, "-n", "19"] if nice else []


def probe_command(path):
    return [
        shutil.which("ffprobe") or "ffprobe", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file", "-f", "mpegts",
        "-show_entries", "stream=codec_type", "-of", "json", path,
    ]


def check_command(path, frames_path, seconds, has_video, has_audio):
    still_for = max(1, int(seconds) - 1)
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1", "-benchmark",
        "-protocol_whitelist", "file", "-f", "mpegts", "-i", path,
    ]
    if has_video:
        command += [
            "-filter_complex",
            f"[0:v:0]split=2[look][hash];"
            f"[look]blackdetect=d=1:pic_th=0.98,freezedetect=n=-60dB:d={still_for}[seen];"
            f"[hash]fps={FRAMES_PER_SECOND},scale={HASH_WIDTH}:{HASH_HEIGHT}:flags=area,format=gray[small]",
            "-map", "[seen]", "-f", "null", "-",
            # Passthrough, or it pads out the time before the first keyframe with copies.
            "-map", "[small]", "-fps_mode", "passthrough", "-f", "rawvideo", "-y", frames_path,
        ]
    if has_audio:
        command += [
            "-map", "0:a:0", "-af", f"silencedetect=n={int(SILENCE_DB)}dB:d={still_for},volumedetect",
            "-f", "null", "-",
        ]
    return command


def frame_hash(frame):
    """A 64-bit picture fingerprint from a 9x8 grey frame, or None if the frame is flat."""
    if len(frame) != HASH_WIDTH * HASH_HEIGHT:
        return None
    if max(frame) - min(frame) < FLAT_RANGE:
        return None
    value = 0
    for row in range(HASH_HEIGHT):
        start = row * HASH_WIDTH
        for column in range(HASH_WIDTH - 1):
            value = (value << 1) | (1 if frame[start + column] < frame[start + column + 1] else 0)
    return value


def frame_hashes(raw):
    size = HASH_WIDTH * HASH_HEIGHT
    return [frame_hash(raw[index:index + size]) for index in range(0, len(raw) - size + 1, size)]


def parse_probe(text):
    """Returns (has video, has audio). None if ffprobe said nothing useful."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    streams = data.get("streams")
    if not isinstance(streams, list):
        return None
    kinds = [item.get("codec_type") for item in streams if isinstance(item, dict)]
    return "video" in kinds, "audio" in kinds


_BLACK = re.compile(r"black_duration:\s*([\d.]+)")
_FREEZE = re.compile(r"lavfi\.freezedetect\.freeze_start:\s*[\d.]+")
_FREEZE_END = re.compile(r"lavfi\.freezedetect\.freeze_end:\s*[\d.]+")
_SILENCE = re.compile(r"silence_start:\s*-?[\d.]+")
_MEAN = re.compile(r"mean_volume:\s*(-?(?:inf|[\d.]+))\s*dB")
_SAMPLES = re.compile(r"n_samples:\s*(\d+)")
_CPU = re.compile(r"bench: utime=([\d.]+)s stime=([\d.]+)s")


def parse_check(stderr, hashes, has_video, has_audio):
    """Turns ffmpeg's log into findings. Each one needs positive evidence to be true."""
    covered = len(hashes) / FRAMES_PER_SECOND
    result = {
        "has_video": has_video, "has_audio": False, "audio_known": covered > 0, "seconds": covered,
        "black": False, "frozen": False, "silent": False, "audible": False, "mean_db": None,
        "hashes": hashes, "cpu": None,
    }
    cpu = _CPU.search(stderr)
    if cpu:
        result["cpu"] = float(cpu.group(1)) + float(cpu.group(2))
    if has_video and covered > 0:
        black = sum(float(item) for item in _BLACK.findall(stderr))
        result["black"] = black >= BLACK_SHARE * covered
        # A freeze still going at the end of the sample has a start and no end. It only
        # reports once it has lasted nearly a whole sample.
        result["frozen"] = len(_FREEZE.findall(stderr)) > len(_FREEZE_END.findall(stderr))
    if has_audio:
        # ffmpeg logs a zero count while setting up, so only the last one is real.
        counts = _SAMPLES.findall(stderr)
        samples = int(counts[-1]) if counts else 0
        means = _MEAN.findall(stderr)
        mean = float(means[-1]) if means else None
        result["has_audio"] = samples > 0
        result["mean_db"] = mean
        result["audible"] = mean is not None and mean > AUDIBLE_DB
        result["silent"] = samples > 0 and mean is not None and mean <= SILENCE_DB and bool(_SILENCE.search(stderr))
    return result


def _run(command, stdout_path, stderr_path, timeout):
    """Runs a command with files, not pipes, for all its input and output."""
    with open(os.devnull, "rb") as nothing, open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(command, stdin=nothing, stdout=out, stderr=err, close_fds=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return None


def _read_tail(path):
    with open(path, "rb") as handle:
        return handle.read()[-OUTPUT_LIMIT:].decode("utf-8", "replace")


def check_bytes(data, seconds, timeout=20):
    """Checks one sample of MPEG-TS. Returns a dict of findings, or None if it gives no answer."""
    if not data or not available():
        return None
    folder = None
    started = time.time()
    try:
        folder = tempfile.mkdtemp(prefix="stream-demote-")
        path = os.path.join(folder, "sample.ts")
        with open(path, "wb") as handle:
            handle.write(data)
        out_path = os.path.join(folder, "out.txt")
        err_path = os.path.join(folder, "err.txt")
        frames_path = os.path.join(folder, "frames.gray")
        if _run(_prefix() + probe_command(path), out_path, err_path, timeout) != 0:
            return None
        probed = parse_probe(_read_tail(out_path))
        if probed is None:
            return None
        has_video, has_audio = probed
        if not has_video and not has_audio:
            return None
        if not has_video:
            # Radio. There's nothing to judge, which is what the caller needs to know.
            return {"has_video": False, "has_audio": True, "audio_known": True, "seconds": 0.0, "black": False,
                    "frozen": False, "silent": False, "audible": True, "mean_db": None, "hashes": [],
                    "cpu": None}
        left = max(1.0, timeout - (time.time() - started))
        code = _run(_prefix() + check_command(path, frames_path, seconds, has_video, has_audio), out_path, err_path, left)
        if code != 0:
            return None
        with open(frames_path, "rb") as handle:
            hashes = frame_hashes(handle.read())
        result = parse_check(_read_tail(err_path), hashes, has_video, has_audio)
        if result["seconds"] < MIN_COVERED * seconds:
            return None
        return result
    except Exception as error:
        logger.debug("Content check gave no answer: %s", type(error).__name__)
        return None
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
