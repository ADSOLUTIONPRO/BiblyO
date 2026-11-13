# SPDX-License-Identifier: EUPL-1.2
"""Lecture sûre des fichiers à importer (L6, T2). Fichiers fabriqués ici, qui imitent les formes relevées par l'audit
du catalogue de la cliente (conception 19) sans en reprendre aucune ligne."""

from __future__ import annotations

import io
import zipfile

import openpyxl
import pytest

from biblyo.modules.imports import reader

HEADERS = ("Titre", "Tome", "Sous-Titre", "Auteur", "Date d'acquisition", "Edition", "Cote", "Numéro d'inventaire")


def xlsx(build) -> bytes:
    book = openpyxl.Workbook()
    build(book)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def catalogue(book) -> None:
    ws = book.active
    ws.title = "Feuil1"
    ws.append(HEADERS)
    ws.append(("Schtroumpfs (Les)", 1, "Les Schtroumpfs noirs", "Peyo ", "inf. 2026", "DUPUIS", "SCH/01", 1))
    ws.append(("Tintin", 20.21, "Vol 714 pour Sydney", "Hergé", "inf. 2026", "CASTERMAN", "TIN/20,21", 2))
    ws.append(("Série d'essai", "ep 1-4", None, None, " ", None, "ESS/1-4", 3))
    ws["G4"].number_format = "mmm-yy"  # une cote restée du texte, au format date
    ws.append((" ",))  # lignes vides en fin de feuille, qui existent dans le XML (une espace seule)
    ws.append((None, None, ""))


def test_a_catalogue_is_read_as_is_with_types_and_exact_headers():
    data = xlsx(catalogue)
    book = reader.read(data, "catalogue.xlsx")
    assert book.kind == "xlsx" and book.size == len(data) and len(book.sha256) == 64
    sheet = book.sheet("Feuil1")
    assert sheet is not None and sheet.header_row == 1
    assert [c.header for c in sheet.columns] == list(HEADERS)
    assert [r.number for r in sheet.rows] == [2, 3, 4]
    first, second, third = sheet.rows
    assert first.values[3] == "Peyo " and first.values[1] == 1  # espaces et types gardés : rien n'est corrigé
    assert second.values[1] == 20.21 and isinstance(second.values[7], int)
    assert third.values[1] == "ep 1-4" and third.values[4] == " " and third.values[5] is None
    codes = {n.code: n for n in sheet.notices}
    assert codes["date_format"].cells == ("G4",)
    assert sheet.trailing_empty_rows == 2 and "trailing_empty" in codes


def test_the_header_row_is_found_below_a_title():
    def build(book):
        ws = book.active
        ws.append(("Catalogue de la bibliothèque",))
        ws.append(())
        ws.append(("Titre", "Auteur"))
        ws.append(("Astérix le Gaulois", "René Goscinny"))

    sheet = reader.read(xlsx(build), "f.xlsx").sheets[0]
    assert sheet.header_row == 3 and [r.number for r in sheet.rows] == [4]


def test_empty_and_duplicate_headers_are_reported():
    def build(book):
        ws = book.active
        ws.append(("Titre", None, "titre"))
        ws.append(("A", "B", "C"))

    sheet = reader.read(xlsx(build), "f.xlsx").sheets[0]
    assert [c.label for c in sheet.columns] == ["Titre", "Colonne B", "titre"]
    assert {n.code for n in sheet.notices} >= {"empty_header", "duplicate_header"}


def test_formulas_merged_cells_and_hidden_sheets_are_reported():
    def build(book):
        ws = book.active
        ws.append(("Titre", "Tome"))
        ws.append(("Lucky Luke", "=1+1"))
        ws.merge_cells("A3:B3")
        ws["A3"] = "Fusionnée"
        hidden = book.create_sheet("Brouillon")
        hidden.sheet_state = "hidden"
        hidden.append(("Titre", "Note"))

    book = reader.read(xlsx(build), "f.xlsx")
    codes = {n.code: n for n in book.sheets[0].notices}
    assert codes["formula"].cells == ("B2",) and codes["merged"].cells == ("A3:B3",)
    assert book.sheet("Brouillon").hidden and "hidden" in {n.code for n in book.sheet("Brouillon").notices}


