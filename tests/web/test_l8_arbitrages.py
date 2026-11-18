# SPDX-License-Identifier: EUPL-1.2
"""Arbitrages L8 du 18 novembre 2026 : préfixe CSN préparé sans toucher aux codes existants, anciennes étiquettes
« CSN-… » jamais rapprochées des nouveaux codes, impression ouverte compte par compte (rôle d'appoint), génération
massive réservée à la gestion du catalogue, aucun scan sans compte connecté."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.holdings import repository as holdings_repo

from ..factories import make_notice, make_user, open_session_for
from .test_circulation_counter import _adult, _scan


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(query, params)
    connection.commit()
    connection.close()


def _copies(settings, database, count=1, title="Les Tuniques bleues"):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return ids


# --- 1. Préfixe CSN ---------------------------------------------------------------------------------------------------
def test_etat_des_codes_puis_prefixe_csn_sans_code_attribue(settings, database, run_cli):
    (copy_id,) = _copies(settings, database)
    _execute(settings, "UPDATE copies SET barcode = NULL, inventory_number = '354' WHERE id = ?", copy_id)
    state = run_cli(settings, "biblyo codes etat")
    assert state.exit_code == 0, state.output
    assert "Préfixe actuel : « EX- »" in state.output and "avec code-barres : 0" in state.output
    assert "Aucun code-barres attribué" in state.output
    simulated = run_cli(settings, "biblyo codes prefixe CSN")
    assert simulated.exit_code == 0 and "Simulation" in simulated.output
    assert _rows(settings, "SELECT prefix FROM sequences WHERE name = 'copy_barcode'") == [("EX-",)]
    assert run_cli(settings, "biblyo codes prefixe CSN-").exit_code != 0  # réservé à l'ancienne reconstruction
    applied = run_cli(settings, "biblyo codes prefixe CSN --appliquer")
    assert applied.exit_code == 0 and "CSN000002" in applied.output  # le n° 1 a servi à add_copies, puis retiré
    assert _rows(settings, "SELECT prefix FROM sequences WHERE name = 'copy_barcode'") == [("CSN",)]
    assert _rows(settings, "SELECT inventory_number FROM copies WHERE id = ?", copy_id) == [("354",)]
    entry = _rows(settings, "SELECT details FROM audit_log WHERE action = 'sequence.prefix'")
    assert '"apres": "CSN"' in entry[0][0]
    assert "CSN" in run_cli(settings, "biblyo codes prefixe CSN").output  # déjà en usage : rien à faire


def test_prefixe_csn_quand_des_codes_existent(settings, database, run_cli):
    first, second = _copies(settings, database, 2)
    before = _rows(settings, "SELECT id, barcode, inventory_number FROM copies ORDER BY id")
    state = run_cli(settings, "biblyo codes etat").output
    assert "codes « EX-… » : 2" in state and "garderait tels quels" in state
    applied = run_cli(settings, "biblyo codes prefixe CSN --appliquer")
    assert applied.exit_code == 0 and "Codes déjà attribués : 2, inchangés" in applied.output
    assert "CSN000003" in applied.output  # la numérotation continue : aucun numéro réemployé
    assert _rows(settings, "SELECT id, barcode, inventory_number FROM copies ORDER BY id") == before
    third = _copies(settings, database, 1, "Spirou")[0]
    assert _rows(settings, "SELECT barcode FROM copies WHERE id = ?", third) == [("CSN000003",)]
    assert first and second


def test_une_ancienne_etiquette_de_la_reconstruction_ne_designe_aucun_livre(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    _execute(settings, "UPDATE copies SET barcode = 'CSN000001' WHERE id = ?", copy_id)
    with database.session() as session:
        assert holdings_repo.find_copy_by_code(session, "CSN-000001") is None  # jamais rapprochée sans tiret
        assert holdings_repo.find_copy_by_code(session, "csn000001").id == copy_id
        assert holdings_repo.find_copy_by_code(session, "CSN 000001").id == copy_id
    _execute(settings, "UPDATE copies SET barcode = 'EX-000009' WHERE id = ?", copy_id)
    with database.session() as session:
        assert holdings_repo.find_copy_by_code(session, "EX000009").id == copy_id  # saisie sans tiret : reconnue
    _execute(settings, "UPDATE copies SET barcode = 'CSN000001' WHERE id = ?", copy_id)
    browser = client_as("librarian")
    found = browser.post("/scanner/identifier", data={"code": "CSN-000001"}, headers={"HX-Request": "true"}).text
    assert "ancienne reconstruction" in found and "Les Tuniques bleues" not in found
    _adult(database, card="CARTE-1")
    counter = _scan(browser, _scan(browser, None, "CARTE-1"), "CSN-000001")
    assert "ancienne reconstruction" in counter
    back = browser.post("/comptoir/retours/scan", data={"code": "CSN-000001"}, headers={"HX-Request": "true"}).text
    assert "ancienne reconstruction" in back
    trial = browser.post(
        "/scanner/douchette/essai", data={"essai": "CSN-000001", "suffixe": "enter", "intervalle": "9"},
        headers={"HX-Request": "true"},
    ).text  # fmt: skip
    assert "Code inconnu" in trial and "ancienne reconstruction" in trial


# --- 2. Permissions -------------------------------------------------------------------------------------------------
def test_generation_massive_reservee_a_la_gestion_du_catalogue(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    _execute(settings, "UPDATE copies SET barcode = NULL, inventory_number = '77' WHERE id = ?", copy_id)
    volunteer = client_as("volunteer")
    assert volunteer.get("/etiquettes/generer").status_code == 403
    assert volunteer.post("/etiquettes/generer", data={"empreinte": "0" * 32, "portee": "tous"}).status_code == 403
    assert volunteer.post("/etiquettes/selection/generer", data={"q": "Tuniques"}).status_code == 403
    assert volunteer.get(f"/emplacements/codes-barres?exemplaire={copy_id}").status_code == 403
    page = volunteer.get("/etiquettes").text
    assert "Voir la simulation" not in page and "Réservé aux comptes qui gèrent le catalogue" in page
    version = _rows(settings, "SELECT version FROM copies WHERE id = ?", copy_id)[0][0]
    one = volunteer.post(f"/etiquettes/exemplaire/{copy_id}/code", data={"version": str(version)})
    assert one.status_code == 303  # un livre depuis sa fiche : gestion des exemplaires, comme avant
    assert client_as("librarian").get("/etiquettes/generer").status_code == 200


def _client_with_roles(app, settings, roles: list[str]):
    from starlette.testclient import TestClient

    from biblyo.core.security.sessions import COOKIE_NAME

    user_id = make_user(settings, f"compte-{'-'.join(roles)}", roles[0])
    for code in roles[1:]:
        _execute(settings, "INSERT INTO user_roles (user_id, role_id) SELECT ?, id FROM roles WHERE code = ?",
                 user_id, code)  # fmt: skip
    raw, csrf_token = open_session_for(settings, user_id)
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set(COOKIE_NAME, raw)
    client.headers["X-CSRF-Token"] = csrf_token
    return client


def test_impression_ouverte_a_un_benevole_choisi_seulement(app, settings, database, client_as):
    printer = _rows(settings, "SELECT r.label, p.permission FROM roles r JOIN role_permissions p ON p.role_id = r.id "
                              "WHERE r.code = 'labels_printer'")  # fmt: skip
    assert printer == [("Impression des étiquettes", "labels.print")]
    volunteer_permissions = _rows(settings, "SELECT p.permission FROM roles r JOIN role_permissions p "
                                            "ON p.role_id = r.id WHERE r.code = 'volunteer'")  # fmt: skip
    assert ("labels.print",) not in volunteer_permissions  # le rôle « Bénévole » n'est pas élargi
    plain = client_as("volunteer")
    assert plain.get("/etiquettes/imprimer?code=avec").status_code == 403
    chosen = _client_with_roles(app, settings, ["volunteer", "labels_printer"])
    assert chosen.get("/etiquettes/imprimer?code=avec").status_code == 200
    assert chosen.get("/etiquettes/feuille-test.pdf").status_code == 200
    assert chosen.get("/etiquettes/generer").status_code == 403  # imprimer n'ouvre pas la génération massive
    printer_only = _client_with_roles(app, settings, ["viewer", "labels_printer"])
    assert printer_only.get("/etiquettes").status_code == 200
    assert printer_only.get("/etiquettes/selection").status_code == 200
    users = client_as("admin").get("/parametres/utilisateurs").text
    assert "Impression des étiquettes" in users and "Imprimer des étiquettes" in users


# --- 5. Un scan n'est pas une preuve d'identité ----------------------------------------------------------------------
def test_aucun_scan_sans_compte_connecte(client, anonymous_form, settings, database):
    _copies(settings, database)
    make_user(settings, "installe", "admin")  # une installation faite : la page de connexion s'affiche
    token = anonymous_form(client, "/connexion")
    for path, data in (("/scanner/identifier", {"code": "EX-000001"}), ("/comptoir/scan", {"code": "EX-000001"}),
                       ("/comptoir/retours/scan", {"code": "EX-000001"}),
                       ("/scanner/inventaire", {"code": "EX-000001"})):  # fmt: skip
        response = client.post(path, data={**data, "_csrf": token}, follow_redirects=False)
        assert response.status_code in (303, 401, 403), (path, response.status_code)
        assert "Les Tuniques bleues" not in response.text
    assert _rows(settings, "SELECT COUNT(*) FROM loans") == [(0,)]
