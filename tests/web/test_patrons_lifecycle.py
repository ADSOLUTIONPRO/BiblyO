# SPDX-License-Identifier: EUPL-1.2
"""Suspension, archivage, conservation, anonymisation, export, historique, « À traiter ». Personnes inventées."""

from __future__ import annotations

import json
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import AddressInput, GuardianInput, HouseholdChoice, PersonInput, Registration

from ..factories import PASSWORD

EMAIL = "elise.fabre@example.org"
STREET = "21 rue du Moulin"
NOTE = "Vient le samedi"


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


@pytest.fixture
def elise(database) -> int:
    person = PersonInput(
        first_name="Élise",
        last_name="Fabre",
        category="adult",
        birth="1982",
        email=EMAIL,
        phone="0612345678",
        address=AddressInput(line1=STREET, postal_code="54000", city="Nancy"),
        notes=NOTE,
    )
    choice = HouseholdChoice(new_name="Famille Fabre", kind="family")
    return service.register(database, Registration(person=person, household=choice), None)


@pytest.fixture
def noa(database, elise) -> int:
    person = PersonInput(first_name="Noa", last_name="Fabre", category="minor", birth="2016")
    guardian = GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True)
    return service.register(database, Registration(person=person, guardian=guardian), None)


def _confirm(**values) -> dict[str, str]:
    return {"numero": "P-000001", "mot_de_passe": PASSWORD, "motif": "request", "precision": "", **values}


# --- Statut -----------------------------------------------------------------------------------------------------------
def test_suspend_archive_reactivate(client_as, settings, elise):
    librarian = client_as("librarian")
    assert "Suspendre" in librarian.get(f"/lecteurs/{elise}").text
    assert librarian.post(f"/lecteurs/{elise}/statut", data={"action": "suspend", "version": "1"}).status_code == 303
    assert _rows(settings, "SELECT status FROM patrons") == [("suspended",)]
    assert librarian.post(f"/lecteurs/{elise}/statut", data={"action": "archive", "version": "2"}).status_code == 303
    assert _rows(settings, "SELECT status FROM patrons") == [("archived",)]
    assert _rows(settings, "SELECT count(*) FROM contacts") != [(0,)]  # archiver ne retire rien
    assert librarian.post(f"/lecteurs/{elise}/statut", data={"action": "reactivate", "version": "3"}).status_code == 303
    assert _rows(settings, "SELECT status FROM patrons") == [("active",)]


def test_status_conflict_and_unknown_action(client_as, elise):
    librarian = client_as("librarian")
    assert librarian.post(f"/lecteurs/{elise}/statut", data={"action": "suspend", "version": "9"}).status_code == 409
    assert librarian.post(f"/lecteurs/{elise}/statut", data={"action": "inconnue"}).status_code == 400


def test_actions_are_offered_by_permission(client_as, elise):
    volunteer = client_as("volunteer", "vol").get(f"/lecteurs/{elise}").text
    librarian = client_as("librarian", "bib").get(f"/lecteurs/{elise}").text
    admin = client_as("admin", "chef").get(f"/lecteurs/{elise}").text
    assert "Gestion de la fiche" not in volunteer
    assert "Suspendre" in librarian and "Exporter" in librarian and "Anonymiser" not in librarian
    assert "Anonymiser" in admin and "Mettre en conservation" in admin


# --- Conservation -----------------------------------------------------------------------------------------------------
def test_hold_blocks_anonymization_until_lifted(client_as, settings, elise):
    admin = client_as("admin")
    assert (
        admin.post(f"/lecteurs/{elise}/conservation", data={"motif_conservation": "Litige", "jusqu_au": ""}).status_code
        == 303
    )
    page = admin.get(f"/lecteurs/{elise}/anonymiser").text
    assert "Anonymisation impossible" in page and "Retapez" not in page
    refused = admin.post(f"/lecteurs/{elise}/anonymiser", data=_confirm())
    assert refused.status_code == 400 and "conservation" in refused.text
    assert _rows(settings, "SELECT status FROM patrons") == [("active",)]
    assert admin.post(f"/lecteurs/{elise}/conservation/1/lever").status_code == 303
    assert admin.post(f"/lecteurs/{elise}/anonymiser", data=_confirm()).status_code == 303


def test_hold_reason_is_reserved(client_as, database, elise):
    service.place_hold(database, elise, "Litige en cours", "", None)
    librarian = client_as("librarian", "bib").get(f"/lecteurs/{elise}").text
    assert "Litige en cours" not in librarian and "En conservation" in librarian
    volunteer = client_as("volunteer", "vol").get(f"/lecteurs/{elise}").text
    assert "Litige en cours" not in volunteer and "En conservation" not in volunteer
    assert "Litige en cours" in client_as("admin", "chef").get(f"/lecteurs/{elise}").text


def test_hold_needs_a_reason(client_as, elise):
    assert (
        client_as("admin").post(f"/lecteurs/{elise}/conservation", data={"motif_conservation": " "}).status_code == 400
    )


