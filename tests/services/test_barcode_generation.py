# SPDX-License-Identifier: EUPL-1.2
"""Lot L8 : tous les codes manquants en une opération. Exemplaires importés sans code (comme ceux du Club), codes
saisis, retirés ou portés par une carte sautés, prêts en cours intacts, simulation sans écriture, opérations
simultanées, préfixe changé explicitement. Classeur fabriqué : aucune donnée du Club."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from biblyo.core.errors import BusinessError, FormErrors
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings import rules
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import CopyInput

from ..web import test_import_shelving as shelving
from ..web.test_circulation_counter import _adult, _form, _scan, _state

database = shelving.database  # fixtures partagées : base avec l'import activé, classeur fabriqué importé
imported = shelving.imported

MOTIVE = "attribution_globale"


def _rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _execute(settings, query: str, *params) -> None:
    connection = sqlite3.connect(settings.database_path)
    connection.execute(query, params)
    connection.commit()
    connection.close()


def _snapshot(settings):
    """Ce qui ne doit jamais bouger : inventaires, ISBN, prêts, éditions."""
    return (
        _rows(settings, "SELECT id, inventory_number, edition_id, call_number FROM copies ORDER BY id"),
        _rows(settings, "SELECT edition_id, scheme, value FROM edition_identifiers ORDER BY 1, 2, 3"),
        _rows(settings, "SELECT * FROM loans ORDER BY id"),
        _rows(settings, "SELECT id, work_id FROM editions ORDER BY id"),
    )


def test_la_simulation_n_ecrit_rien(settings, database, imported):
    before = _rows(settings, "SELECT * FROM copies ORDER BY id")
    sequence = _rows(settings, "SELECT * FROM sequences")
    plan = holdings.generation_plan(database)
    assert len(plan.copy_ids) == len(imported) == 23
    assert plan.first == "EX-000001" and plan.last == "EX-000023"
    assert _rows(settings, "SELECT * FROM copies ORDER BY id") == before
    assert _rows(settings, "SELECT * FROM sequences") == sequence
    assert _rows(settings, "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'barcodes.%'") == [(0,)]


def test_tous_les_codes_manquants_en_une_operation(settings, database, imported):
    kept = _snapshot(settings)
    with database.session() as session:
        totals = holdings.barcode_totals(session)
    assert (totals.copies, totals.with_code, totals.without_code) == (23, 0, 23)
    plan = holdings.generation_plan(database)
    given = holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)
    assert list(given) == list(plan.copy_ids) and list(given.values()) == list(plan.codes)
    # Dans l'ordre des numéros d'inventaire du Club (D-L6-7) : « 9 » avant « 10 ».
    order = [inventory for (inventory,) in _rows(settings, "SELECT inventory_number FROM copies ORDER BY barcode")]
    numeric = [int(value) for value in order if value.isdigit()]
    assert numeric == sorted(numeric)
    assert _snapshot(settings) == kept  # inventaires, ISBN, prêts, éditions intacts
    assert _rows(settings, "SELECT COUNT(DISTINCT barcode), COUNT(*) FROM copies") == [(23, 23)]
    assert _rows(settings, "SELECT last_value FROM sequences WHERE name = 'copy_barcode'") == [(23,)]
    journal = _rows(settings, "SELECT action, COUNT(*) FROM audit_log WHERE action IN ('copy.update', "
                              "'barcodes.assigned') GROUP BY action ORDER BY action")  # fmt: skip
    assert journal == [("barcodes.assigned", 1), ("copy.update", 23)]
    with database.session() as session:
        totals = holdings.barcode_totals(session)
    assert totals.without_code == 0
    with pytest.raises(BusinessError, match="Aucun exemplaire"):
        holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)


def test_codes_pris_retires_ou_de_carte_sautes(settings, database, imported):
    first, second = sorted(imported.values())[:2]
    holdings.update_copy(database, first, CopyInput(barcode="EX-000002", inventory_number=""), None, None)
    _execute(settings, "INSERT INTO retired_codes VALUES ('EX-000003', 'barcode', 'ancien', '2026-11-01')")
    _adult(database, card="EX-000004")
    plan = holdings.generation_plan(database)
    assert {"EX-000002", "EX-000003", "EX-000004"}.isdisjoint(plan.codes)
    assert plan.codes[:2] == ("EX-000001", "EX-000005")
    given = holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)
    assert first not in given and second in given
    assert _rows(settings, "SELECT barcode FROM copies WHERE id = ?", first) == [("EX-000002",)]


def test_retires_et_perdus_laisses_de_cote(settings, database, imported):
    ids = sorted(imported.values())
    _execute(settings, "UPDATE copies SET status = 'withdrawn', withdrawn_on = '2026-11-01', "
                       "withdrawal_reason = 'abîmé' WHERE id = ?", ids[0])  # fmt: skip
    _execute(settings, "UPDATE copies SET status = 'lost' WHERE id = ?", ids[1])
    with database.session() as session:
        totals = holdings.barcode_totals(session)
    assert totals.excluded == {"withdrawn": 1, "lost": 1}
    plan = holdings.generation_plan(database)
    assert ids[0] not in plan.copy_ids and ids[1] not in plan.copy_ids and len(plan.copy_ids) == 21
    # Une sélection explicite peut les inclure.
    chosen = holdings.generation_plan(database, [ids[0]])
    assert chosen.copy_ids == (ids[0],)


def test_une_selection_garde_les_codes_existants(settings, database, imported):
    ids = sorted(imported.values())[:3]
    holdings.update_copy(database, ids[0], CopyInput(barcode="MAIN-1", inventory_number=""), None, None)
    plan = holdings.generation_plan(database, ids)
    assert plan.kept == (ids[0],) and plan.copy_ids == tuple(ids[1:])
    given = holdings.assign_planned(database, ids, plan.fingerprint, None, motive="attribution_selection")
    assert set(given) == set(ids[1:])
    assert _rows(settings, "SELECT barcode FROM copies WHERE id = ?", ids[0]) == [("MAIN-1",)]


def test_rien_n_est_ecrit_si_la_situation_a_change(settings, database, imported):
    plan = holdings.generation_plan(database)
    copy_id = plan.copy_ids[5]
    inventory = _rows(settings, "SELECT inventory_number FROM copies WHERE id = ?", copy_id)[0][0]
    holdings.update_copy(database, copy_id, CopyInput(barcode="", inventory_number=inventory, notes="vu"), None, None)
    before = _rows(settings, "SELECT id, barcode, version FROM copies ORDER BY id")
    with pytest.raises(ConflictError):
        holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)
    assert _rows(settings, "SELECT id, barcode, version FROM copies ORDER BY id") == before
    # Un code attribué ailleurs entre-temps fait avancer la séquence : la simulation n'est plus juste.
    plan = holdings.generation_plan(database)
    work = _rows(settings, "SELECT work_id FROM editions LIMIT 1")[0][0]
    catalogue.add_copies(
        database, _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0], 1, "", None
    )
    with pytest.raises(ConflictError):
        holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)


def test_deux_attributions_simultanees(settings, database, imported):
    plan = holdings.generation_plan(database)
    outcomes: list[object] = []

    def run() -> None:
        try:
            outcomes.append(holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE))
        except BusinessError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert _rows(settings, "SELECT COUNT(DISTINCT barcode), COUNT(*) FROM copies") == [(23, 23)]
    assert _rows(settings, "SELECT last_value FROM sequences WHERE name = 'copy_barcode'") == [(23,)]


def test_un_pret_en_cours_n_est_pas_touche(client_as, settings, database, imported):
    copy_id = imported["9"]
    _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    _adult(database, card="CARTE-9")
    browser = client_as("volunteer")
    html = _scan(browser, _scan(browser, None, "CARTE-9"), "9")
    assert browser.post("/comptoir/valider", data=_form(_state(html))).status_code == 200
    loans = _rows(settings, "SELECT * FROM loans")
    plan = holdings.generation_plan(database)
    assert copy_id in plan.copy_ids  # prêté, il reçoit son code comme les autres
    holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)
    assert _rows(settings, "SELECT * FROM loans") == loans
    code = _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]
    back = browser.post("/comptoir/retours/scan", data={"code": code}, headers={"HX-Request": "true"})
    assert back.status_code == 200
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL") == [(0,)]


def test_plusieurs_milliers_d_exemplaires(settings, database, imported):
    edition = _rows(settings, "SELECT id FROM editions LIMIT 1")[0][0]
    connection = sqlite3.connect(settings.database_path)
    connection.executemany(
        "INSERT INTO copies (public_id, edition_id, barcode, status, created_at, updated_at, inventory_number, "
        "version) VALUES (?, ?, NULL, 'in_processing', '2026-11-18', '2026-11-18', ?, 1)",
        [(f"essai-{n}", edition, f"M{n}") for n in range(3000)],
    )
    connection.commit()
    connection.close()
    started = time.monotonic()
    plan = holdings.generation_plan(database)
    given = holdings.assign_planned(database, None, plan.fingerprint, None, motive=MOTIVE)
    elapsed = time.monotonic() - started
    assert len(given) == 3023 and len(set(given.values())) == 3023
    assert elapsed < 60, elapsed


def test_prefixe_change_explicitement(settings, database, imported):
    ids = sorted(imported.values())
    first = holdings.generation_plan(database, ids[:2])
    holdings.assign_planned(database, ids[:2], first.fingerprint, None, motive="attribution_selection")
    with pytest.raises(FormErrors, match="réservé"):
        holdings.change_barcode_prefix(database, "CSN-", "EX-", None)
    with pytest.raises(FormErrors, match="numéro de lecteur"):
        holdings.change_barcode_prefix(database, "P-", "EX-", None)
    with pytest.raises(FormErrors):
        holdings.change_barcode_prefix(database, "9X", "EX-", None)
    with pytest.raises(ConflictError):
        holdings.change_barcode_prefix(database, "CSN", "AUTRE", None)
    assert holdings.change_barcode_prefix(database, " csn ", "EX-", None) == "CSN000003"  # la numérotation continue
    assert _rows(settings, "SELECT barcode FROM copies WHERE barcode IS NOT NULL ORDER BY barcode") == [
        ("EX-000001",), ("EX-000002",)
    ]  # fmt: skip
    plan = holdings.generation_plan(database, ids[2:3])
    assert plan.codes == ("CSN000003",)
    entry = _rows(settings, "SELECT details FROM audit_log WHERE action = 'sequence.prefix'")
    assert '"avant": "EX-"' in entry[0][0] and '"apres": "CSN"' in entry[0][0]


def test_regles_du_prefixe() -> None:
    assert rules.prefix_problems("CSN", 6) == ([], [])
    refused, warned = rules.prefix_problems("EX-", 6)
    assert not refused and warned  # le tiret change de place sur un clavier américain
    assert rules.prefix_problems("CSN-", 6)[0]
    assert rules.prefix_problems("P", 6)[0]
    assert rules.prefix_problems("TROPLONGPREFIXE", 6)[0]
    assert rules.prefix_problems("é", 6)[0]