@pytest.mark.parametrize(
    ("data", "name", "message"),
    [
        (b"", "f.xlsx", "vide"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "f.xls", "Ancien format Excel"),
        (b"%PDF-1.4 ...", "f.pdf", "ni un classeur"),
        (b"PK\x03\x04 abime", "f.xlsx", "abîmé"),
    ],
)
def test_what_is_not_a_catalogue_is_refused_with_a_clear_message(data, name, message):
    with pytest.raises(reader.ReadError, match=message):
        reader.read(data, name)


def test_a_file_over_the_limit_is_refused_before_reading():
    with pytest.raises(reader.ReadError, match="dépasse 1 Mio"):
        reader.read(b"x" * (1024 * 1024 + 1), "f.csv", max_bytes=1024 * 1024)


def _with_entry(data: bytes, name: str, content: bytes, *, replace: bool = False) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            if replace and item.filename == name:
                continue
            target.writestr(item, source.read(item.filename))
        target.writestr(name, content)
    return out.getvalue()


def test_macros_are_refused():
    data = _with_entry(xlsx(catalogue), "xl/vbaProject.bin", b"\0" * 64)
    with pytest.raises(reader.ReadError, match="macros"):
        reader.read(data, "f.xlsm")


def test_a_zip_that_is_not_a_workbook_is_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("document.txt", "bonjour")
    with pytest.raises(reader.ReadError, match="pas un classeur"):
        reader.read(out.getvalue(), "f.xlsx")


def test_an_abnormally_compressed_archive_is_refused():
    data = _with_entry(xlsx(catalogue), "xl/media/vide.bin", b"\0" * (20 * 1024 * 1024))
    with pytest.raises(reader.ReadError, match="anormalement compressé"):
        reader.read(data, "f.xlsx")


def _causes(exc: BaseException):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def _sheet_xml(cells: str, doctype: str = "") -> bytes:
    return (
        f'<?xml version="1.0"?>{doctype}'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        f'<row r="1">{cells}<c r="B1" t="inlineStr"><is><t>B</t></is></c></row></sheetData></worksheet>'
    ).encode()


def test_external_xml_entities_are_refused_by_defusedxml(tmp_path):
    from defusedxml import EntitiesForbidden

    secret = tmp_path / "secret.txt"
    secret.write_text("NE PAS LIRE")
    cell = '<c r="A1" t="inlineStr"><is><t>&e;</t></is></c>'
    evil = _sheet_xml(cell, f'<!DOCTYPE x [<!ENTITY e SYSTEM "file://{secret}">]>')
    with pytest.raises(reader.ReadError) as refused:
        reader.read(_with_entry(xlsx(catalogue), "xl/worksheets/sheet1.xml", evil, replace=True), "f.xlsx")
    assert any(isinstance(cause, EntitiesForbidden) for cause in _causes(refused.value))
    # contre-épreuve : la même feuille, sans déclaration d'entité, se lit
    sane = _sheet_xml('<c r="A1" t="inlineStr"><is><t>sain</t></is></c>')
    book = reader.read(_with_entry(xlsx(catalogue), "xl/worksheets/sheet1.xml", sane, replace=True), "f.xlsx")
    assert [c.header for c in book.sheets[0].columns] == ["sain", "B"]


def test_too_many_cells_are_refused():
    with pytest.raises(reader.ReadError, match="trop de cellules"):
        reader.read(xlsx(catalogue), "f.xlsx", max_cells=10)


def test_a_csv_in_windows_1252_with_semicolons():
    text = "Titre;Tome;Edition\r\nAstérix;1;DARGAUD\r\nGaston;;DUPUIS\r\n;;\r\n"
    sheet = reader.read(text.encode("cp1252"), "catalogue.csv").sheets[0]
    assert [c.header for c in sheet.columns] == ["Titre", "Tome", "Edition"]
    assert [r.values for r in sheet.rows] == [("Astérix", "1", "DARGAUD"), ("Gaston", None, "DUPUIS")]
    assert sheet.trailing_empty_rows == 1


def test_a_csv_in_utf8_with_commas_and_quotes():
    text = '﻿Titre,Auteur\n"Chick Bill, le shérif",Tibet\n'
    sheet = reader.read(text.encode("utf-8"), "f.csv").sheets[0]
    assert sheet.rows[0].values == ("Chick Bill, le shérif", "Tibet")


def test_column_letters():
    assert [reader.column_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
