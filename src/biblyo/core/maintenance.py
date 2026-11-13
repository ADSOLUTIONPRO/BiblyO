# SPDX-License-Identifier: EUPL-1.2
"""Mode maintenance : tant que la base n'est pas à jour, seules quelques pages répondent."""

from __future__ import annotations

from jinja2 import Environment
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from biblyo.core.schema_state import SchemaMonitor

#: `05` § 1.2 : en maintenance, seules la page de maintenance, la santé et les fichiers statiques répondent.
ALWAYS_ALLOWED = frozenset({"/sante"})


class MaintenanceMiddleware:
    def __init__(self, app: ASGIApp, monitor: SchemaMonitor, env: Environment) -> None:
        self.app = app
        self.monitor = monitor
        self.env = env

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in ALWAYS_ALLOWED or path.startswith("/static/"):
            await self.app(scope, receive, send)
            return
        state = await run_in_threadpool(self.monitor.current)
        if state.ok:
            await self.app(scope, receive, send)
            return
        body = self.env.get_template("layouts/maintenance.html").render(state=state).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"60"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
