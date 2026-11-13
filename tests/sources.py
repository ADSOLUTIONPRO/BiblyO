# SPDX-License-Identifier: EUPL-1.2
"""Accès aux réponses enregistrées des sources (`tests/fixtures/sources/`) pour les tests de l'enrichissement."""

from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import Element  # nosec B405 : analyse faite par defusedxml ci-dessous

from defusedxml import ElementTree as SafeElementTree

from biblyo.modules.enrichment.connectors import unimarc
from biblyo.modules.enrichment.connectors.base import Candidate

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def path_of(name: str) -> Path:
    """« bnf/isbn-exact-petit-prince » ou « sudoc/notice-par-ppn »."""
    return FIXTURES / f"{name}.xml"


def read_bytes(name: str) -> bytes:
    return path_of(name).read_bytes()


def load(name: str) -> Element:
    return SafeElementTree.fromstring(read_bytes(name))


def candidates(name: str) -> list[Candidate]:
    """Les candidats d'une réponse enregistrée, lus comme le connecteur de la source le ferait."""
    source = name.split("/", 1)[0]
    found: list[Candidate] = []
    for element in unimarc.iter_records(load(name)):
        record = unimarc.read(element)
        if source == "bnf":
            external_id = unimarc.ark_of(record)
            url = f"https://catalogue.bnf.fr/{external_id}"
        else:
            external_id = unimarc.ppn_of(record)
            url = f"https://www.sudoc.fr/{external_id}"
        found.append(unimarc.candidate(record, source=source, url=url, external_id=external_id))
    return found


def candidate(name: str, external_id: str) -> Candidate:
    for found in candidates(name):
        if found.external_id == external_id:
            return found
    raise LookupError(f"{external_id} absent de {name}")
