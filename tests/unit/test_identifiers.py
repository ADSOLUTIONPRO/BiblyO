# SPDX-License-Identifier: EUPL-1.2
"""Forme d'un code lu : ISBN, EAN, code interne, et douchette réglée sur le mauvais clavier."""

from __future__ import annotations

import pytest

from biblyo.core.identifiers import clean, from_qwerty, shape


def test_nettoyage_du_suffixe_et_des_espaces() -> None:
    assert clean("ex-000001\r\n") == "EX-000001"
    assert clean(" EX 000001\t") == "EX000001"
    assert clean(None) == ""


@pytest.mark.parametrize(
    ("raw", "isbn13"),
    [("9782800152851", "9782800152851"), ("978-2-8001-5285-1", "9782800152851"), ("2800152850", "9782800152851"),
     ("272340252X", None)],
)  # fmt: skip
def test_isbn(raw: str, isbn13: str | None) -> None:
    found = shape(raw)
    assert (found.isbn.isbn13 if found.isbn else None) == isbn13


def test_un_code_interne_n_est_jamais_un_isbn() -> None:
    for raw in ("EX-000001", "CSN000930", "P-000123", "354", "9"):
        found = shape(raw)
        assert found.isbn is None and found.ean is None
        assert found.internal


def test_ean_qui_n_est_pas_un_livre() -> None:
    found = shape("4006381333931")
    assert found.isbn is None and found.ean == "4006381333931" and found.is_book_number
    assert shape("012345678905").ean == "0012345678905"  # UPC-A sans son zéro initial


@pytest.mark.parametrize(
    ("typed", "meant"),
    [("EX)ààààà&", "EX-000001"), ("EX)àààçéç", "EX-000929"), ("CSNàààçè(", "CSN000975"),
     ("çè_éàéè&àà_(", "978202710085"), ("çè!éàéè&àà!(", "978202710085")],
)  # fmt: skip
def test_douchette_en_qwerty_sur_un_poste_azerty(typed: str, meant: str) -> None:
    assert from_qwerty(typed) == meant
    assert shape(typed).keyboard == meant


def test_pas_de_reparation_sans_signe_du_clavier() -> None:
    assert from_qwerty("EX-000001") is None
    assert from_qwerty("9782800152851") is None
    assert from_qwerty("&&&{}") is None  # après relecture, ce n'est toujours pas un code
