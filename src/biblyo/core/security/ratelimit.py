# SPDX-License-Identifier: EUPL-1.2
"""Limitation par adresse : décompte des échecs, en mémoire. Le verrouillage par compte est en base."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque

from fastapi import Request

MAX_KEYS = 10_000


class FailureLimiter:
    """Au-delà de `limit` échecs sur `window` secondes, la clé est bloquée jusqu'à ce qu'ils expirent."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, moment: float) -> deque[float]:
        hits = self._failures.get(key, deque())
        while hits and moment - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            self._failures.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def record(self, key: str) -> None:
        moment = time.monotonic()
        with self._lock:
            hits = self._prune(key, moment)
            if not hits and len(self._failures) >= MAX_KEYS:
                self._failures.clear()  # mémoire bornée : le verrouillage par compte reste en place
            hits.append(moment)
            self._failures[key] = hits

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _is_trusted(address: str, trusted: tuple[str, ...]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if parsed in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request, trusted: tuple[str, ...]) -> str:
    """Adresse du client. `X-Forwarded-For` n'est lu que si la connexion vient d'un proxy déclaré."""
    peer = request.client.host if request.client else "inconnue"
    if not _is_trusted(peer, trusted):
        return peer
    hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    return peer
