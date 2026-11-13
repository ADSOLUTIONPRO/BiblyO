# SPDX-License-Identifier: EUPL-1.2
"""Familles, structures, référents, responsables : chaque droit est porté par un lien. Personnes inventées."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.patrons import repository as repo
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import AddressInput, ContactsEdit, GuardianInput, PersonInput, Registration, Rights

EMAIL = "foyer.martin@example.org"
STREET = "5 impasse des Lilas"


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


def _person(database, first, last, category="adult", **extra) -> int:
    person = PersonInput(first_name=first, last_name=last, category=category, **extra)
    return service.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _family(**values) -> dict[str, str]:
    return {
        "etape": "enregistrer",
        "nom_foyer": "Famille Martin",
        "a0_prenom": "Paul",
        "a0_nom": "Martin",
        "a0_telephone": "0612345678",
        "a0_relation": "parent",
        "a0_emprunte": "1",
        "a0_contact": "1",
        "a1_prenom": "Anne",
        "a1_nom": "Durand",
        "a1_relation": "legal_guardian",
        "e0_prenom": "Léa",
        "e0_nom": "Martin",
        "e0_naissance": "2016",
        "e1_prenom": "Tom",
        "e1_nom": "Martin",
        "e1_naissance": "2018",
        **values,
    }


@pytest.fixture
def family(database):
    paul, anne = _person(database, "Paul", "Martin"), _person(database, "Anne", "Durand", email="anne@example.org")
    lea = _person(database, "Léa", "Martin", "minor", birth="2016")
    home = service.create_household(database, "family", "Famille Martin", "", None)
    other = service.create_household(database, "family", "Famille Durand", "", None)
    service.add_member(database, home, paul, "responsible", None)
    service.add_member(database, home, lea, "child", None)
    service.add_member(database, other, anne, "responsible", None)
    service.add_member(database, other, lea, "child", None)
    service.set_household_contacts(
        database, home, ContactsEdit(email=EMAIL, address=AddressInput(line1=STREET, city="Nancy")), None, None
    )
    return {"paul": paul, "anne": anne, "lea": lea, "home": home, "other": other}


# --- Inscrire une famille ---------------------------------------------------------------------------------------------
def test_family_is_registered_in_one_step_with_explicit_links(client_as, settings):
    volunteer = client_as("volunteer")
    assert "Premier responsable" in volunteer.get("/foyers/nouveau").text
    data = _family()
    review = volunteer.post("/foyers/nouveau", data={**data, "etape": "verifier"})
    assert review.status_code == 200 and "Famille Martin" in review.text and "Léa Martin" in review.text
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(0,)]
    saved = volunteer.post("/foyers/nouveau", data=data)
    assert saved.status_code == 303 and saved.headers["location"] == "/foyers/1"
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(4,)]
    assert _rows(settings, "SELECT count(*) FROM patron_cards WHERE ended_on IS NULL") == [(4,)]
    links = _rows(
        settings,
        "SELECT g.first_name, m.first_name, relation, can_borrow_for, is_contact, contact_rank FROM guardianships l "
        "JOIN patrons g ON g.id = l.guardian_id JOIN patrons m ON m.id = l.minor_id ORDER BY 1, 2",
    )
    assert links == [
        ("Anne", "Léa", "legal_guardian", 0, 0, 2),
        ("Anne", "Tom", "legal_guardian", 0, 0, 2),
        ("Paul", "Léa", "parent", 1, 1, 1),
        ("Paul", "Tom", "parent", 1, 1, 1),
    ]


def test_family_with_an_error_writes_nothing(client_as, settings):
    response = client_as("volunteer").post("/foyers/nouveau", data=_family(e1_prenom="", a0_courriel="pas-valide"))
    assert response.status_code == 400 and "Quelques points" in response.text
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(0,)]
    assert _rows(settings, "SELECT count(*) FROM households") == [(0,)]


def test_optional_rows_stay_folded_unless_they_hold_something(client_as):
    volunteer = client_as("volunteer")
    empty = volunteer.get("/foyers/nouveau").text
    assert '<details class="card adv-card" open' not in empty and "Ajouter d’autres enfants" in empty
    data = {"etape": "verifier", "nom_foyer": "Famille Roy", "a1_prenom": "Anne", "e4_nom": "Roy"}
    refused = volunteer.post("/foyers/nouveau", data=data).text
    assert '<details class="card adv-card" open' in refused  # l'erreur du second responsable est visible
    assert '<details class="adv" open' in refused  # celle du cinquième enfant aussi


def test_empty_family_is_refused(client_as, settings):
    response = client_as("volunteer").post("/foyers/nouveau", data={"etape": "verifier", "nom_foyer": "Famille vide"})
    assert response.status_code == 400 and "au moins une personne" in response.text


def test_family_homonyms_must_be_confirmed(client_as, settings, database):
    _person(database, "Paul", "Martin")
    volunteer = client_as("volunteer")
    data = _family()
    refused = volunteer.post("/foyers/nouveau", data=data)
    assert refused.status_code == 400 and "confirmez" in refused.text
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(1,)]
    assert volunteer.post("/foyers/nouveau", data={**data, "homonymes_ok": "1"}).status_code == 303


def test_family_shared_contacts_and_ambiguous_phone(client_as, settings):
    volunteer = client_as("volunteer")
    refused = volunteer.post("/foyers/nouveau", data=_family(a0_telephone="28 12 34"))
    assert refused.status_code == 400 and "Quel numéro est le bon" in refused.text
    saved = volunteer.post(
        "/foyers/nouveau",
        data=_family(
            a0_telephone="28 12 34",
            a0_telephone_territoire="caledonie",
            foyer_courriel=EMAIL,
            adresse_ligne1=STREET,
            code_postal="54000",
            ville="Nancy",
        ),
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT kind FROM contacts WHERE household_id = 1 ORDER BY kind") == [
        ("address",),
        ("email",),
    ]
    assert _rows(settings, "SELECT value FROM contacts WHERE kind = 'phone'") == [("+687281234",)]


def test_family_requires_the_permission(client_as):
    assert client_as("viewer").get("/foyers/nouveau").status_code == 403
    assert client_as("viewer", "v2").post("/foyers/nouveau", data=_family()).status_code == 403


# --- Foyer ------------------------------------------------------------------------------------------------------------
def test_child_in_two_households_appears_in_both(client_as, family):
    browser = client_as("volunteer")
    for key in ("home", "other"):
        assert f'href="/lecteurs/{family["lea"]}"' in browser.get(f"/foyers/{family[key]}").text
    sheet = browser.get(f"/lecteurs/{family['lea']}").text
    assert "Famille Martin" in sheet and "Famille Durand" in sheet


def test_adults_who_answer_for_the_household_come_first(client_as, database, family):
    extra = _person(database, "Aline", "Zorn")
    service.add_member(database, family["home"], extra, "member", None)
    page = client_as("volunteer").get(f"/foyers/{family['home']}").text
    assert page.index("Paul Martin") < page.index("Aline Zorn") < page.index("Léa Martin")
    cards = client_as("volunteer", "v2").post("/lecteurs/recherche", data={"vue": "foyers", "q": "Paul"}).text
    assert cards.index("Paul Martin") < cards.index("Léa Martin")  # Paul n'est que dans la famille Martin


def test_membership_grants_no_right(database, family):
    with database.session() as session:
        assert repo.eligible_guardians(session, family["lea"]) == []
        assert not repo.can_borrow_for(session, family["paul"], family["lea"])


def test_guardians_with_different_rights(client_as, settings, database, family):
    service.add_guardian(database, family["lea"], family["paul"], Rights("parent", True, True, 1), None)
    service.add_guardian(database, family["lea"], family["anne"], Rights("parent", False, True, 2), None)
    with database.session() as session:
        # Paul peut emprunter mais n'a pas de moyen d'être joint en propre : le foyer le rend joignable.
        assert repo.eligible_guardians(session, family["lea"]) == [family["paul"]]
    page = client_as("volunteer").get(f"/lecteurs/{family['lea']}").text
    assert "Ne peut pas emprunter" in page and "Peut emprunter" in page


def test_contacts_of_a_household_are_masked_then_journaled(client_as, settings, family):
    volunteer = client_as("volunteer", "vol")
    librarian = client_as("librarian")
    for browser in (volunteer, librarian):
        page = browser.get(f"/foyers/{family['home']}").text
        assert EMAIL not in page and STREET not in page
    assert volunteer.post(f"/foyers/{family['home']}/coordonnees/afficher").status_code == 403
    assert volunteer.get(f"/foyers/{family['home']}/coordonnees").status_code == 403
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'household.contacts_shown'") == [(0,)]
    shown = librarian.post(f"/foyers/{family['home']}/coordonnees/afficher").text
    assert EMAIL in shown and STREET in shown
    assert _rows(settings, "SELECT count(*) FROM audit_log WHERE action = 'household.contacts_shown'") == [(1,)]


def test_shared_contacts_can_be_edited(client_as, settings, family):
    librarian = client_as("librarian")
    assert EMAIL in librarian.get(f"/foyers/{family['home']}/coordonnees").text
    saved = librarian.post(
        f"/foyers/{family['home']}/coordonnees", data={"foyer_courriel": "nouveau@example.org", "adresse_ligne1": ""}
    )
    assert saved.status_code == 303
    assert _rows(settings, "SELECT value FROM contacts WHERE household_id = ?", family["home"]) == [
        ("nouveau@example.org",)
    ]


def test_volunteer_cannot_change_a_household(client_as, family):
    volunteer = client_as("volunteer")
    home, lea = family["home"], family["lea"]
    for path in (
        f"/foyers/{home}/modifier",
        f"/foyers/{home}/membres",
        f"/foyers/{home}/membres/{lea}/role",
        f"/foyers/{home}/membres/{lea}/retirer",
        f"/foyers/{home}/archiver",
        f"/lecteurs/{lea}/foyers",
        f"/lecteurs/{lea}/foyers/{home}/retirer",
        f"/lecteurs/{lea}/responsables",
        f"/lecteurs/{lea}/responsables/{family['paul']}",
        f"/lecteurs/{lea}/responsables/{family['paul']}/retirer",
    ):
        assert volunteer.post(path).status_code == 403, path
    assert volunteer.get("/foyers/structure").status_code == 403


def test_members_are_added_changed_and_removed(client_as, settings, family):
    librarian = client_as("librarian")
    home = family["home"]
    assert (
        librarian.post(f"/foyers/{home}/membres", data={"membre_id": str(family["anne"]), "role": "member"}).status_code
        == 303
    )
    refused = librarian.post(f"/foyers/{home}/membres", data={"membre_id": str(family["anne"]), "role": "member"})
    assert refused.status_code == 400 and "déjà partie" in refused.text
    assert (
        librarian.post(f"/foyers/{home}/membres/{family['anne']}/role", data={"role": "responsible"}).status_code == 303
    )
    assert librarian.post(f"/foyers/{home}/membres/{family['anne']}/retirer").status_code == 303
    assert _rows(settings, "SELECT count(*) FROM patrons WHERE id = ?", family["anne"]) == [(1,)]  # la fiche reste
    assert _rows(settings, "SELECT count(*) FROM household_members WHERE patron_id = ?", family["anne"]) == [(1,)]


def test_household_rename_conflict(client_as, family):
    librarian = client_as("librarian")
    home = family["home"]
    assert librarian.post(f"/foyers/{home}/modifier", data={"nom": "Famille M.", "version": "1"}).status_code == 409


def test_reader_is_attached_from_the_sheet(client_as, settings, database, family):
    librarian = client_as("librarian")
    extra = service.create_household(database, "structure", "École Ferry", "", None)
    response = librarian.post(f"/lecteurs/{family['lea']}/foyers", data={"foyer_id": str(extra)})
    assert response.status_code == 303
    assert _rows(settings, "SELECT role FROM household_members WHERE household_id = ?", extra) == [("member",)]
    assert librarian.post(f"/lecteurs/{family['lea']}/foyers", data={"foyer_id": ""}).status_code == 400
    assert librarian.post(f"/lecteurs/{family['lea']}/foyers/{extra}/retirer").status_code == 303


def test_household_is_archived_only_when_empty(client_as, settings, database, family):
    librarian = client_as("librarian")
    home = family["home"]
    refused = librarian.post(f"/foyers/{home}/archiver")
    assert refused.status_code == 400 and "encore des membres" in refused.text
    empty = service.create_household(database, "family", "Famille Vide", "", None)
    assert librarian.post(f"/foyers/{empty}/archiver", data={"version": "1"}).status_code == 303
    assert "Famille Vide" not in librarian.get("/lecteurs", params={"vue": "foyers"}).text
    assert "Réactiver" in librarian.get(f"/foyers/{empty}").text
    assert librarian.post(f"/foyers/{empty}/reactiver").status_code == 303


# --- Structures -------------------------------------------------------------------------------------------------------
def test_structure_with_referents(client_as, settings, database, family):
    librarian = client_as("librarian")
    created = librarian.post("/foyers/structure", data={"nom": "École Jules-Ferry"})
    assert created.status_code == 303
    target = created.headers["location"]
    identifier = target.rsplit("/", 1)[1]
    assert (
        librarian.post(f"{target}/membres", data={"membre_id": str(family["anne"]), "role": "referent"}).status_code
        == 303
    )
    refused = librarian.post(f"{target}/membres", data={"membre_id": str(family["lea"]), "role": "referent"})
    assert refused.status_code == 400 and "référent est un adulte" in refused.text
    assert (
        librarian.post(f"{target}/membres", data={"membre_id": str(family["lea"]), "role": "member"}).status_code == 303
    )
    page = librarian.get(target).text
    assert "Référent" in page and "Structure" in page and "aucun droit" in page
    assert _rows(settings, "SELECT kind FROM households WHERE id = ?", identifier) == [("structure",)]
    assert _rows(settings, "SELECT count(*) FROM guardianships") == [
        (0,)
    ]  # être référent ne crée aucune responsabilité


def test_structure_name_is_required(client_as):
    assert client_as("librarian").post("/foyers/structure", data={"nom": " "}).status_code == 400


# --- Responsables -----------------------------------------------------------------------------------------------------
def test_guardian_is_added_with_explicit_rights_and_updated(client_as, settings, family):
    librarian = client_as("librarian")
    lea, anne = family["lea"], family["anne"]
    response = librarian.post(
        f"/lecteurs/{lea}/responsables",
        data={"resp_id": str(anne), "relation": "parent", "contact": "1", "rang": "2"},
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT can_borrow_for, is_contact, contact_rank FROM guardianships") == [(0, 1, 2)]
    updated = librarian.post(
        f"/lecteurs/{lea}/responsables/{anne}",
        data={"version": "1", "relation": "legal_guardian", "emprunte": "1", "rang": "1"},
    )
    assert updated.status_code == 303
    assert _rows(settings, "SELECT relation, can_borrow_for, is_contact FROM guardianships") == [
        ("legal_guardian", 1, 0)
    ]
    stale = librarian.post(f"/lecteurs/{lea}/responsables/{anne}", data={"version": "1", "relation": "parent"})
    assert stale.status_code == 409
    details = _rows(settings, "SELECT details FROM audit_log WHERE action = 'patron.guardian_updated'")[0][0]
    assert "Anne" not in details


def test_guardian_must_be_an_adult(client_as, database, family):
    other = _person(database, "Zoé", "Roux", "minor", birth="2016")
    response = client_as("librarian").post(
        f"/lecteurs/{family['lea']}/responsables", data={"resp_id": str(other), "relation": "parent"}
    )
    assert response.status_code == 400


def test_guardian_needs_a_choice(client_as, family):
    response = client_as("librarian").post(f"/lecteurs/{family['lea']}/responsables", data={"relation": "parent"})
    assert response.status_code == 400 and "Choisissez" in response.text


def test_guardian_is_removed_without_touching_the_people(client_as, settings, database, family):
    service.add_guardian(database, family["lea"], family["paul"], Rights("parent", True, True, 1), None)
    librarian = client_as("librarian")
    assert librarian.post(f"/lecteurs/{family['lea']}/responsables/{family['paul']}/retirer").status_code == 303
    assert _rows(settings, "SELECT count(*) FROM guardianships") == [(0,)]
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(3,)]


def test_majority_keeps_guardians_to_review(client_as, database, family):
    service.add_guardian(database, family["lea"], family["paul"], Rights("parent", True, True, 1), None)
    service.confirm_majority(database, family["lea"], None, None)
    page = client_as("librarian").get(f"/lecteurs/{family['lea']}").text
    assert "à revoir" in page and "Paul Martin" in page


def test_guardians_new_person_in_registration_joins_the_household(settings, database):
    home = service.create_household(database, "family", "Famille Roux", "", None)
    service.register(
        database,
        Registration(
            person=PersonInput(first_name="Tom", last_name="Roux", category="minor"),
            household=service.HouseholdChoice(household_id=home),
            guardian=GuardianInput(first_name="Marc", last_name="Roux", can_borrow_for=True),
        ),
        None,
    )
    assert _rows(settings, "SELECT role FROM household_members ORDER BY patron_id") == [("child",), ("responsible",)]
