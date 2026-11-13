# SPDX-License-Identifier: EUPL-1.2
"""Correspondance (T3) et simulation intégrale (T4) de l'import, sur un classeur fabriqué qui reprend toutes les
formes relevées par l'audit du catalogue de la cliente (conception 19), sans aucune de ses données."""

from __future__ import annotations

import csv
import io

import openpyxl
import pytest

from biblyo.modules.imports import profile as profiles
from biblyo.modules.imports import reader, report, simulate

from .. import import_samples as samples


def _run(*, enriched: bool = True, sources: bool | str = True, existing=None, profile=None, rows=None):
    main = reader.read(samples.workbook(rows=rows), "catalogue.xlsx")
    other = (
        reader.read(samples.workbook(enriched=True, sources=sources, rows=rows), "enrichi.xlsx") if enriched else None
    )
    sheet = main.sheets[0]
    chosen = profile or profiles.detect(sheet)
    if other is not None and profile is None:
        chosen = profiles.detect(other.sheets[0], chosen)
    return simulate.simulate(main, sheet.name, chosen, complement=other, complement_sheet="Feuil1", existing=existing)


def _by_inventory(sim, inventory):
    return next(r for r in sim.rows if r.inventory == str(inventory))


def _codes(row):
    return {m.code for m in row.messages}


# --- T3 : correspondance ----------------------------------------------------------------------------------------------
def test_the_thirteen_columns_and_the_two_added_ones_all_get_a_destination():
    main = reader.read(samples.workbook(enriched=True), "enrichi.xlsx").sheets[0]
    profile = profiles.detect(main)
    assert profile.columns == {
        "Titre": "series", "Tome": "volume", "Sous-Titre": "title", "Auteur": "contributor", "Auteur 2": "contributor",
        "Date d'acquisition": "acquisition", "Edition": "publisher", "Etat": "condition", "Cote": "call_number",
        "Numéro d'inventaire": "inventory", "Collection": "collection", "Auteur 3": "contributor",
        "Auteur 4": "contributor", "ISBN (référence à vérifier)": "isbn_proposed",
        "Année édition (à vérifier)": "year_proposed",
    }  # fmt: skip


def test_an_unknown_column_is_kept_never_ignored():
    suggestion = profiles.suggest("Rayonnage du grenier", ["Titre", "Rayonnage du grenier"])
    assert suggestion.field == profiles.KEEP and suggestion.confidence == "none"
    assert profiles.suggest("", []).field == profiles.KEEP
    assert profiles.suggest("Titre", ["Titre"]).field == "title"  # seul, c'est le titre du livre
    assert profiles.suggest("ISBN", ["ISBN"]).field == "isbn"
    assert profiles.suggest("Année", ["Année"]).field == "year"


def test_the_provisional_condition_and_collection_tables_are_suggested_not_imposed():
    profile = profiles.detect(reader.read(samples.workbook(), "c.xlsx").sheets[0])
    # Arbitrage du 13 novembre : aucune transformation ambiguë par défaut ; seul « usé » est un état de biblyO
    assert profile.condition_map == {
        "satisfaisant": "", "leg. usure": "", "usé": "worn", "nettoyable": "", "à revoir": "",
    }  # fmt: skip
    assert [profiles.recommend_condition(v) for v in ("satisfaisant", "leg. usure", "nettoyable")] == [
        "good", "fair", "fair",
    ]  # fmt: skip
    assert profile.collection_map["double albums"] == "mention"
    assert profile.collection_map["hs"] == "mention"
    assert profile.collection_map["Collection intégrale"] == "mention"
    assert profile.collection_map["Banque Mutuelle"] == "collection"
    assert profile.barcode == "deferred"  # D-L6-7 : inventaire gardé, code-barres attribué plus tard
    assert profile.roles == "unspecified"  # D-L6-4 : aucun rôle deviné


def test_a_saved_profile_is_a_stable_json_and_survives_a_round_trip():
    profile = profiles.detect(reader.read(samples.workbook(), "c.xlsx").sheets[0])
    again = profiles.Profile.from_json(profile.to_json())
    assert again.to_json() == profile.to_json() and again.sha256 == profile.sha256
    broken = profiles.Profile.from_json('{"columns": {"Titre": "n_importe_quoi"}, "roles": "x", "barcode": "y"}')
    assert broken.columns == {} and broken.roles == "unspecified" and broken.barcode == "deferred"
    assert profiles.Profile.from_json('{"barcode": "none"}').barcode == "deferred"  # profil d'avant PC2


