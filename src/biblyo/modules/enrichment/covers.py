# SPDX-License-Identifier: EUPL-1.2
"""Couvertures des sources : quelle image demander, à quel service, et comment dire ce qui s'est passé.

Une couverture n'est demandée que par un identifiant contrôlé : l'ARK de la notice de la BnF quand elle est connue
(directement, ou citée par une notice du Sudoc), sinon l'EAN ou l'ISBN confirmé. La réponse HTTP 500 du service n'est
jamais classée d'office : la notice dit si une image était annoncée. La panne du service de couvertures n'empêche
jamais d'appliquer les autres champs.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from biblyo.core.i18n import _
from biblyo.modules.enrichment.connectors.base import Candidate, Capability, CoverRequest, CoverResult
from biblyo.modules.enrichment.connectors.bnf import ARK, LICENCE, BnfConnector
from biblyo.modules.enrichment.sources import SourceConfig, SourceHub

PREVIEW_CACHE_SIZE = 200
STATUS_LABELS: dict[str, str] = {
    "found": _("Couverture récupérée"),
    "announced_not_served": _("La notice annonce une couverture, mais le service ne la fournit pas."),
    "none_found": _("Aucune image trouvée pour cette notice."),
    "unavailable": _(
        "Service de couvertures indisponible : réessayez plus tard. Les autres champs restent applicables."
    ),
    "invalid_image": _("La réponse du service n’est pas une image valide : rien n’a été enregistré."),
    "unsupported": _("Les couvertures sont désactivées pour cette source."),
    "not_announced": _("Aucune couverture annoncée."),
}


def ark_for(candidate: Candidate) -> str:
    """L'ARK de la BnF qui porte la couverture : celui du candidat, ou celui qu'une notice du Sudoc cite."""
    if candidate.source == "bnf" and ARK.match(candidate.external_id):
        return candidate.external_id
    return next((value for value in candidate.linked_ids if ARK.match(value)), "")


def offered(candidate: Candidate) -> bool:
    """Une couverture peut être proposée : annoncée par la notice, ou cherchable par la notice de la BnF liée."""
    return candidate.has_cover or bool(ark_for(candidate))


def request_for(candidate: Candidate, *, original: bool) -> CoverRequest | None:
    ark = ark_for(candidate)
    if ark:
        return CoverRequest(external_id=ark, side=1, announced=candidate.has_cover, original=original)
    if candidate.edition.ean:
        return CoverRequest(ean=candidate.edition.ean[0], side=1, announced=candidate.has_cover, original=original)
    if candidate.edition.isbn13:
        return CoverRequest(isbn=candidate.edition.isbn13[0], side=1, announced=candidate.has_cover, original=original)
    return None


def provider(hub: SourceHub) -> SourceConfig | None:
    """La source qui fournit les couvertures : activée, couvertures activées, connecteur capable."""
    for config in hub.configs():
        if not (config.enabled and config.covers_enabled):
            continue
        connector = hub.connector(config)
        if connector is not None and Capability.COVER in connector.capabilities:
            return config
    return None


def fetch(hub: SourceHub, candidate: Candidate, *, original: bool) -> CoverResult:
    request = request_for(candidate, original=original)
    if request is None:
        return CoverResult("none_found", detail="aucun identifiant pour demander une couverture")
    config = provider(hub)
    if config is None:
        return CoverResult("unsupported")
    return hub.cover(config, request)


def public_url(candidate: Candidate) -> str:
    ark = ark_for(candidate)
    return BnfConnector.public_url(ark) if ark else candidate.url


def licence_note(retrieved_at: str) -> str:
    """Le texte des conditions, tel qu'il est conservé avec l'image et affiché dans les crédits."""
    return f"{LICENCE.covers_note} Récupérée le {retrieved_at[:10]}. Conditions : {LICENCE.url}"


class PreviewCache:
    """Vignettes déjà validées et réencodées, gardées en mémoire : un rechargement ne rappelle pas le service."""

    def __init__(self, size: int = PREVIEW_CACHE_SIZE) -> None:
        self._size = size
        self._lock = threading.Lock()
        self._items: OrderedDict[str, bytes] = OrderedDict()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)