# --- Anonymisation ----------------------------------------------------------------------------------------------------
def test_screen_says_what_stays_without_counting_backups(client_as, database, elise):
    service.export(database, elise, None)
    page = client_as("admin").get(f"/lecteurs/{elise}/anonymiser").text
    assert "Sauvegardes antérieures" in page and "jusqu’à leur suppression" in page
    assert "1 export d’après le journal" in page
    assert "sauvegardes concernées" not in page
    assert "Identité" in page and "Liens avec un foyer" in page


@pytest.mark.parametrize(
    ("change", "field", "expected"),
    [
        ({"numero": "P-000009"}, "numero", "ne correspond pas"),
        ({"numero": "L-000001"}, "numero", "numéro de carte"),
        ({"mot_de_passe": "mauvais"}, "mot_de_passe", "Mot de passe incorrect"),
        ({"motif": ""}, "motif", "Choisissez un motif"),
        ({"motif": "other"}, "motif", "Précisez le motif"),
        ({"motif": "other", "precision": "Demande de Fabre"}, "motif", "ni nom"),
        ({"motif": "other", "precision": "tel 0612345678"}, "motif", "ni nom"),
        ({"motif": "other", "precision": "x@example.org"}, "motif", "ni nom"),
    ],
)
def test_invalid_confirmation_changes_nothing(client_as, settings, elise, change, field, expected):
    response = client_as("admin").post(f"/lecteurs/{elise}/anonymiser", data=_confirm(**change))
    assert response.status_code == 400 and expected in response.text
    assert _rows(settings, "SELECT status, first_name FROM patrons") == [("active", "Élise")]
    assert _rows(settings, "SELECT count(*) FROM contacts") == [(3,)]
    assert PASSWORD not in response.text


def test_anonymization_leaves_nothing_identifying_on_any_table(client_as, settings, database, elise, noa):
    service.place_hold(database, elise, "Motif ancien", "2020-01-01", None)  # échue : ne bloque plus, mais s'efface
    with database.session(write=True) as session:
        from biblyo.modules.patrons.models import PatronHold

        session.get(PatronHold, 1).placed_at = "2019-01-01"
    admin = client_as("admin")
    response = admin.post(
        f"/lecteurs/{elise}/anonymiser", data=_confirm(motif="other", precision="Départ de la commune")
    )
    assert response.status_code == 303
    connection = sqlite3.connect(settings.database_path)
    tables = ("patrons", "contacts", "patron_cards", "household_members", "guardianships", "patron_holds", "audit_log")
    dump = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}  # noqa: S608
    connection.close()
    elise_row = next(row for row in dump["patrons"] if row[0] == elise)
    text = " ".join(
        str(item)
        for table in tables
        for row in dump[table]
        for item in row
        if not (table == "patrons" and row[0] == noa)
    )
    for secret in ("Élise", "elise", EMAIL, "+33612345678", STREET, NOTE, "Motif ancien", "1982"):
        assert secret not in text, secret
    assert "anonymized" in elise_row and "Fabre" not in " ".join(str(item) for item in elise_row)
    assert dump["contacts"] == [] and dump["guardianships"] == []
    assert not [row for row in dump["household_members"] if row[1] == elise]
    assert [row[2] for row in dump["patron_cards"] if row[1] == "L-000001"] == [None]
    entry = json.loads(_rows(settings, "SELECT details FROM audit_log WHERE action = 'patron.anonymize'")[0][0])
    assert (
        entry["motif"] == "other"
        and entry["precision"] == "Départ de la commune"
        and entry["coordonnees_effacees"] == 3
    )
    page = admin.get(f"/lecteurs/{elise}").text
    assert (
        "Lecteur anonymisé" in page
        and "jusqu’à leur suppression" in page
        and "Fabre" not in page.replace("Famille Fabre", "")
    )
    assert admin.get(f"/lecteurs/{elise}/anonymiser").status_code == 404
    assert admin.get(f"/lecteurs/{elise}/modifier").status_code == 404


def test_emptied_household_loses_what_named_the_person(client_as, settings, database, elise):
    service.set_household_contacts(
        database,
        1,
        service.ContactsEdit(email="foyer@example.org", address=AddressInput(line1=STREET, city="Nancy")),
        None,
        None,
    )
    assert client_as("admin").post(f"/lecteurs/{elise}/anonymiser", data=_confirm()).status_code == 303
    assert _rows(settings, "SELECT name, notes FROM households") == [("Foyer anonymisé", None)]
    assert _rows(settings, "SELECT count(*) FROM contacts") == [(0,)]


def test_household_with_other_members_keeps_its_shared_data(client_as, settings, database, elise, noa):
    service.add_member(database, 1, noa, "child", None)
    service.set_household_contacts(database, 1, service.ContactsEdit(email="foyer@example.org"), None, None)
    assert client_as("admin").post(f"/lecteurs/{elise}/anonymiser", data=_confirm()).status_code == 303
    assert _rows(settings, "SELECT name FROM households") == [("Famille Fabre",)]
    assert _rows(settings, "SELECT value FROM contacts") == [("foyer@example.org",)]