def test_risky_conversions_are_announced_before_the_simulation():
    sheet = reader.read(samples.workbook(), "c.xlsx").sheets[0]
    volume = next(c for c in sheet.columns if c.label == "Tome")
    assert "gardés tels quels, sans numéro choisi" in " ".join(profiles.risky(sheet, volume, "volume"))


# --- T4 : simulation --------------------------------------------------------------------------------------------------
def test_totals_reconcile_rows_copies_and_books():
    sim = _run(enriched=False)
    t = sim.totals
    assert (t["rows_read"], t["rows_rejected"], t["copies"]) == (23, 0, 23)
    assert t["books"] == 17 and t["extra_copies"] == 6
    assert t["distribution"] == {1: 14, 2: 2, 5: 1}
    assert t["series"] == 12 and t["standalone_works"] == 1
    assert all(ok for _label, ok, _detail in sim.checks)


def test_every_copy_keeps_a_stable_source_identity_and_is_never_merged():
    sim = _run()
    ids = [r.source_id for r in sim.rows]
    assert len(set(ids)) == len(ids) == 23
    assert ids[0] == f"{sim.file_sha256[:12]}:Feuil1:2"
    assert len({r.copy for r in sim.rows}) == 23  # cinq exemplaires d'un même livre restent cinq
    recueil = _by_inventory(sim, 17)
    assert len(sim.books[recueil.book].rows) == 5 and "multiple_copies" in _codes(recueil)


def test_the_simulation_is_deterministic_and_its_plan_changes_with_the_profile():
    first, second = _run(), _run()
    assert first.plan_sha256 == second.plan_sha256
    profile = profiles.detect(reader.read(samples.workbook(), "c.xlsx").sheets[0])
    profile.roles = "first_author"
    assert _run(enriched=False, profile=profile).plan_sha256 != _run(enriched=False).plan_sha256


def test_inverted_articles_keep_the_exact_title_and_gain_a_search_form():
    sim = _run()
    marins = _by_inventory(sim, 1)
    assert marins.fields["series"] == "Marins (Les)"
    series = sim.series[marins.series]
    assert series.title == "Marins (Les)" and series.search_form == "Les Marins"
    assert simulate.natural_title("agent 99 (L')") == "L'agent 99"
    assert simulate.natural_title("histoire du phare (une)") == "une histoire du phare"
    assert sim.totals["inverted_titles"] == 5
    comete = _by_inventory(sim, 6)
    assert "title_complement" in _codes(comete)
    assert any(p.kind == "search_form" and p.value == "Le petit Comète" for p in comete.proposals)
    assert comete.fields["series"] == "Comète (Le petit)"


def test_an_atypical_volume_is_kept_and_no_number_is_chosen():
    sim = _run()
    double = _by_inventory(sim, 7)
    assert double.fields["volume"] == "20,21" and double.fields["volume_number"] is None
    assert "atypical_volume" in _codes(double)
    episodes = _by_inventory(sim, 8)
    assert episodes.fields["volume"] == "ep 1-4" and episodes.fields["volume_number"] is None


def test_names_are_kept_as_written_and_uncertain_forms_are_only_proposed():
    sim = _run()
    bobine = _by_inventory(sim, 9)
    assert [c.written for c in bobine.fields["contributors"]] == ["Soria, Carlos R.", "Eric & Sam"]
    assert {"name_inverted", "name_multiple"} <= _codes(bobine)
    assert {(p.kind, p.value) for p in bobine.proposals} >= {
        ("name_form", "Carlos R. Soria"), ("name_split", "Eric | Sam"),
    }  # fmt: skip
    bobard = _by_inventory(sim, 10)
    assert "name_organization" in _codes(bobard) and "name_variants" in _codes(bobard)
    assert "J.C. Martin" in sim.agents and "J-C Martin" in sim.agents  # jamais fusionnés d'office
    assert sim.totals["agents_written"] > sim.totals["agents_keys"]
    roles = [(c.role, c.to_specify) for c in bobine.fields["contributors"]]
    assert roles == [("other", True), ("other", True)]  # D-L6-4 : aucun rôle deviné, pas même « auteur »


def test_publishers_keep_their_label_and_are_compared_prudently():
    sim = _run()
    assert _by_inventory(sim, 1).fields["publisher"] == "DARGOT"
    assert _by_inventory(sim, 11).fields["publisher"] == "Dargot"
    dargot = sim.publishers["dargot"]
    assert dargot.forms == {"DARGOT": 3, "Dargot": 1} and dargot.usage_form == "Dargot"
    assert "publisher_variants" in _codes(_by_inventory(sim, 11))
    assert "publisher_close" in _codes(_by_inventory(sim, 9))  # WALT DISNEY et DISNEY
    assert "publisher_mention" in _codes(_by_inventory(sim, 12))  # ALBUM DOUBLE


