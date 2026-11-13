# SPDX-License-Identifier: EUPL-1.2
"""Liste des modules de l'application."""

from __future__ import annotations

from biblyo.core.modules import Module


def all_modules() -> list[Module]:
    from biblyo.modules.admin.module import MODULE as admin
    from biblyo.modules.catalogue.module import MODULE as catalogue
    from biblyo.modules.circulation.module import MODULE as circulation
    from biblyo.modules.dashboard.module import MODULE as dashboard
    from biblyo.modules.devtools.module import MODULE as devtools
    from biblyo.modules.enrichment.module import MODULE as enrichment
    from biblyo.modules.holdings.module import MODULE as holdings
    from biblyo.modules.imports.module import MODULE as imports
    from biblyo.modules.patrons.module import MODULE as patrons

    return [dashboard, admin, catalogue, holdings, patrons, circulation, enrichment, imports, devtools]
