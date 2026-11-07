"""Stream Demote: moves streams that fail for real viewers down the channel's order.

Watching is passive and uses no provider connections. The one thing that opens a stream is
moving a viewer who's still there to the next one, when asked to. See the README.
"""

from __future__ import annotations

import logging
import os
import sys

if __package__:
    from .stream_demote import dispatcharr as dispatcharr_module
    from .stream_demote import engine, flag_route, ticker
    from .stream_demote.service import Service
    from .stream_demote.store import Store
    from .stream_demote.dispatcharr import Dispatcharr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from stream_demote import dispatcharr as dispatcharr_module
    from stream_demote import engine, flag_route, ticker
    from stream_demote.dispatcharr import Dispatcharr
    from stream_demote.service import Service
    from stream_demote.store import Store

logger = logging.getLogger(__name__)

# Dispatcharr names a plugin after its folder.
PLUGIN_KEY = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace(" ", "_").lower()
EVENTS = ("channel_start", "stream_switch", "channel_error", "channel_stop", "m3u_refresh")

_service = None


def get_service():
    global _service
    if _service is None:
        from django.db import close_old_connections, connection

        store = Store(lambda: connection.cursor(), vendor=connection.vendor)
        _service = Service(store, Dispatcharr(PLUGIN_KEY), after_timer=close_old_connections)
    return _service


def _tidy_up():
    from django.db import close_old_connections

    close_old_connections()


def _set_up_process():
    # Celery has no web requests to serve, and runs none of this. Django management commands
    # that skip plugin discovery never import this file at all.
    if any("celery" in part for part in sys.argv[:1]):
        return
    try:
        flag_route.install(get_service)
    except Exception:
        logger.exception("Could not add the flag route")
    try:
        dispatcharr_module.install_hand_edit_hook()
    except Exception:
        logger.exception("Could not watch for hand edits, so an edit in place will count as by hand")
    try:
        if ticker.should_run():
            ticker.start(get_service, _tidy_up)
    except Exception:
        logger.exception("Could not start the background check")


if __package__:
    _set_up_process()


class Plugin:
    name = "Stream Demote"
    version = "0.3.0"
    description = "Moves streams that fail for real viewers down the channel's order, and keeps them there when other tools re-sort."

    def stop(self, context=None):
        """Dispatcharr calls this on disable, delete and reload.

        Disabling leaves the background loop idling, because enabling again doesn't reload this
        file. A reload starts a new loop, and the old one sees that and ends by itself.
        """
        if (context or {}).get("reason") == "delete":
            ticker.stop()

    def run(self, action, params, context):
        params = params or {}
        settings = engine.Settings((context or {}).get("settings"))
        service = get_service()

        if action == "on_event":
            # The UI also shows a Run button for this action. With no event it does nothing.
            event = params.get("event")
            if event:
                service.handle_event(event, params.get("payload") or {})
            return {"status": "ok"}
        if action == "status":
            return service.status(flag_route.is_live())
        if action == "recent":
            path, rows = service.write_journal_csv()
            lines = [_describe(row) for row in rows[:5]]
            message = " | ".join(lines) if lines else "The journal is empty."
            return {"status": "ok", "message": message, "file": path, "rows": _safe_rows(rows[:50])}
        if action == "undo_last":
            service.ready()
            moved = service.undo(limit=1)
            return {"status": "ok", "moved": moved, "message": f"Moved {moved} stream back."}
        if action == "undo_channel":
            return self._undo_channel(service, settings)
        if action == "undo_all":
            service.ready()
            moved = service.undo()
            return {"status": "ok", "moved": moved, "message": f"Moved {moved} streams back."}
        if action == "create_token":
            token, problem = service.create_token(settings.token_label)
            if problem:
                return {"status": "error", "message": problem}
            # The UI keeps "file" on screen after the notification fades.
            return {
                "status": "ok",
                "message": "Token created. Copy it from Output on this page now. It won't be shown again.",
                "file": token,
                "token": token,
            }
        if action == "list_tokens":
            service.ready()
            rows = service.store.list_tokens()
            live = [row["label"] for row in rows if not row["revoked_at"]]
            return {
                "status": "ok",
                "message": "Live tokens: " + (", ".join(live) if live else "none"),
                "tokens": rows,
            }
        if action == "revoke_token":
            count = service.revoke_token(settings.token_label)
            return {"status": "ok", "revoked": count, "message": f"Revoked {count} token(s)."}
        if action == "teach_placeholder":
            return service.teach_channel(settings.teach_channel, settings)
        if action == "forget_placeholders":
            service.ready()
            service.store.forget_pictures()
            return {"status": "ok", "message": "Forgot every remembered placeholder."}
        if action == "erase_data":
            service.ready()
            service.store.erase()
            return {"status": "ok", "message": "Erased the journal, tokens and history. Stream order was not changed."}
        return {"status": "error", "message": f"Unknown action {action}"}

    def _undo_channel(self, service, settings):
        service.ready()
        number = str(settings.undo_channel).strip()
        try:
            channels = service.dp.channels_by_number(float(number))
        except ValueError:
            return {"status": "error", "message": "Type a channel number in the Channel to undo setting first."}
        moved = sum(service.undo(channel_id=channel["id"]) for channel in channels)
        return {"status": "ok", "moved": moved, "message": f"Moved {moved} streams back on channel {number}."}


def _describe(row):
    import time

    stamp = time.strftime("%H:%M", time.gmtime(row["at"]))
    return f"{stamp} {row['action']} stream {row['stream_id']} channel {row['channel_id']} ({row['cause'] or ''})"


def _safe_rows(rows):
    return [{name: row.get(name) for name in ("at", "action", "channel_id", "stream_id", "cause", "actor", "undone_at")} for row in rows]
