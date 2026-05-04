"""
FastAPI middleware that sets a per-request query-tag context.

For every incoming HTTP request we push:
    app=bhe_catalog
    module=ui_api
    submodule=<route function name>     (or <method>_<path> if no name)
    request_id=<X-Request-Id or uuid>
    user=<X-Forwarded-Preferred-Username if present>

Any SQL submitted via db._execute_sql_api during the request will pick these
up automatically and pass them as the SEA `query_tags` field, so the
Databricks "Query tags" column shows precisely which UI call issued the query.
"""

from __future__ import annotations

import re
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from .._query_tag import push_tags, reset_tags

# Tag values must obey: no , : - / = . in the KEY (values are fine).
# We sanitize submodule values to be readable + URL-safe.
_SANITIZE = re.compile(r"[^A-Za-z0-9_./-]+")


def _safe_value(s: str, max_len: int = 128) -> str:
    return _SANITIZE.sub("_", s)[:max_len].strip("_") or "unknown"


def _resolve_submodule(request: Request) -> str:
    """Pick the most descriptive identifier we can find for the route."""
    route = request.scope.get("route")
    if route is not None:
        # FastAPI routes have a .name (function name) and a .path (template).
        name = getattr(route, "name", None)
        if name:
            return _safe_value(name)
        path = getattr(route, "path", None)
        if path:
            return _safe_value(f"{request.method.lower()}_{path}")
    return _safe_value(f"{request.method.lower()}_{request.url.path}")


async def _query_tag_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    user = request.headers.get("x-forwarded-preferred-username") or ""
    submodule = _resolve_submodule(request)

    token = push_tags(
        module="ui_api",
        submodule=submodule,
        request_id=request_id,
        user=_safe_value(user) if user else "",
    )
    try:
        response = await call_next(request)
    finally:
        reset_tags(token)

    response.headers.setdefault("x-request-id", request_id)
    return response


def install_query_tag_middleware(app: FastAPI) -> None:
    app.middleware("http")(_query_tag_middleware)
