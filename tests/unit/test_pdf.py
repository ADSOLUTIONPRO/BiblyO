# SPDX-License-Identifier: EUPL-1.2
"""Moteur PDF : A4 réel, impression sans mise à l'échelle, table des références juste, textes mesurés et codés."""

from __future__ import annotations

import io

from PIL import Image

from biblyo.core.pdf import A4, Document, encode_text, fit_text, text_width, wrap_text
from tests import pdf_reader


def test_page_a4_au_millimetre() -> None:
    document = Document("Essai")
    page = document.add_page()
    page.rect(10, 20, 30, 40)
    data = document.render()
    assert data.startswith(b"%PDF-1.7\n") and data.endswith(b"%%EOF\n")
    assert b"/MediaBox [0 0 595.2756 841.8898]" in data
    assert b"/PrintScaling /None" in data
    pdf_reader.check_xref(data)
    (read,) = pdf_reader.pages(data)
    assert (round(read.width, 3), round(read.height, 3)) == A4


def test_rendu_identique_pour_un_meme_contenu() -> None:
    def build() -> bytes:
        document = Document("Même")
        document.add_page().text(10, 10, 8, "Les Schtroumpfs")
        return document.render()

    assert build() == build()


def test_textes_en_winansi() -> None:
    assert encode_text("Œuvre « été » — 3 €") == "Œuvre « été » — 3 €".encode("cp1252")
    assert encode_text("Gdańsk") == b"Gdansk"  # « ń » n'existe pas en WinAnsi : l'accent tombe
    assert encode_text("漫画") == b"??"
    document = Document()
    document.add_page().text(5, 5, 8, "(Tintin) \\ 1")
    (read,) = pdf_reader.pages(document.render())
    assert read.texts[0][2] == "(Tintin) \\ 1"


def test_mesure_et_coupure() -> None:
    # « W » mesure 944 millièmes de corps en Helvetica : 10 points font 9,44 points, soit 3,33 mm.
    assert round(text_width("W", 10), 2) == 3.33
    assert text_width("Ab", 8, bold=True) > text_width("Ab", 8)
    long = "Les aventures extraordinaires d'Adèle Blanc-Sec"
    cut = fit_text(long, 7, 30)
    assert cut.endswith("…") and text_width(cut, 7) <= 30
    assert fit_text("Court", 7, 30) == "Court"
    lines = wrap_text(long, 7, 30, 2)
    assert len(lines) == 2 and all(text_width(line, 7) <= 30 for line in lines)


def test_image_avec_transparence() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 20), (200, 30, 30, 128)).save(buffer, "PNG")
    document = Document()
    number = document.add_image(buffer.getvalue())
    document.add_page().image(number, 10, 10, 20, 10)
    data = document.render()
    assert b"/SMask" in data and b"/Im0 Do" not in data and b"/Im0" in data
    pdf_reader.check_xref(data)
