# SPDX-License-Identifier: EUPL-1.2
"""Planches et étiquettes : gabarits qui tiennent sur l'A4, cases occupées sautées, codes relus au bon endroit."""

from __future__ import annotations

import pytest

from biblyo.core.errors import FormErrors
from biblyo.modules.labels import layout, render, sheets
from biblyo.modules.labels.layout import Appearance, LabelData
from tests import pdf_reader

ALL = frozenset(layout.ELEMENTS)


def _items(count: int, start: int = 1) -> list[LabelData]:
    return [
        LabelData(f"EX-{n:06d}", f"Les aventures de Tintin, tome {n}", "Les aventures de Tintin", str(n),
                  f"BD HER {n}", str(n + 100), "Salle BD / Meuble 1", copy_id=n)
        for n in range(start, start + count)
    ]  # fmt: skip


@pytest.mark.parametrize("sheet", sheets.KNOWN, ids=lambda s: s.code)
def test_gabarits_connus(sheet: sheets.Sheet) -> None:
    assert sheet.problems() == []
    assert sheet.sources and sheet.verification in sheets.VERIFICATION_LABELS
    assert sheet.count in (21, 24, 40, 65)
    last_x, last_y = sheet.slot(sheet.count)
    assert last_x + sheet.width <= 210 and last_y + sheet.height <= 297
    # Planches centrées sur la page, à un demi-millimètre près (L7159 : marge de gauche d'une seule source).
    assert abs(sheet.top - (297 - last_y - sheet.height)) < 0.05
    if sheet.code != "avery-l7159":
        assert abs(sheet.left - (210 - last_x - sheet.width)) < 0.05


@pytest.mark.parametrize("sheet", sheets.KNOWN, ids=lambda s: s.code)
@pytest.mark.parametrize("model", ["compact", "standard", "detaille"])
def test_chaque_code_relu_dans_sa_case(sheet: sheets.Sheet, model: str) -> None:
    look = Appearance(layout.MODELS[model][1], "Bibliothèque des Tilleuls", False)
    items = _items(sheet.count + 3)
    plan = layout.plan(items, look, sheet, occupied=frozenset({1, 2, 3}))
    data = render.to_pdf(plan, title="Essai")
    pdf_reader.check_xref(data)
    read = pdf_reader.pages(data)
    assert len(read) == 2
    codes = [code for page in read for code in page.codes]
    assert [code.text for code in codes] == [item.code for item in items]
    for placed, code in zip([p for page in plan.pages for p in page], codes, strict=True):
        assert placed.data.code == code.text
        assert placed.x + sheets.CUT_TOLERANCE <= code.x - 10 * code.module + 1e-6
        assert code.x + code.width + 10 * code.module <= placed.x + sheet.width - sheets.CUT_TOLERANCE + 1e-6
        assert placed.y < code.y and code.y + code.height < placed.y + sheet.height
        assert code.height >= layout.MIN_BAR_HEIGHT - 1e-6
        assert code.module >= 0.254 - 1e-6
    assert plan.pages[0][0].position == 4  # les trois premières cases sont déjà utilisées


def test_calibration_deplace_tout_le_contenu() -> None:
    sheet = sheets.KNOWN_BY_CODE["avery-l7160"]
    look = Appearance(frozenset({"numero"}))
    base = pdf_reader.pages(render.to_pdf(layout.plan(_items(1), look, sheet), title=""))[0].codes[0]
    moved = pdf_reader.pages(render.to_pdf(layout.plan(_items(1), look, sheet, offset=(1.5, -0.8)), title=""))[0]
    assert round(moved.codes[0].x - base.x, 3) == 1.5 and round(moved.codes[0].y - base.y, 3) == -0.8


def test_elements_effaces_quand_la_place_manque() -> None:
    small = sheets.custom_sheet(sheets.CustomInput("5", "13", "38,1", "14", "10", "4,75", "2,5", "1"))
    drawn = layout.layout(_items(1)[0], Appearance(ALL, "Club", True), small)
    assert "emplacement" in drawn.dropped and drawn.bars[0].height >= layout.MIN_BAR_HEIGHT


def test_un_code_trop_long_pour_l_etiquette_est_refuse() -> None:
    narrow = sheets.custom_sheet(sheets.CustomInput("5", "10", "25", "20", "10", "5", "2", "2"))
    with pytest.raises(layout.LayoutError):
        layout.layout(LabelData("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), Appearance(frozenset()), narrow)


def test_feuille_entierement_occupee() -> None:
    sheet = sheets.KNOWN_BY_CODE["avery-l7160"]
    with pytest.raises(layout.LayoutError):
        layout.plan(_items(1), Appearance(frozenset()), sheet, occupied=frozenset(range(1, 22)))


def test_gabarit_personnalise() -> None:
    sheet = sheets.custom_sheet(sheets.CustomInput("3", "8", "70", "36", "4,5", "0", "0", "0"))
    assert sheet.count == 24 and sheet.problems() == []
    with pytest.raises(FormErrors):
        sheets.custom_sheet(sheets.CustomInput("3", "8", "70", "37", "4,5", "1", "0", "0"))  # dépasse la feuille
    with pytest.raises(FormErrors):
        sheets.custom_sheet(sheets.CustomInput("3", "8", "abc", "36", "4,5", "0", "0", "0"))
    assert sheets.from_data(sheets.as_data(sheet)) == sheet


def test_feuille_test() -> None:
    sheet = sheets.KNOWN_BY_CODE["avery-l7651"]
    (page,) = pdf_reader.pages(render.test_sheet_pdf(sheet))
    assert layout.TEST_CODE128 in [code.text for code in page.codes]
    assert any("100 mm" in text for _, _, text in page.texts)
    numbers = {text for _, _, text in page.texts if text.isdigit() and len(text) <= 2}
    assert numbers == {str(n) for n in range(1, 66)}


def test_apercu_svg_hachure_les_cases_utilisees() -> None:
    sheet = sheets.KNOWN_BY_CODE["avery-l7160"]
    plan = layout.plan(_items(2), Appearance(frozenset({"titre", "numero"})), sheet, occupied=frozenset({1, 2}))
    svg = str(render.sheet_svg(plan, 0, title="Feuille 1"))
    assert svg.count('class="lslot lslot-occupied"') == 2
    assert svg.count('class="lslot lslot-label"') == 2
    assert svg.count('class="lslot lslot-free"') == 17
    assert "EX-000001" in svg and 'viewBox="0 0 210 297"' in svg
    assert "<script" not in svg and "style=" not in svg


def test_le_svg_echappe_les_textes() -> None:
    sheet = sheets.KNOWN_BY_CODE["avery-l7160"]
    item = LabelData("EX-000001", "<b>Titre</b> & co")
    svg = str(render.sheet_svg(layout.plan([item], Appearance(frozenset({"titre"})), sheet), 0, title="<x>"))
    assert "<b>" not in svg and "&lt;b&gt;" in svg and "&lt;x&gt;" in svg
