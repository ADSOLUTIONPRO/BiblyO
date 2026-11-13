# SPDX-License-Identifier: EUPL-1.2
"""En-têtes de sécurité, sur toutes les réponses (04-SECURITE-ROLES.md, § 2)."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
}
IMMUTABLE = "public, max-age=31536000, immutable"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        versioned = path.startswith("/static/") and b"v=" in scope.get("query_string", b"")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if versioned and message["status"] == 200:
                    headers["Cache-Control"] = IMMUTABLE
                elif "text/html" in headers.get("content-type", ""):
                    # Une page peut contenir des données personnelles ou un mot de passe provisoire.
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, wrapped)
