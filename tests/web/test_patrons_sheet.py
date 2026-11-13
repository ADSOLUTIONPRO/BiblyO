# SPDX-License-Identifier: EUPL-1.2
"""Fiche, inscription en deux temps, modification, cartes, coordonnées à la demande. Personnes inventées."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import AddressInput, GuardianInput, PersonInput, Registration

EMAIL = "sophie.moreau@example.org"
PHONE = "06 12 34 56 78"
E164 = "+33612345678"
STREET = "12 rue des Tilleuls"
NOTE = "Préfère être appelée le matin"
SECRETS = (EMAIL, E164, PHONE, STREET, NOTE, "03/05/1980")


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
def sophie(database) -> int:
    person = PersonInput(
        first_name="Sophie",
        last_name="Moreau",
        category="adult",
        birth="03/05/1980",
        email=EMAIL,
        phone=PHONE,
        address=AddressInput(line1=STREET, postal_code="54000", city="Nancy"),
        notes=NOTE,
    )
    return service.register(database, Registration(person=person), None)


@pytest.fixture
def lina(database, sophie) -> int:
    person = PersonInput(first_name="Lina", last_name="Moreau", category="minor", birth="2016")
    guardian = GuardianInput(number="P-000001", relation="parent", can_borrow_for=True, is_contact=True)
    return service.register(database, Registration(person=person, guardian=guardian), None)


def _form(**values) -> dict[str, str]:
    return {"etape": "verifier", "categorie": "adult", "prenom": "Marc", "nom": "Petit", **values}


# --- Fiche ------------------------------------------------------------------------------------------------------------
def test_sheet_shows_identity_and_cards(client_as, sophie):
    page = client_as("volunteer").get(f"/lecteurs/{sophie}").text
    assert "Sophie Moreau" in page and "P-000001" in page and "L-000001" in page and "En service" in page
    assert "Coordonnées réservées" in page


def test_unknown_sheet_is_404(client_as):
    assert client_as("volunteer").get("/lecteurs/999").status_code == 404


def test_sheet_is_refused_without_the_permission(client_as, sophie):
    assert client_as("viewer").get(f"/lecteurs/{sophie}").status_code == 403


@pytest.mark.parametrize("role", ["volunteer"])
def test_nothing_personal_in_the_sheet_of_a_volunteer(client_as, sophie, role):
    page = client_as(role).get(f"/lecteurs/{sophie}").text
    for secret in SECRETS:
        assert secret not in page, secret
    assert "1980" in page  # l'année, repère de désambiguïsation


def test_contacts_stay_masked_until_asked_for(client_as, settings, sophie):
    librarian = client_as("librarian")
    page = librarian.get(f"/lecteurs/{sophie}").text
    for secret in (EMAIL, E164, PHONE, STREET):
        assert secret not in page
    assert NOTE in page and "03/05/1980" in page  # la date complète et les observations, permises
    assert "Afficher les coordonnées" in page
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.contacts_shown'") == [(0,)]
    shown = librarian.post(f"/lecteurs/{sophie}/coordonnees/afficher", headers={"HX-Request": "true"})
    assert shown.status_code == 200
    assert EMAIL in shown.text and PHONE in shown.text and STREET in shown.text
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.contacts_shown'") == [(1,)]
    assert EMAIL not in librarian.get(f"/lecteurs/{sophie}/coordonnees/masquer").text


@pytest.mark.parametrize("role", ["volunteer", "viewer"])
def test_direct_access_to_contacts_is_forbidden(client_as, settings, sophie, role):
    browser = client_as(role)
    for call, path in (
        (browser.post, f"/lecteurs/{sophie}/coordonnees/afficher"),
        (browser.get, f"/lecteurs/{sophie}/coordonnees/masquer"),
        (browser.get, f"/lecteurs/{sophie}/coordonnees"),
        (browser.post, f"/lecteurs/{sophie}/coordonnees"),
    ):
        response = call(path)
        assert response.status_code == 403, path
        assert EMAIL not in response.text
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.contacts_shown'") == [(0,)]


def test_a_minor_is_reached_through_authorised_guardians_only(client_as, database, settings, sophie, lina):
    other = service.register(
        database,
        Registration(
            person=PersonInput(first_name="Marc", last_name="Roux", category="adult", email="marc.roux@example.org")
        ),
        None,
    )
    service.add_guardian(database, lina, other, service.Rights("other", False, False), None)
    shown = client_as("librarian").post(f"/lecteurs/{lina}/coordonnees/afficher").text
    assert EMAIL in shown and "marc.roux@example.org" not in shown


def test_minor_sheet_says_how_it_stands(client_as, database):
    person = PersonInput(first_name="Tom", last_name="Roux", category="minor")
    alone = service.register(database, Registration(person=person), None)
    page = client_as("volunteer").get(f"/lecteurs/{alone}").text
    assert "Responsable à renseigner" in page and "pas de coordonnées propres" in page


def test_guardian_rights_are_shown_link_by_link(client_as, sophie, lina):
    page = client_as("volunteer").get(f"/lecteurs/{lina}").text
    assert "Peut emprunter" in page and "À contacter" in page
    assert "Lina Moreau" in client_as("volunteer", "vol2").get(f"/lecteurs/{sophie}").text


# --- Inscription ------------------------------------------------------------------------------------------------------
def test_volunteer_registers_in_two_steps(client_as, settings):
    volunteer = client_as("volunteer")
    assert "Vérifier" in volunteer.get("/lecteurs/nouveau").text
    review = volunteer.post("/lecteurs/nouveau", data=_form(courriel="marc.petit@example.org"))
    assert (
        review.status_code == 200 and "marc.petit@example.org" in review.text and 'value="enregistrer"' in review.text
    )
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(0,)]  # vérifier n'écrit rien
    saved = volunteer.post("/lecteurs/nouveau", data=_form(etape="enregistrer", courriel="marc.petit@example.org"))
    assert saved.status_code == 303 and saved.headers["location"] == "/lecteurs/1"
    assert _rows(settings, "SELECT number, status FROM patrons") == [("P-000001", "active")]
    # après l'enregistrement, le bénévole revient à une fiche sans coordonnées
    assert "marc.petit@example.org" not in volunteer.get("/lecteurs/1").text


def test_the_options_only_announce_what_the_role_can_fill(client_as):
    assert "adhésion" not in client_as("volunteer", "vol").get("/lecteurs/nouveau").text.lower()
    assert "carte déjà imprimée, adhésion" in client_as("librarian", "bib").get("/lecteurs/nouveau").text


def test_correct_returns_to_the_form_with_the_values(client_as):
    page = client_as("volunteer").post("/lecteurs/nouveau", data=_form(etape="corriger")).text
    assert 'value="Marc"' in page and 'value="verifier"' in page


def test_errors_are_shown_and_nothing_is_written(client_as, settings):
    response = client_as("volunteer").post("/lecteurs/nouveau", data=_form(nom="", courriel="pas-un-courriel"))
    assert response.status_code == 400 and "Quelques points" in response.text
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(0,)]


def test_homonym_must_be_confirmed(client_as, settings, sophie):
    volunteer = client_as("volunteer")
    data = _form(prenom="Sophie", nom="Moreau")
    review = volunteer.post("/lecteurs/nouveau", data=data).text
    assert "déjà ce nom" in review and "P-000001" in review and "Nancy" in review
    for secret in (EMAIL, E164, STREET, NOTE):
        assert secret not in review
    refused = volunteer.post("/lecteurs/nouveau", data={**data, "etape": "enregistrer"})
    assert refused.status_code == 400 and "confirmez" in refused.text
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(1,)]
    saved = volunteer.post("/lecteurs/nouveau", data={**data, "etape": "enregistrer", "homonymes_ok": "1"})
    assert saved.status_code == 303
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(2,)]


def test_ambiguous_phone_is_never_guessed(client_as, settings):
    volunteer = client_as("volunteer")
    response = volunteer.post("/lecteurs/nouveau", data=_form(telephone="28 12 34"))
    assert response.status_code == 400 and "Quel numéro est le bon" in response.text
    assert "Nouvelle-Calédonie" in response.text
    review = volunteer.post("/lecteurs/nouveau", data=_form(telephone="28 12 34", telephone_territoire="caledonie"))
    assert "+687 28 12 34 · Nouvelle-Calédonie" in review.text  # le numéro compris, lisible, avant d'enregistrer
    saved = volunteer.post(
        "/lecteurs/nouveau",
        data=_form(etape="enregistrer", telephone="28 12 34", telephone_territoire="caledonie"),
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT value FROM contacts") == [("+687281234",)]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("0690 12 34 56", "+590690123456"), ("+44 20 7946 0958", "+442079460958"), ("0612345678", "+33612345678")],
)
def test_overseas_and_international_numbers(client_as, settings, typed, stored):
    response = client_as("volunteer").post("/lecteurs/nouveau", data=_form(etape="enregistrer", telephone=typed))
    assert response.status_code == 303
    assert _rows(settings, "SELECT value FROM contacts") == [(stored,)]


def test_minor_with_new_guardian_and_explicit_rights(client_as, settings):
    response = client_as("volunteer").post(
        "/lecteurs/nouveau",
        data=_form(
            etape="enregistrer",
            categorie="minor",
            prenom="Inès",
            nom="Garnier",
            naissance="2015",
            resp_prenom="Paul",
            resp_nom="Garnier",
            resp_telephone="0612345678",
            resp_relation="parent",
            resp_emprunte="1",
        ),
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT can_borrow_for, is_contact, relation FROM guardianships") == [(1, 0, "parent")]


def test_minor_contacts_are_dropped_and_guardian_picked_by_number(client_as, settings, sophie):
    response = client_as("volunteer").post(
        "/lecteurs/nouveau",
        data=_form(
            etape="enregistrer",
            categorie="minor",
            courriel="ne.pas.garder@example.org",
            resp_id=str(sophie),
            resp_contact="1",
        ),
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT count(*) FROM contacts WHERE value LIKE '%garder%'") == [(0,)]
    assert _rows(settings, "SELECT guardian_id, can_borrow_for, is_contact FROM guardianships") == [(sophie, 0, 1)]


def test_household_choice_new_and_existing(client_as, settings):
    volunteer = client_as("volunteer")
    assert (
        volunteer.post(
            "/lecteurs/nouveau",
            data=_form(etape="enregistrer", foyer_mode="nouveau", foyer_nom="Famille Petit", foyer_type="family"),
        ).status_code
        == 303
    )
    options = volunteer.post("/foyers/choix", data={"choix_q": "Petit"}).text
    assert 'data-label="Famille Petit"' in options and "Marc Petit" in options
    saved = volunteer.post(
        "/lecteurs/nouveau",
        data=_form(etape="enregistrer", prenom="Anne", foyer_mode="existant", foyer_id="1"),
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT household_id, patron_id FROM household_members ORDER BY patron_id") == [
        (1, 1),
        (1, 2),
    ]


def test_a_taken_card_is_refused_without_naming_its_holder(client_as, sophie):
    response = client_as("volunteer").post("/lecteurs/nouveau", data=_form(etape="enregistrer", carte="L-000001"))
    assert response.status_code == 400 and "déjà pris" in response.text and "Sophie" not in response.text


def test_volunteer_cannot_set_membership_or_notes(client_as, settings):
    client_as("volunteer").post(
        "/lecteurs/nouveau", data=_form(etape="enregistrer", adhesion="31/08/2027", observations="note")
    )
    assert _rows(settings, "SELECT membership_expires_on, notes FROM patrons") == [(None, None)]


def test_registering_requires_the_permission(client_as):
    assert client_as("viewer").get("/lecteurs/nouveau").status_code == 403
    assert client_as("viewer", "v2").post("/lecteurs/nouveau", data=_form(etape="enregistrer")).status_code == 403


# --- Modification -----------------------------------------------------------------------------------------------------
def test_volunteer_cannot_edit_after_registration(client_as, sophie):
    volunteer = client_as("volunteer")
    assert volunteer.get(f"/lecteurs/{sophie}/modifier").status_code == 403
    assert volunteer.post(f"/lecteurs/{sophie}/modifier", data={"prenom": "X", "nom": "Y"}).status_code == 403
    for action in ("majorite", "renouveler", "carte"):
        assert volunteer.post(f"/lecteurs/{sophie}/{action}").status_code == 403
    assert "/modifier" not in volunteer.get(f"/lecteurs/{sophie}").text


def test_librarian_edits_identity(client_as, settings, sophie):
    librarian = client_as("librarian")
    form = librarian.get(f"/lecteurs/{sophie}/modifier").text
    assert 'value="Sophie"' in form and NOTE in form
    saved = librarian.post(
        f"/lecteurs/{sophie}/modifier",
        data={
            "version": "1",
            "prenom": "Sophia",
            "nom": "Moreau",
            "naissance": "1980",
            "adhesion": "",
            "observations": "",
        },
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT first_name, birth, notes FROM patrons") == [("Sophia", "1980", None)]
    details = _rows(settings, "SELECT details FROM audit_log WHERE action = 'patron.update'")[0][0]
    assert "Sophia" not in details and "first_name" in details


def test_concurrent_modification_is_refused(client_as, settings, sophie):
    librarian = client_as("librarian")
    data = {"prenom": "A", "nom": "B", "naissance": "", "adhesion": "", "observations": ""}
    assert librarian.post(f"/lecteurs/{sophie}/modifier", data={**data, "version": "1"}).status_code == 303
    late = librarian.post(f"/lecteurs/{sophie}/modifier", data={**data, "prenom": "C", "version": "1"})
    assert late.status_code == 409 and "autre personne" in late.text
    assert _rows(settings, "SELECT first_name FROM patrons") == [("A",)]


def test_edit_without_personal_permission_keeps_hidden_values(client_as, settings, database, sophie):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO roles (code, label, is_system, created_at) VALUES ('sans_perso', 'Sans perso', 0, '2026-11-08')"
    )
    role = connection.execute("SELECT id FROM roles WHERE code = 'sans_perso'").fetchone()[0]
    for permission in ("patrons.view", "patrons.edit"):
        connection.execute("INSERT INTO role_permissions (role_id, permission) VALUES (?, ?)", (role, permission))
    connection.commit()
    connection.close()
    browser = client_as("sans_perso")
    page = browser.get(f"/lecteurs/{sophie}/modifier").text
    assert NOTE not in page and "03/05/1980" not in page
    saved = browser.post(f"/lecteurs/{sophie}/modifier", data={"version": "1", "prenom": "Sophia", "nom": "Moreau"})
    assert saved.status_code == 303
    assert _rows(settings, "SELECT first_name, birth, notes FROM patrons") == [("Sophia", "1980-05-03", NOTE)]


def test_contacts_edit_is_journaled_and_replaces(client_as, settings, sophie):
    librarian = client_as("librarian")
    form = librarian.get(f"/lecteurs/{sophie}/coordonnees").text
    assert EMAIL in form
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'patron.contacts_shown'") == [(1,)]
    saved = librarian.post(
        f"/lecteurs/{sophie}/coordonnees",
        data={"version": "1", "courriel": "autre@example.org", "telephone": "", "adresse_ligne1": ""},
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT value FROM contacts WHERE kind != 'address'") == [("autre@example.org",)]


def test_a_minor_has_no_contact_form(client_as, sophie, lina):
    assert client_as("librarian").get(f"/lecteurs/{lina}/coordonnees").status_code == 404


def test_majority_confirmed_keeps_links(client_as, settings, database):
    guardian = service.register(
        database, Registration(person=PersonInput(first_name="Paul", last_name="Roux", category="adult")), None
    )
    minor = service.register(
        database,
        Registration(
            person=PersonInput(first_name="Tom", last_name="Roux", category="minor", birth="2000"),
            guardian=GuardianInput(number="P-000001", can_borrow_for=True),
        ),
        None,
    )
    librarian = client_as("librarian")
    page = librarian.get(f"/lecteurs/{minor}").text
    assert "Majorité à confirmer" in page
    assert librarian.post(f"/lecteurs/{minor}/majorite", data={"version": "1"}).status_code == 303
    assert _rows(settings, "SELECT category FROM patrons WHERE id = ?", minor) == [("adult",)]
    assert _rows(settings, "SELECT guardian_id FROM guardianships") == [(guardian,)]


def test_membership_renewal(client_as, settings, sophie):
    librarian = client_as("librarian")
    assert (
        librarian.post(f"/lecteurs/{sophie}/renouveler", data={"version": "1", "adhesion": "31/08/2027"}).status_code
        == 303
    )
    assert _rows(settings, "SELECT membership_expires_on FROM patrons") == [("2027-08-31",)]
    assert "31/08/2027" in librarian.get(f"/lecteurs/{sophie}").text
    assert librarian.post(f"/lecteurs/{sophie}/renouveler", data={"adhesion": "bientôt"}).status_code == 400


def test_card_lost_then_replaced_and_old_number_refused(client_as, settings, sophie):
    librarian = client_as("librarian")
    assert librarian.post(f"/lecteurs/{sophie}/carte", data={"motif": "lost", "action": "remplacer"}).status_code == 303
    page = librarian.get(f"/lecteurs/{sophie}").text
    assert "L-000001" in page and "Perdue" in page and "L-000002" in page and "En service" in page
    refused = librarian.post(
        f"/lecteurs/{sophie}/carte", data={"motif": "lost", "action": "remplacer", "numero": "L-000001"}
    )
    assert refused.status_code == 400 and "déjà pris" in refused.text
    assert _rows(settings, "SELECT number, end_reason FROM patron_cards ORDER BY id") == [
        ("L-000001", "lost"),
        ("L-000002", None),
    ]


def test_card_withdrawn_then_a_new_one_given(client_as, settings, sophie):
    librarian = client_as("librarian")
    assert (
        librarian.post(f"/lecteurs/{sophie}/carte", data={"motif": "revoked", "action": "retirer"}).status_code == 303
    )
    page = librarian.get(f"/lecteurs/{sophie}").text
    assert "Sans carte" in page and "Remettre une carte" in page
    assert librarian.post(f"/lecteurs/{sophie}/carte", data={"action": "remplacer"}).status_code == 303
    assert _rows(settings, "SELECT number, end_reason FROM patron_cards ORDER BY id")[-1] == ("L-000002", None)


def test_old_card_is_not_a_page_address(client_as, sophie):
    assert client_as("volunteer").get("/lecteurs/L-000001").status_code == 404
