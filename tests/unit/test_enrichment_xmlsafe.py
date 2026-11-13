# SPDX-License-Identifier: EUPL-1.2
"""Lecture XML durcie : DOCTYPE, entités, profondeur, nombre d'éléments, taille, réponses vides ou tronquées."""

from __future__ import annotations

import pytest

from biblyo.modules.enrichment.connectors.xmlsafe import InvalidXml, parse
from tests.sources import read_bytes


def test_recorded_responses_parse():
    root = parse(read_bytes("bnf/isbn-exact-petit-prince"))
    assert root.tag.endswith("searchRetrieveResponse")
    assert parse(read_bytes("sudoc/notice-par-ppn")).tag == "record"


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]><x>&e;</x>',
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
        b'<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><x>&b;</x>',
    ],
)
def test_doctype_and_entities_are_refused(body):
    with pytest.raises(InvalidXml, match="refusée"):
        parse(body)


@pytest.mark.parametrize(
    "body", [b"", b"   ", b"<x>", b"<x></y>", b"pas du xml", read_bytes("bnf/isbn-exact-petit-prince")[:3000]]
)
def test_malformed_or_truncated_xml_is_invalid(body):
    with pytest.raises(InvalidXml):
        parse(body)


def test_depth_is_bounded():
    body = b"<a>" * 40 + b"</a>" * 40
    with pytest.raises(InvalidXml, match="profondeur"):
        parse(body)
    assert parse(b"<a>" * 10 + b"</a>" * 10) is not None


def test_element_count_is_bounded():
    body = b"<r>" + b"<e/>" * 500 + b"</r>"
    with pytest.raises(InvalidXml, match="éléments"):
        parse(body, max_elements=100)


def test_size_is_bounded():
    with pytest.raises(InvalidXml, match="octets"):
        parse(b"<r>" + b"<e/>" * 1000 + b"</r>", max_bytes=1000)


def test_a_page_of_html_is_not_a_valid_response():
    with pytest.raises(InvalidXml):
        parse(b"<!doctype html><html><body><p>Erreur</p></body></html>")