def test_a_call_number_is_never_a_key():
    sim = _run()
    assert set(sim.shared_call_numbers) == {"BOB/01"}
    assert "shared_call_number" in _codes(_by_inventory(sim, 9))
    assert _by_inventory(sim, 9).book != _by_inventory(sim, 10).book
    assert "call_volume_mismatch" in _codes(_by_inventory(sim, 12))  # JAC/04, tome 8
    assert "call_volume_mismatch" not in _codes(_by_inventory(sim, 7))  # NAV/20,21 : pas de la forme série/numéro


def test_a_text_cell_formatted_as_a_date_is_reported_with_its_reference():
    sim = _run()
    row = _by_inventory(sim, 13)
    assert row.fields["call_number"] == "JUN/008"
    assert any(m.code == "date_format_cell" and "I14" in m.text for m in row.messages)
    assert sim.totals["date_cells"] == 1


def test_the_same_volume_with_two_titles_stays_two_works():
    sim = _run()
    one, two = _by_inventory(sim, 10), _by_inventory(sim, 11)
    assert one.work != two.work and one.series == two.series
    assert "same_volume_other_title" in _codes(one)


def test_the_complement_never_overrides_the_main_file():
    sim = _run()
    assert len(sim.differences) == 5
    assert {(d.inventory, d.column, d.main, d.complement) for d in sim.differences} == {
        ("15", "Auteur", "", "Rita Rive"), ("15", "Auteur 2", "", "Hugo Rive"), ("16", "Auteur", "", "Rita Rive"),
        ("16", "Auteur 2", "", "Hugo Rive"), ("16", "Edition", "", "RIVE"),
    }  # fmt: skip
    row = _by_inventory(sim, 16)
    assert row.fields["contributors"] == [] and row.fields["publisher"] == ""
    assert {(p.kind, p.value) for p in row.proposals} >= {("contributor", "Rita Rive"), ("publisher", "RIVE")}
    assert all(p.origin == "enrichi.xlsx" for p in row.proposals)


def test_isbn_and_years_to_check_are_proposals_with_their_source():
    sim = _run()
    first = _by_inventory(sim, 1)
    isbn = next(p for p in first.proposals if p.kind == "isbn")
    assert isbn.value == samples.ISBN_A and isbn.url == "https://exemple.invalid/1" and isbn.note == "à vérifier"
    assert sim.totals["proposals"]["isbn"] == 5 and sim.totals["proposals"]["publication_year"] == 3
    assert first.fields["isbn"] == ""  # jamais un fait


def test_contradictory_proposals_keep_editions_apart():
    sim = _run()
    one, two = _by_inventory(sim, 22), _by_inventory(sim, 23)
    assert one.book != two.book and one.work == two.work
    assert "edition_conflict" in _codes(one)
    assert _by_inventory(sim, 2).book == _by_inventory(sim, 3).book  # même ISBN proposé : une édition


def test_a_sources_sheet_that_disagrees_is_reported():
    sim = _run(sources="décalée")
    assert "source_mismatch" in _codes(_by_inventory(sim, 1))


def test_existing_inventory_numbers_block_their_rows_and_matches_are_only_probable():
    existing = simulate.Existing(inventory={"1"}, works={("marins", "2", "vent tourne"): 41}, isbns={samples.ISBN_C: 7})
    sim = _run(existing=existing)
    first = _by_inventory(sim, 1)
    assert first.action == "error" and "inventory_exists" in _codes(first) and not first.copy
    assert sim.totals["rows_rejected"] == 1 and all(ok for _l, ok, _d in sim.checks)
    assert "probable_match" in _codes(_by_inventory(sim, 2))
    assert "probable_match" in _codes(_by_inventory(sim, 23))
    assert any("probablement" in b for b in sim.blockers)


def test_barcodes_are_only_generated_on_request_and_without_conflict():
    sheet = reader.read(samples.workbook(), "c.xlsx").sheets[0]
    profile = profiles.detect(sheet)
    assert {r.fields["barcode"] for r in _run(enriched=False, profile=profile).rows} == {""}
    profile.barcode = "generate"
    sim = _run(enriched=False, profile=profile, existing=simulate.Existing(barcodes={"EX-000001", "EX-000003"}))
    codes = [r.fields["barcode"] for r in sim.rows]
    assert codes[:3] == ["EX-000004", "EX-000005", "EX-000006"] and len(set(codes)) == 23
    assert not any("Codes-barres" in b for b in sim.blockers)


