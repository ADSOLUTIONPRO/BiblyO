# SPDX-License-Identifier: EUPL-1.2
"""Taille des requêtes bornée : le jeton CSRF se lit dans le corps, donc avant toute authentification."""

from __future__ import annotations

import re

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from biblyo.core.media import MAX_UPLOAD_BYTES

#: Suffisant pour tous les formulaires. Seules les routes de téléversement ci-dessous ont un autre plafond.
MAX_BODY_BYTES = 1024 * 1024
_MULTIPART_MARGIN = 64 * 1024
ROUTE_LIMITS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^/catalogue/editions/\d+/couverture$"), MAX_UPLOAD_BYTES + _MULTIPART_MARGIN),
    (re.compile(r"^/catalogue/notices/\d+/couverture/fichier$"), MAX_UPLOAD_BYTES + _MULTIPART_MARGIN),
)


def limit_for(path: str, default: int) -> int:
    return next((limit for pattern, limit in ROUTE_LIMITS if pattern.match(path)), default)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["path"], self.limit)
        declared = Headers(scope=scope).get("content-length", "0")
        if declared.isdigit() and int(declared) > limit:
            await PlainTextResponse("Requête trop volumineuse.", status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413)  # envoi par morceaux, sans taille annoncée
            return message

        await self.app(scope, counted, send)
