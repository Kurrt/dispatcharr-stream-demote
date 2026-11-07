"""A small background loop, because some checks can't wait for the next event.

Every web worker runs one, and a shared claim means only one of them does the work each
time. It runs inside Dispatcharr's own process, so it opens no connection to anything but
its database and Redis.
"""

from __future__ import annotations

import logging
import random
import sys
import threading
import time
import types

logger = logging.getLogger(__name__)

TICK_SECONDS = 15
HOLDER = "_stream_demote_ticker"


def _holder():
    # This survives Dispatcharr reloading the plugin's code, so an old loop can see it's old.
    holder = sys.modules.get(HOLDER)
    if holder is None:
        holder = types.ModuleType(HOLDER)
        holder.generation = 0
        sys.modules[HOLDER] = holder
    return holder


def should_run():
    """Only processes that serve streams. Celery and management commands never run the loop."""
    try:
        from dispatcharr.db.process_label import get_process_role

        return get_process_role() in ("uwsgi", "gunicorn")
    except Exception:
        return False


def start(get_service, after_tick=None):
    holder = _holder()
    holder.generation += 1
    mine = holder.generation
    thread = threading.Thread(target=_loop, args=(get_service, after_tick, mine), name="stream-demote-tick", daemon=True)
    thread.start()
    return mine


def stop():
    _holder().generation += 1


def _loop(get_service, after_tick, mine):
    holder = _holder()
    time.sleep(random.uniform(1, TICK_SECONDS))
    logged = 0.0
    while holder.generation == mine:
        keep_going = True
        try:
            keep_going = get_service().tick()
        except Exception:
            now = time.time()
            if now - logged > 600:
                logged = now
                logger.exception("Stream Demote's background check failed")
            else:
                logger.warning("Stream Demote's background check failed again")
        finally:
            if after_tick:
                try:
                    after_tick()
                except Exception:
                    logger.debug("Could not tidy up after a background check", exc_info=True)
        # The plugin was removed, so there's nothing left to do here.
        if keep_going is False:
            return
        time.sleep(TICK_SECONDS)
