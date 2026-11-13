# SPDX-License-Identifier: EUPL-1.2
"""Lecture XML durcie : déclarations de type et entités refusées, taille, profondeur et nombre d'éléments bornés."""

from __future__ import annotations

from xml.etree.ElementTree import Element  # nosec B405 : l'analyse passe par defusedxml

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DTDForbidden, EntitiesForbidden

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 200_000


class InvalidXml(ValueError):
    """Réponse illisible ou dangereuse : classée « réponse invalide », jamais « aucun résultat », jamais en cache."""


def parse(
    body: bytes, *, max_bytes: int = MAX_BYTES, max_depth: int = MAX_DEPTH, max_elements: int = MAX_ELEMENTS
) -> Element:
    if not body or not body.strip():
        raise InvalidXml("réponse vide")
    if len(body) > max_bytes:
        raise InvalidXml(f"réponse de {len(body)} octets, au-delà de {max_bytes}")
    try:
        root: Element = SafeElementTree.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DTDForbidden, EntitiesForbidden) as exc:
        raise InvalidXml("déclaration XML refusée") from exc
    except (DefusedXmlException, SafeElementTree.ParseError, ValueError) as exc:
        raise InvalidXml(f"XML illisible : {type(exc).__name__}") from exc
    count = 0
    stack: list[tuple[Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if depth > max_depth:
            raise InvalidXml(f"profondeur au-delà de {max_depth}")
        if count > max_elements:
            raise InvalidXml(f"plus de {max_elements} éléments")
        stack.extend((child, depth + 1) for child in element)
    return root
