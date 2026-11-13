# SPDX-License-Identifier: EUPL-1.2
"""Application d'un import (lot L6, T5) : tout ou rien, plan confirmé exigé, aucun doublon, restauration possible.

Les classeurs sont fabriqués (`tests/import_samples.py`) : aucune donnée du Club."""

from __future__ import annotations

import io
import json
import sqlite3
import threading

import pytest
from openpyxl import load_workbook

from biblyo.core.backup import restore_backup, verify_backup
from biblyo.core.db import Database
from biblyo.modules.imports import apply as importer
from biblyo.modules.imports import guard, reader, simulate
from biblyo.modules.imports import profile as profiles

from .. import import_samples as samples

CATALOGUE = ("works", "editions", "copies", "series", "agents", "publishers", "collections", "work_contributors")
TRACES = ("import_batches", "import_rows", "import_links", "import_proposals", "edition_isbn_candidates")


def _counts(settings, tables=CATALOGUE + TRACES) -> dict[str, int]:
    connection = sqlite3.connect(settings.database_path)
    try:
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608
    finally:
        connection.close()


def _query(settings, sql: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _request(database, *, enriched=True, main_bytes=None, profile=None, expected=None) -> importer.ApplyRequest:
    main = reader.read(main_bytes or samples.workbook(), "catalogue.xlsx")
    other = reader.read(samples.workbook(enriched=True), "enrichi.xlsx") if enriched else None
    chosen = profile or profiles.detect(main.sheets[0])
    if other is not None and profile is None:
        chosen = profiles.detect(other.sheets[0], chosen)
    with database.session() as session:
        sim = simulate.simulate(
            main, "Feuil1", chosen, complement=other, complement_sheet="Feuil1", existing=simulate.snapshot(session)
        )
        session.rollback()
    return importer.ApplyRequest(
        workbook=main,
        sheet="Feuil1",
        profile=chosen,
        expected_plan=expected or sim.plan_sha256,
        user_id=None,
        complement=other,
        complement_sheet="Feuil1" if other else "",
    )


def _enable(settings, value: str = "true") -> None:
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, '2026-11-13T00:00:00Z')",
        (importer.ENABLED_SETTING, value),
    )
    connection.commit()
    connection.close()


@pytest.fixture
def database(settings):
    _enable(settings)
    db = Database(settings.database_path)
    yield db
    db.dispose()


def test_applying_is_disabled_on_an_instance_until_an_administrator_enables_it(settings, database):
    _enable(settings, "false")
    with pytest.raises(importer.ApplyRefused, match="désactivée"):
        importer.apply(database, settings, _request(database))
    assert _counts(settings, TRACES) == dict.fromkeys(TRACES, 0)
    assert list(settings.backup_dir.iterdir()) == []  # refusé avant même la sauvegarde


def test_an_import_creates_exactly_the_plan_and_keeps_every_trace(settings, database):
    request = _request(database)
    before = _counts(settings)
    result = importer.apply(database, settings, request)
    after = _counts(settings)
    created = result.summary["created"]
    assert after["copies"] - before["copies"] == 23 == created["copies"]
    assert after["editions"] - before["editions"] == created["editions"] == result.summary["totals"]["books"]
    assert after["works"] - before["works"] == created["works"]
    assert after["import_rows"] == 23 and after["import_batches"] == 1
    assert all(line["ok"] for line in result.summary["reconciliation"])
    # Identité : inventaire d'origine à l'identique, aucun code-barres inventé (D-L6-7), statut « en préparation »
    copies = _query(settings, "SELECT inventory_number, barcode, status, location_id FROM copies ORDER BY id")[-23:]
    assert [c[0] for c in copies] == [str(n) for n in range(1, 24)]
    assert (
        {c[1] for c in copies} == {None}
        and {c[2] for c in copies} == {"in_processing"}
        and {c[3] for c in copies} == {None}
    )
    # D-L6-11 : affiché dans l'ordre naturel, la forme d'origine reste dans la ligne source
    assert _query(settings, "SELECT title FROM series WHERE title LIKE '%Marins%'") == [("Les Marins",)]
    raw = json.loads(_query(settings, "SELECT raw FROM import_rows WHERE row_number = 2")[0][0])
    assert raw["Titre"] == "Marins (Les)" and raw["Numéro d'inventaire"] == 1
    # D-L6-3 : l'état d'origine reste en précision ; une valeur non rangée n'a pas d'état et devient une proposition
    revoir = _query(settings, "SELECT condition, condition_note FROM copies WHERE inventory_number = '14'")[0]
    assert revoir == (None, "à revoir")
    assert _query(
        settings, "SELECT COUNT(*) FROM import_proposals WHERE field = 'condition' AND value = 'à revoir'"
    ) == [(1,)]
    # D-L6-2 et D-L6-4 : propositions jamais appliquées, aucun rôle deviné
    assert _query(settings, "SELECT COUNT(*) FROM edition_identifiers") == [(0,)]
    assert _query(settings, "SELECT DISTINCT status FROM edition_isbn_candidates") == [("proposed",)]
    assert _query(settings, "SELECT DISTINCT role FROM work_contributors") == [("other",)]
    assert _query(settings, "SELECT COUNT(*) FROM editions WHERE publication_year IS NOT NULL") == [(0,)]
    # Tome atypique gardé sans numéro choisi, noms gardés tels qu'écrits
    assert _query(settings, "SELECT display_name FROM agents WHERE display_name LIKE 'Soria%'") == [
        ("Soria, Carlos R.",)
    ]
    # Journal, sauvegarde vérifiée avant l'écriture
    assert verify_backup(settings.backup_dir / result.backup_name)["counts"]["copies"] == before["copies"]
    assert _query(settings, "SELECT action FROM audit_log WHERE action LIKE 'import.%'") == [("import.applied",)]


