# SPDX-License-Identifier: EUPL-1.2
"""Module Scanner (lot L8) : identifier un code partout, modes de travail, assistant de la douchette."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module
from biblyo.modules.scan.routes import router

MODULE = Module(
    code="scan",
    label=_("Scanner"),
    router=router,
    depends_on=("catalogue", "holdings", "patrons", "circulation", "labels"),
    template_dir=Path(__file__).parent / "templates",
)
