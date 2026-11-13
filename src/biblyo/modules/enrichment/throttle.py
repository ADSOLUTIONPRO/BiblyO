# SPDX-License-Identifier: EUPL-1.2
"""Espacement des appels par source et mise de côté d'une source en panne.

Un régulateur par source, commun aux recherches interactives et au futur lot : jamais deux appels à moins de
l'intervalle réglé. Après trois échecs de suite, la source est mise de côté cinq minutes, puis dix, puis trente :
aucune relance en rafale pendant une panne. `Retry-After` prolonge la mise de côté.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

FAILURES_BEFORE_SIDELINING = 3
SIDELINE_STEPS = (300.0, 600.0, 1800.0)
MAX_WAIT = 60.0


@dataclass
class _State:
    next_allowed: float = 0.0
    failures: int = 0
    sidelined_until: float = 0.0
    sidelines: int = 0


class Throttle:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}

    def _state(self, source: str) -> _State:
        return self._states.setdefault(source, _State())

    def sidelined_for(self, source: str) -> float:
        """Secondes restantes de mise de côté, 0 si la source est disponible."""
        with self._lock:
            return max(0.0, self._state(source).sidelined_until - self._clock())

    def acquire(self, source: str, interval_s: float) -> None:
        """Attend le créneau de la source, puis le réserve. Lève `Sidelined` si elle est mise de côté."""
        with self._lock:
            state = self._state(source)
            now = self._clock()
            if state.sidelined_until > now:
                raise Sidelined(source, state.sidelined_until - now)
            wait = min(max(0.0, state.next_allowed - now), MAX_WAIT)
            state.next_allowed = max(now, state.next_allowed) + interval_s
        if wait > 0:
            self._sleep(wait)

    def succeeded(self, source: str) -> None:
        with self._lock:
            state = self._state(source)
            state.failures = 0
            state.sidelines = 0

    def failed(self, source: str, *, retry_after: float | None = None) -> float:
        """Enregistre un échec ; rend la durée de mise de côté décidée, 0 si la source reste disponible."""
        with self._lock:
            state = self._state(source)
            state.failures += 1
            now = self._clock()
            duration = 0.0
            if retry_after is not None:
                duration = min(max(retry_after, 1.0), SIDELINE_STEPS[-1])
            elif state.failures >= FAILURES_BEFORE_SIDELINING:
                duration = SIDELINE_STEPS[min(state.sidelines, len(SIDELINE_STEPS) - 1)]
                state.sidelines += 1
                state.failures = 0
            if duration:
                state.sidelined_until = max(state.sidelined_until, now + duration)
            return duration

    def reset(self, source: str) -> None:
        with self._lock:
            self._states.pop(source, None)


class Sidelined(RuntimeError):
    def __init__(self, source: str, remaining: float) -> None:
        super().__init__(f"{source} mise de côté pour {remaining:.0f} s")
        self.source = source
        self.remaining = remaining
