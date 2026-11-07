"""The flag route. Plugins have no route of their own, so this one is added to Django's list.

That isn't a supported plugin API, so it's kept apart from everything else. If a release
breaks it, demotion by events keeps working and only the flag button stops.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

ROUTE = "api/stream-demote/flag/"
ROUTE_NAME = "stream_demote_flag"
MAX_BODY = 1024


def _network_allowed(request):
    """Follows the same Stream Endpoints list in Network Access as the streams themselves.

    A plugin route gets no check by default. If a release moves this function, the token
    is still needed, so the route carries on and says so in the log.
    """
    try:
        from dispatcharr.utils import network_access_allowed
    except ImportError:
        logger.warning("Could not check the network access list for the flag route")
        return True
    return network_access_allowed(request, "STREAMS")


def make_view(get_service):
    from django.http import JsonResponse
    from django.views.decorators.csrf import csrf_exempt

    def reply(status, body, headers=None):
        response = JsonResponse(body, status=status)
        response["Cache-Control"] = "no-store"
        for name, value in (headers or {}).items():
            response[name] = value
        return response

    @csrf_exempt
    def flag_view(request):
        service = get_service()
        # The route stays in memory until a restart, so a disabled plugin must answer here too.
        if not service.dp.plugin_state()[0]:
            return reply(404, {"result": "disabled"})
        if request.method != "POST":
            return reply(405, {"result": "post_only"}, {"Allow": "POST"})
        if not _network_allowed(request):
            return reply(403, {"result": "forbidden"})
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return reply(413, {"result": "too_large"})
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else {}
        except (UnicodeDecodeError, ValueError):
            body = None
        try:
            status, data, headers = service.flag(request.META.get("HTTP_AUTHORIZATION", ""), body)
        except Exception as error:
            service.note_problem("flag", error)
            return reply(500, {"result": "error"})
        return reply(status, data, headers)

    return flag_view


def install(get_service):
    """Add the route once. Returns True if it's now present."""
    from django.urls import path

    from dispatcharr import urls as root

    if any(getattr(item, "name", None) == ROUTE_NAME for item in root.urlpatterns):
        return True
    # Ahead of the catch-all that serves the web app.
    root.urlpatterns.insert(0, path(ROUTE, make_view(get_service), name=ROUTE_NAME))
    return True


def is_live():
    try:
        from dispatcharr import urls as root
    except Exception:
        return False
    return any(getattr(item, "name", None) == ROUTE_NAME for item in root.urlpatterns)
