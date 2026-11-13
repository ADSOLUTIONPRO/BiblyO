# SPDX-License-Identifier: EUPL-1.2
"""Identifiants publics : UUID v7, stables d'un export à l'autre et triés par date de création."""

from __future__ import annotations

import secrets
import uuid

from biblyo.core.clock import now


def new_public_id() -> str:
    """UUID v7 (RFC 9562) : horodatage en millisecondes, puis aléa. La bibliothèque standard ne le fournit pas ici."""
    milliseconds = int(now().timestamp() * 1000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    high = (milliseconds << 80) | (0x7 << 76) | ((random_bits >> 62) << 64)
    low = (0b10 << 62) | (random_bits & ((1 << 62) - 1))
    value = high | low
    return str(uuid.UUID(int=value))