def test_journal_no_longer_links_the_person_to_a_household_or_guardian(client_as, settings, database, elise, noa):
    admin = client_as("admin")
    assert admin.post(f"/lecteurs/{noa}/anonymiser", data=_confirm(numero="P-000002")).status_code == 303
    for (details,) in _rows(
        settings, "SELECT details FROM audit_log WHERE entity_type = 'patron' AND entity_id = ?", noa
    ):
        data = json.loads(details or "{}")
        assert data.get("foyer") is None and data.get("responsable") is None


def test_minor_who_loses_guardian_appears_to_handle(client_as, database, elise, noa):
    admin = client_as("admin")
    assert admin.post(f"/lecteurs/{elise}/anonymiser", data=_confirm()).status_code == 303
    page = admin.get("/lecteurs/a-traiter").text
    assert "Noa Fabre" in page


def test_anonymization_without_active_card(client_as, settings, database, elise):
    service.end_card(database, elise, "revoked", replace=False, user_id=None)
    assert client_as("admin").post(f"/lecteurs/{elise}/anonymiser", data=_confirm()).status_code == 303
    assert _rows(settings, "SELECT status FROM patrons") == [("anonymized",)]


# --- Export -----------------------------------------------------------------------------------------------------------
def test_export_requires_the_permission(client_as, settings, elise):
    for role in ("volunteer", "viewer"):
        browser = client_as(role, f"c-{role}")
        assert browser.get(f"/lecteurs/{elise}/export").status_code == 403
        assert browser.get(f"/lecteurs/{elise}/export.json").status_code == 403
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.export'") == [(0,)]


def test_export_json_is_named_by_number_and_never_cached(client_as, settings, elise):
    response = client_as("librarian").get(f"/lecteurs/{elise}/export.json")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename="P-000001.json"'
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["format"] == "biblyo.lecteur.v1" and data["lecteur"]["prenom"] == "Élise"
    assert any(item["valeur"] == EMAIL for item in data["coordonnees"])
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.export'") == [(1,)]


def test_export_of_a_minor_names_guardians_without_their_contacts(client_as, elise, noa):
    data = client_as("librarian").get(f"/lecteurs/{noa}/export.json").json()
    assert data["responsables"][0]["nom"] == "Élise Fabre"
    assert EMAIL not in json.dumps(data, ensure_ascii=False) and "0612345678" not in json.dumps(data)


def test_export_page_is_printable_and_journaled(client_as, settings, elise):
    page = client_as("librarian").get(f"/lecteurs/{elise}/export").text
    assert EMAIL in page and "Imprimer" in page and f"/lecteurs/{elise}/export.json" in page
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.export'") == [(1,)]


def test_export_of_an_unknown_reader_is_404(client_as):
    assert client_as("librarian").get("/lecteurs/999/export.json").status_code == 404


# --- Historique et à traiter ------------------------------------------------------------------------------------------
def test_history_says_who_and_which_fields_never_the_values(client_as, elise):
    librarian = client_as("librarian", "bib")
    librarian.post(
        f"/lecteurs/{elise}/modifier",
        data={"version": "1", "prenom": "Élisa", "nom": "Fabre", "naissance": "1982", "observations": NOTE},
    )
    page = client_as("volunteer", "vol").get(f"/lecteurs/{elise}").text
    assert "Fiche modifiée" in page and "prénom" in page and "Inscription" in page
    section = page[page.index("titre-historique") :]
    assert "Élise" not in section


def test_to_handle_requires_edit_and_lists_each_case(client_as, database, elise):
    service.renew_membership(database, elise, "01/01/2020", None, None)
    teen = service.register(
        database,
        Registration(person=PersonInput(first_name="Tim", last_name="Roy", category="minor", birth="2000")),
        None,
    )
    alone = service.register(
        database,
        Registration(person=PersonInput(first_name="Ana", last_name="Roy", category="minor", birth="2018")),
        None,
    )
    service.create_household(database, "family", "Famille Vide", "", None)
    assert client_as("volunteer", "vol").get("/lecteurs/a-traiter").status_code == 403
    page = client_as("librarian", "bib").get("/lecteurs/a-traiter").text
    assert "Majorités à confirmer" in page and f'href="/lecteurs/{teen}"' in page
    assert f'href="/lecteurs/{alone}"' in page and "Élise Fabre" in page and "Famille Vide" in page
    assert "En conservation" not in page  # réservé à qui peut poser une conservation
    assert "En conservation" in client_as("admin", "chef").get("/lecteurs/a-traiter").text


def test_to_handle_counts_all_but_lists_the_first(client_as, database):
    for index in range(55):
        service.register(
            database,
            Registration(
                person=PersonInput(first_name=f"Enfant{index:02d}", last_name="Roy", category="minor", birth="2018")
            ),
            None,
        )
    page = client_as("librarian").get("/lecteurs/a-traiter").text
    assert page.count('href="/lecteurs/') == 50 and "50 premières fiches sur 55" in page
