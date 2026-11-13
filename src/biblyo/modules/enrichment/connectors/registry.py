# SPDX-License-Identifier: EUPL-1.2
"""Connecteurs déclarés. Ajouter une source : écrire son connecteur, l'inscrire ici, rien d'autre."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from biblyo.modules.enrichment.connectors.base import Connector
from biblyo.modules.enrichment.connectors.bnf import BnfConnector
from biblyo.modules.enrichment.connectors.openlibrary import OpenLibraryConnector
from biblyo.modules.enrichment.connectors.sudoc import SudocConnector
from biblyo.modules.enrichment.connectors.transport import HttpTransport

Factory = Callable[[HttpTransport, float, Mapping[str, Any]], Connector]
FACTORIES: dict[str, Factory] = {
    "bnf": lambda transport, timeout, config: BnfConnector(transport, timeout_s=timeout, config=config),
    "sudoc": lambda transport, timeout, config: SudocConnector(transport, timeout_s=timeout),
    "openlibrary": lambda transport, timeout, config: OpenLibraryConnector(transport, timeout_s=timeout),
}
HOSTS: frozenset[str] = BnfConnector.hosts | SudocConnector.hosts | OpenLibraryConnector.hosts
#: sources capables de fournir une couverture : leur case « Couvertures » a un sens
COVER_SOURCES: frozenset[str] = frozenset({"bnf", "openlibrary"})
#: Sources qui demandent une adresse de contact dans l'en-tête User-Agent : sans elle, elles restent inactives.
CONTACT_REQUIRED: frozenset[str] = frozenset({"openlibrary"})


def build(
    code: str, transport: HttpTransport, *, timeout_s: float, config: Mapping[str, Any] | None = None
) -> Connector | None:
    factory = FACTORIES.get(code)
    return factory(transport, timeout_s, config or {}) if factory else None
