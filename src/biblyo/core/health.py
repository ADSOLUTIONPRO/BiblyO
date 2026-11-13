# SPDX-License-Identifier: EUPL-1.2
"""Sonde de santé, publique : aucun détail au-delà de l'état."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from biblyo import __version__

router = APIRouter()


@router.get("/sante", include_in_schema=False)
def health(request: Request) -> JSONResponse:
    state = request.app.state.schema.current(refresh=True)
    body = {
        "status": "ok" if state.ok else "maintenance",
        "schema": state.status.value,
        "version": __version__,
    }
    return JSONResponse(body, status_code=200 if state.ok else 503)
