# SPDX-License-Identifier: EUPL-1.2
"""Nom lisible du navigateur dans « Mon compte »."""

from __future__ import annotations

import pytest

from biblyo.core.useragent import describe_agent

MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15"
BLINK = "AppleWebKit/537.36 (KHTML, like Gecko)"
CHROME = "Chrome/136.0.0.0 Safari/537.36"


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        (f"{MAC} {BLINK} {CHROME}", "Chrome sur macOS"),
        (f"{MAC} {BLINK} Headless{CHROME}", "Chrome sur macOS"),
        (f"{WINDOWS} {BLINK} {CHROME} Edg/136.0.0.0", "Edge sur Windows"),
        (f"{WINDOWS} {BLINK} {CHROME} OPR/120.0.0.0", "Opera sur Windows"),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", "Firefox sur Linux"),
        (f"{MAC} AppleWebKit/605.1.15 Version/18.5 Safari/605.1.15", "Safari sur macOS"),
        (f"{IPHONE} Version/18.5 Mobile/15E148 Safari/604.1", "Safari sur iOS"),
        (f"{IPHONE} CriOS/136.0 Mobile/15E148 Safari/604.1", "Chrome sur iOS"),
        (f"Mozilla/5.0 (Linux; Android 15; Pixel 9) {BLINK} {CHROME}", "Chrome sur Android"),
        ("Firefox/140.0", "Firefox"),
        ("curl/8.7.1", "Navigateur inconnu"),
        ("<script>alert(1)</script>", "Navigateur inconnu"),
        ("", "Navigateur inconnu"),
        (None, "Navigateur inconnu"),
    ],
)
def test_describe_agent(signature: str | None, expected: str) -> None:
    assert describe_agent(signature) == expected