def test_missing_and_duplicate_inventory_numbers_are_refused_not_dropped():
    rows = [list(r) for r in samples.ROWS[:4]]
    rows[1][9] = None
    rows[3][9] = 1
    sim = _run(enriched=False, rows=[tuple(r) for r in rows])
    assert [r.action for r in sim.rows] == ["error", "error", "create", "error"]
    assert sim.totals["rows_read"] == 4 and sim.totals["copies"] == 1
    assert all(ok for _l, ok, _d in sim.checks)


def test_every_filled_cell_keeps_its_raw_value_and_transformations_are_counted():
    sim = _run()
    by_letter = {c.letter: c for c in sim.columns if c.source == "main"}
    assert all(c.kept_raw == c.filled for c in sim.columns)
    assert by_letter["D"].transformed == 2  # « Jean Dupré  » et « \tAtelier Crayon » : espaces retirés
    assert {c.header for c in sim.columns if c.source == "complement"} == set(samples.EXTRA)


# --- Rapports ---------------------------------------------------------------------------------------------------------
def test_reports_never_turn_a_cell_into_a_formula():
    text_input = 'Titre;Sous-Titre;Auteur;Numéro d\'inventaire\r\nMarins;=HYPERLINK("http://x");+33 Dupré;1\r\n'
    source = reader.read(text_input.encode(), "c.csv")
    sim = simulate.simulate(source, "CSV", profiles.detect(source.sheets[0]))
    data = report.to_csv(sim)
    assert data.startswith("﻿".encode())
    text = data.decode("utf-8-sig")
    lines = list(csv.reader(io.StringIO(text), delimiter=";"))
    flat = [cell for line in lines for cell in line]
    assert '\'=HYPERLINK("http://x")' in flat and not any(c.startswith("=") for c in flat)
    book = openpyxl.load_workbook(io.BytesIO(report.to_xlsx(sim)))
    cells = [c for sheet in book.worksheets for row in sheet.iter_rows() for c in row]
    assert all(c.data_type != "f" for c in cells)
    assert any(c.value == '=HYPERLINK("http://x")' and c.data_type == "s" for c in cells)


def test_the_xlsx_report_has_every_view():
    book = openpyxl.load_workbook(io.BytesIO(report.to_xlsx(_run())), read_only=True)
    assert book.sheetnames == [
        "Synthèse", "Lignes", "Anomalies", "Livres", "Séries", "Cotes partagées", "Propositions", "Différences",
        "Colonnes", "Contributeurs", "Éditeurs", "Profil",
    ]  # fmt: skip
    lines = list(book["Lignes"].iter_rows(values_only=True))
    assert len(lines) == 24 and lines[1][0] == 2


# --- CSV en entrée ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("encoding", "delimiter"), [("utf-8-sig", ";"), ("utf-8", ","), ("cp1252", ";"), ("utf-8", "\t")]
)
def test_csv_files_are_read_whatever_their_encoding_and_delimiter(encoding, delimiter):
    text = delimiter.join(samples.HEADERS) + "\r\n"
    text += delimiter.join(
        ["Marins (Les)", "1", "La mer", "Jean Dupré", "", "inf. 2026", "DARGOT", "usé", "MAR/01", "1", "", "", ""]
    )
    sim_input = reader.read(text.encode(encoding), "catalogue.csv")
    sheet = sim_input.sheets[0]
    assert [c.label for c in sheet.columns] == list(samples.HEADERS)
    sim = simulate.simulate(sim_input, sheet.name, profiles.detect(sheet))
    assert sim.rows[0].fields["contributors"][0].written == "Jean Dupré"
    assert sim.rows[0].fields["condition"] == "worn"


def test_csv_cells_that_look_like_formulas_are_reported_and_kept_as_text():
    text = "Titre;Numéro d'inventaire\r\n=1+1;1\r\n@SOMME(A1);2\r\n"
    sheet = reader.read(text.encode(), "c.csv").sheets[0]
    assert sheet.formula_like_cells == {"A2", "A3"}
    assert any(n.code == "formula_like" for n in sheet.notices)
    sim = simulate.simulate(reader.read(text.encode(), "c.csv"), sheet.name, profiles.detect(sheet))
    assert sim.rows[0].fields["title"] == "=1+1" and "formula_like" in _codes(sim.rows[0])