def test_every_copy_keeps_its_link_to_its_row_and_the_values_the_import_wrote(settings, database):
    importer.apply(database, settings, _request(database))
    rows = _query(
        settings,
        "SELECT r.row_number, c.inventory_number, r.normalized FROM import_rows r"
        " JOIN import_links l ON l.row_id = r.id AND l.entity_type = 'copy' JOIN copies c ON c.id = l.entity_id"
        " ORDER BY r.row_number",
    )
    assert [(n, inv) for n, inv, _s in rows] == [(n + 1, str(n)) for n in range(1, 24)]
    stored = json.loads(rows[14][2])  # inventaire 15 : le fichier complémentaire y ajoute des noms, sans le dire
    assert stored["complement"]["values"]["Auteur"] == "Rita Rive"
    assert stored["copy"]["inventory"] == "15"


def test_a_second_import_of_the_same_file_is_refused_and_writes_nothing(settings, database):
    importer.apply(database, settings, _request(database))
    before = _counts(settings)
    with pytest.raises(importer.ApplyRefused):
        importer.apply(database, settings, _request(database))
    assert _counts(settings) == before


def test_the_same_copies_from_another_file_are_refused_by_their_inventory(settings, database):
    importer.apply(database, settings, _request(database))
    rows = [tuple(r) for r in samples.ROWS]
    rows[0] = ("Marins (Les)", 1, "Autre sous-titre", *rows[0][3:])  # autre fichier, mêmes inventaires
    before = _counts(settings)
    request = _request(database, enriched=False, main_bytes=samples.workbook(rows=rows))
    with pytest.raises(importer.ApplyRefused) as refused:
        importer.apply(database, settings, request)
    assert "refusée" in " ".join(refused.value.details)
    assert _counts(settings) == before


def test_a_plan_that_changed_since_it_was_read_is_refused(settings, database):
    stale = _request(database, expected="0" * 64)
    with pytest.raises(importer.ApplyRefused, match="plan a changé"):
        importer.apply(database, settings, stale)
    request = _request(database)
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO publishers (name, name_norm, created_at) VALUES ('Nouveau', 'nouveau', '2026-11-13T00:00:00Z')"
    )
    connection.commit()
    connection.close()
    with pytest.raises(importer.ApplyRefused, match="plan a changé"):  # le catalogue a changé entre-temps
        importer.apply(database, settings, request)
    request.profile.roles = "first_author"  # le profil a changé après la lecture
    with pytest.raises(importer.ApplyRefused, match="plan a changé"):
        importer.apply(database, settings, request)
    assert _counts(settings, TRACES) == dict.fromkeys(TRACES, 0)


@pytest.mark.parametrize("stage", ["publishers", "works", "editions", "row", "commit"])
def test_a_failure_in_the_middle_leaves_nothing_behind_and_is_recorded(settings, database, stage):
    before = _counts(settings)

    def cut(name: str, done: int) -> None:
        if name == stage and (name != "row" or done == 12):
            raise ConnectionResetError("coupure simulée")

    with pytest.raises(importer.ApplyFailed) as failed:
        importer.apply(database, settings, _request(database), checkpoint=cut)
    after = _counts(settings)
    assert {t: after[t] for t in CATALOGUE} == {t: before[t] for t in CATALOGUE}
    assert after["import_rows"] == after["import_links"] == after["import_proposals"] == 0
    assert _query(settings, "SELECT status FROM import_batches") == [("failed",)]
    assert failed.value.batch_id is not None
    assert _query(settings, "SELECT action FROM audit_log WHERE action LIKE 'import.%'") == [("import.failed",)]
    result = importer.apply(database, settings, _request(database))  # l'échec n'empêche pas une nouvelle tentative
    assert result.summary["created"]["copies"] == 23


