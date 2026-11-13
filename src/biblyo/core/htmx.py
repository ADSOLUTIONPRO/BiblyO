# SPDX-License-Identifier: EUPL-1.2
"""Détection des requêtes HTMX : une même route rend une page complète ou un fragment."""

from __future__ import annotations

from fastapi import Request


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-History-Restore-Request") != "true"
