# SPDX-License-Identifier: EUPL-1.2
"""Module Codes-barres et étiquettes (lot L8) : générer les codes manquants, imprimer des planches A4."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, Permission
from biblyo.modules.labels import models as models
from biblyo.modules.labels.copy import copy_labels
from biblyo.modules.labels.routes import router

MODULE = Module(
    code="labels",
    label=_("Codes-barres et étiquettes"),
    router=router,
    depends_on=("catalogue", "holdings", "admin"),
    permissions=(Permission("labels.print", _("Imprimer des étiquettes de codes-barres")),),
    template_dir=Path(__file__).parent / "templates",
    extras={"copy_labels": copy_labels},
)