def test_two_confirmations_at_once_apply_only_one(settings, database):
    request = _request(database)
    started, release = threading.Event(), threading.Event()
    outcomes: list[str] = []

    def slow(name: str, _done: int) -> None:
        if name == "publishers":
            started.set()
            release.wait(10)

    def first() -> None:
        importer.apply(database, settings, request, checkpoint=slow)
        outcomes.append("appliqué")

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(10)
    with pytest.raises(importer.ApplyRefused, match="déjà en cours"):
        importer.apply(database, settings, _request(database, expected=request.expected_plan))
    release.set()
    thread.join(20)
    assert outcomes == ["appliqué"]
    assert _counts(settings)["import_batches"] == 1


def test_the_database_itself_refuses_a_second_applied_batch_of_a_file(settings, database):
    importer.apply(database, settings, _request(database))
    sha = _query(settings, "SELECT file_sha256 FROM import_batches")[0][0]
    connection = sqlite3.connect(settings.database_path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO import_batches (target, filename, file_sha256, file_bytes, mapping, schema_version, status,"
            " created_at) VALUES ('catalogue', 'x.xlsx', ?, 1, '{}', '0012', 'applying', 'x')",
            (sha,),
        )
    connection.close()


def test_existing_records_are_joined_never_rewritten(settings, database):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO publishers (name, name_norm, created_at) VALUES ('Dargot', 'dargot', '2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()
    before = _query(settings, "SELECT * FROM publishers")
    result = importer.apply(database, settings, _request(database))
    assert _query(settings, "SELECT * FROM publishers WHERE name_norm = 'dargot'") == before
    assert result.summary["joined"]["publishers"] == 1
    linked = _query(
        settings,
        "SELECT DISTINCT l.created FROM import_links l JOIN publishers p ON p.id = l.entity_id"
        " WHERE l.entity_type = 'publisher' AND p.name_norm = 'dargot'",
    )
    assert linked == [(0,)]


def test_the_backup_taken_before_the_import_restores_the_previous_state(settings, database):
    before = _counts(settings)
    result = importer.apply(database, settings, _request(database))
    assert _counts(settings)["copies"] == before["copies"] + 23
    database.dispose()
    restore_backup(settings, settings.backup_dir / result.backup_name)
    assert _counts(settings) == before


def test_the_legacy_reconstruction_is_refused_at_every_step(settings, database):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Exemplaires 926"
    sheet.append(["Inventaire", "Code-barres futur", "Titre", "Cote ouvrage", "Ligne Excel"])
    sheet.append([1, "CSN-000001", "Un titre", "ABC/01", 7])
    out = io.BytesIO()
    book.save(out)
    workbook = reader.read(out.getvalue(), "Classement_Complet.xlsx")
    assert guard.refusal(workbook)
    assert {"colonne « Ligne Excel » (Exemplaires 926)", "1 code(s) « CSN-… » (Exemplaires 926)"} <= set(
        guard.legacy_reconstruction(workbook)
    )
    profile = profiles.detect(workbook.sheets[0])
    sim = simulate.simulate(workbook, "Exemplaires 926", profile)
    assert any("reconstruction de l’ancienne application" in b for b in sim.blockers)
    request = importer.ApplyRequest(workbook, "Exemplaires 926", profile, sim.plan_sha256, None)
    with pytest.raises(importer.ApplyRefused):
        importer.apply(database, settings, request)
    assert _counts(settings, TRACES) == dict.fromkeys(TRACES, 0)


def test_an_artificial_code_is_never_taken_as_a_barcode():
    rows = [tuple(r) for r in samples.ROWS[:2]]
    main = reader.read(samples.workbook(rows=rows), "c.xlsx")
    profile = profiles.detect(main.sheets[0])
    profile.columns["Cote"] = "barcode"  # par erreur
    profile.barcode = "column"
    book = load_workbook(io.BytesIO(samples.workbook(rows=rows)))
    book.active["I2"] = "CSN-000042"
    out = io.BytesIO()
    book.save(out)
    sim = simulate.simulate(reader.read(out.getvalue(), "c.xlsx"), "Feuil1", profile)
    assert "barcode_artificial" in {m.code for m in sim.rows[0].messages} and sim.rows[0].action == "error"


def test_an_imported_copy_stays_editable_with_its_short_inventory_and_no_barcode(settings, database):
    from biblyo.core.errors import FormErrors
    from biblyo.modules.holdings import service as holdings

    importer.apply(database, settings, _request(database))
    copy_id, version = _query(settings, "SELECT id, version FROM copies WHERE inventory_number = '9'")[0]
    holdings.update_copy(
        database, copy_id, holdings.CopyInput(barcode="", inventory_number="9", call_number="BOB/01"), version, None
    )
    assert _query(settings, "SELECT barcode, inventory_number, call_number FROM copies WHERE id = ?", copy_id) == [
        (None, "9", "BOB/01")
    ]
    version = _query(settings, "SELECT version FROM copies WHERE id = ?", copy_id)[0][0]
    holdings.update_copy(  # attribution ultérieure d'un code scannable : l'inventaire ne bouge pas (D-L6-7)
        database,
        copy_id,
        holdings.CopyInput(barcode="LAB-0009", inventory_number="9", call_number="BOB/01"),
        version,
        None,
    )
    assert _query(settings, "SELECT barcode, inventory_number FROM copies WHERE id = ?", copy_id) == [("LAB-0009", "9")]
    version = _query(settings, "SELECT version FROM copies WHERE id = ?", copy_id)[0][0]
    with pytest.raises(FormErrors):  # un code-barres attribué ne s'efface pas
        holdings.update_copy(
            database, copy_id, holdings.CopyInput(barcode="", inventory_number="9", call_number="BOB/01"), version, None
        )


def _book(rows: list[list[object]], headers: list[str]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "Feuil1"
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


VARIANTS = _book(
    [
        ["Rivages", 1, "Le port", "Ana Mer", "DUPONT", "MER/01", 1, "L'écho des falaises", "Salle 2 · étagère B"],
        ["Rivages", 2, "La digue", "Ana Mer", "DUPONT", "MER/02", 2, "L'echo des falaises", ""],
    ],
    ["Titre", "Tome", "Sous-Titre", "Auteur", "Edition", "Cote", "Numéro d'inventaire", "Collection", "Emplacement"],
)


def test_collection_variants_block_until_a_form_is_chosen_and_keep_both_raw_values(settings, database):
    main = reader.read(VARIANTS, "variantes.xlsx")
    profile = profiles.detect(main.sheets[0])
    sim = simulate.simulate(main, "Feuil1", profile)
    assert any("collections écrites autrement" in b for b in sim.blockers)
    assert {m.code for r in sim.rows for m in r.messages} >= {"collection_variants"}
    profile.collection_canonical = {
        "L'écho des falaises": "L'écho des falaises",
        "L'echo des falaises": "L'écho des falaises",
    }
    with database.session() as session:
        sim = simulate.simulate(main, "Feuil1", profile, existing=simulate.snapshot(session))
        session.rollback()
    assert not sim.blockers and {m.code for r in sim.rows for m in r.messages} >= {"collection_canonical"}
    importer.apply(database, settings, importer.ApplyRequest(main, "Feuil1", profile, sim.plan_sha256, None))
    assert _query(settings, "SELECT name FROM collections") == [("L'écho des falaises",)]
    raws = [json.loads(r[0])["Collection"] for r in _query(settings, "SELECT raw FROM import_rows ORDER BY row_number")]
    assert raws == ["L'écho des falaises", "L'echo des falaises"]  # les deux valeurs d'origine restent


def test_a_location_column_is_only_a_proposal_never_a_shelving(settings, database):
    main = reader.read(VARIANTS, "variantes.xlsx")
    profile = profiles.detect(main.sheets[0])
    assert profile.columns["Emplacement"] == "location_hint"
    profile.collection_map = {"L'écho des falaises": "note", "L'echo des falaises": "note"}
    with database.session() as session:
        sim = simulate.simulate(main, "Feuil1", profile, existing=simulate.snapshot(session))
        session.rollback()
    importer.apply(database, settings, importer.ApplyRequest(main, "Feuil1", profile, sim.plan_sha256, None))
    assert _query(settings, "SELECT DISTINCT location_id, status FROM copies") == [(None, "in_processing")]
    assert _query(settings, "SELECT field, value FROM import_proposals WHERE value LIKE 'Salle%'") == [
        ("other", "Salle 2 · étagère B")
    ]
