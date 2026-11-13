# SPDX-License-Identifier: EUPL-1.2
"""Outils de développement : catalogue de composants."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.modules import Module
from biblyo.modules.devtools.routes import router

MODULE = Module(
    code="devtools",
    label="Outils de développement",
    router=router,
    template_dir=Path(__file__).parent / "templates",
)
