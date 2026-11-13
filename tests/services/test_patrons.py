# SPDX-License-Identifier: EUPL-1.2
"""Lecteurs et familles sur une vraie base migrée. Personnes inventées, courriels en example.org, numéros fictifs."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from biblyo.core import audit
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.patrons import repository as repo
from biblyo.modules.patrons import service
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.service import (
    AddressInput,
    AnonymizeInput,
    ConflictError,
    ContactsEdit,
    GuardianInput,
    HouseholdChoice,
    IdentityEdit,
    PersonInput,
    Registration,
    RegistrationRefused,
    Rights,
)

from ..factories import PASSWORD, make_notice, make_user


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


def _adult(first="Paul", last="Martin", **extra) -> PersonInput:
    return PersonInput(first_name=first, last_name=last, category="adult", **extra)


def _minor(first="Léa", last="Martin", **extra) -> PersonInput:
    return PersonInput(first_name=first, last_name=last, category="minor", **extra)


def _register(database, person, **options) -> int:
    return service.register(database, Registration(person=person, **options), None)


def _patron(database, patron_id):
    with database.session() as session:
        found = session.get(Patron, patron_id)
        assert found is not None
        return found


def _sheet(database, patron_id):
    with database.session() as session:
        return repo.sheet(session, patron_id, service.today())


# --- Identité permanente et cartes --------------------------------------------------------------------------------
def test_three_distinct_identifiers(settings, database):
    patron_id = _register(database, _adult(email="paul.martin@example.org"))
    patron = _patron(database, patron_id)
    assert len(patron.public_id) == 36 and patron.number == "P-000001"
    assert _rows(settings, "SELECT number, ended_on FROM patron_cards WHERE patron_id = ?", patron_id) == [
        ("L-000001", None)
    ]


def test_card_lost_then_replaced_and_never_given_again(settings, database):
    first = _register(database, _adult())
    new_number = service.end_card(database, first, "lost", replace=True, user_id=None)
    assert new_number == "L-000002"
    sheet = _sheet(database, first)
    assert [(card.number, card.end_reason) for card in sheet.cards] == [("L-000002", None), ("L-000001", "lost")]
    assert _patron(database, first).number == "P-000001"  # l'identité ne bouge pas
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _adult("Anne", "Durand", card_number="l-000001"))
    assert set(caught.value.fields) == {"carte"}
    assert "Martin" not in caught.value.fields["carte"]  # aucun détenteur révélé
    second = _register(database, _adult("Anne", "Durand"))
    assert _sheet(database, second).active_card.number == "L-000003"


def test_revoked_card_without_replacement(database):
    patron_id = _register(database, _adult())
    assert service.end_card(database, patron_id, "revoked", replace=False, user_id=None) is None
    sheet = _sheet(database, patron_id)
    assert sheet.active_card is None and sheet.cards[0].end_reason == "revoked"
    with pytest.raises(BusinessError, match="pas de carte en service"):
        service.end_card(database, patron_id, "lost", replace=False, user_id=None)
    with pytest.raises(FormErrors):
        service.end_card(database, patron_id, "lost", replace=True, manual_number="L-000001", user_id=None)


def test_a_card_number_cannot_also_be_a_copy_code(settings, database):
    work = make_notice(settings)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    catalogue.add_copies(database, edition, 1, "", None)
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _adult(card_number="EX-000001"))
    assert "carte" in caught.value.fields


def test_a_card_cannot_take_the_shape_of_a_patron_number(database):
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _adult(card_number="p-000123"))
    assert "numéro de lecteur" in caught.value.fields["carte"]
    patron_id = _register(database, _adult())
    with pytest.raises(FormErrors) as refused:
        service.end_card(database, patron_id, "lost", replace=True, manual_number="P000123", user_id=None)
    assert "numéro de lecteur" in refused.value.fields["carte"]
    assert _sheet(database, patron_id).active_card.number == "L-000001"  # rien n'a été écrit


def test_numbers_are_unique_under_concurrent_registrations(settings):
    errors: list[BaseException] = []

    def worker(index):
        own = Database(settings.database_path)
        try:
            for step in range(5):
                service.register(own, Registration(person=_adult("Personne", f"Numéro {index}-{step}")), None)
        except BaseException as error:
            errors.append(error)
        finally:
            own.dispose()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    numbers = [row[0] for row in _rows(settings, "SELECT number FROM patrons ORDER BY number")]
    assert numbers == [f"P-{value:06d}" for value in range(1, 21)]
    cards = [row[0] for row in _rows(settings, "SELECT number FROM patron_cards ORDER BY number")]
    assert cards == [f"L-{value:06d}" for value in range(1, 21)]


# --- Homonymes ----------------------------------------------------------------------------------------------------
def test_two_homonyms_one_of_them_a_minor(database):
    adult = _register(database, _adult("Léa", "Martin", address=AddressInput("1 rue des Lilas", "", "69001", "Lyon")))
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _minor("Léa", "Martin", birth="2015"))
    review = caught.value.review
    assert [row.id for row in review.homonyms] == [adult]
    assert review.homonyms[0].marks_text == "P-000001 · carte L-000001 · Lyon"
    minor = _register(database, _minor("Léa", "Martin", birth="2015"), homonyms_confirmed=True)
    with database.session() as session:
        rows = repo.search(session, "lea martin", personal=False)
    assert {row.id for row in rows} == {adult, minor}
    assert all(row.homonym for row in rows)
    assert {row.marks.birth_year for row in rows} == {None, 2015}


def test_nothing_is_merged_automatically(settings, database):
    _register(database, _adult("Jean", "Petit"))
    _register(database, _adult("Jean", "Petit"), homonyms_confirmed=True)
    assert len(_rows(settings, "SELECT id FROM patrons WHERE name_norm = 'jean petit'")) == 2


# --- Mineurs, foyers, responsables --------------------------------------------------------------------------------
def test_minor_without_guardian_is_allowed_and_flagged(database):
    minor = _register(database, _minor())
    sheet = _sheet(database, minor)
    assert sheet.needs_guardian is True and sheet.guardians == []
    with database.session() as session:
        assert repo.eligible_guardians(session, minor) == []
        assert [row.id for row in repo.to_handle(session, service.today()).without_guardian] == [minor]


def test_minor_has_no_own_contacts(database):
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _minor(email="lea@example.org"))
    assert "coordonnees" in caught.value.fields


def test_child_in_two_households_with_guardians_of_different_rights(settings, database):
    mother = _register(
        database,
        _adult("Claire", "Martin", phone="06 12 34 56 78"),
        household=HouseholdChoice(new_name="Famille Martin"),
    )
    father = _register(
        database,
        _adult("Marc", "Bernard", email="marc.bernard@example.org"),
        household=HouseholdChoice(new_name="Famille Bernard"),
    )
    households = dict(_rows(settings, "SELECT name, id FROM households"))
    child = _register(
        database,
        _minor("Lou", "Bernard", birth="2016"),
        household=HouseholdChoice(household_id=households["Famille Martin"]),
        guardian=GuardianInput(number="P-000001", relation="parent", can_borrow_for=True, is_contact=True),
    )
    service.add_member(database, households["Famille Bernard"], child, "child", None)
    service.add_guardian(database, child, father, Rights("parent", can_borrow_for=False, is_contact=True), None)
    sheet = _sheet(database, child)
    assert {item.name for item in sheet.households} == {"Famille Martin", "Famille Bernard"}
    assert [(link.other_id, link.can_borrow_for, link.is_contact) for link in sheet.guardians] == [
        (mother, True, True),
        (father, False, True),
    ]
    with database.session() as session:
        assert repo.eligible_guardians(session, child) == [mother]
        assert repo.can_borrow_for(session, father, child) is False


def test_a_household_gives_no_right(settings, database):
    uncle = _register(
        database, _adult("Tom", "Martin", phone="06 98 76 54 32"), household=HouseholdChoice(new_name="Famille Martin")
    )
    household = _rows(settings, "SELECT id FROM households")[0][0]
    child = _register(database, _minor("Zoé", "Martin"), household=HouseholdChoice(household_id=household))
    with database.session() as session:
        assert repo.can_borrow_for(session, uncle, child) is False  # même foyer, aucun lien : aucun droit
    assert _sheet(database, child).needs_guardian is True


def test_unreachable_guardian_does_not_count(database):
    guardian = _register(database, _adult("Sans", "Contact"))
    child = _register(database, _minor("Enfant", "Contact"))
    service.add_guardian(database, child, guardian, Rights("parent", can_borrow_for=True, is_contact=True), None)
    with database.session() as session:
        assert repo.eligible_guardians(session, child) == []  # habilité mais injoignable
    service.update_contacts(database, guardian, ContactsEdit(email="sans.contact@example.org"), None, None)
    with database.session() as session:
        assert repo.eligible_guardians(session, child) == [guardian]


def test_new_guardian_created_during_registration(settings, database):
    child = _register(
        database,
        _minor("Ana", "Lopez", birth="2017"),
        household=HouseholdChoice(new_name="Famille Lopez"),
        guardian=GuardianInput(first_name="Rosa", last_name="Lopez", phone="0690 12 34 56", can_borrow_for=True),
    )
    sheet = _sheet(database, child)
    guardian = sheet.guardians[0]
    assert guardian.other_name == "Rosa Lopez" and guardian.can_borrow_for and not guardian.is_contact
    assert _rows(settings, "SELECT value FROM contacts WHERE patron_id = ?", guardian.other_id) == [("+590690123456",)]
    assert {item.role for item in _sheet(database, guardian.other_id).households} == {"responsible"}


@pytest.mark.parametrize(
    ("minor_first", "case"),
    [("Soi", "self"), ("Petit", "minor guardian"), ("Adulte", "adult ward")],
)
def test_impossible_guardianships_are_refused(database, minor_first, case):
    child = _register(database, _minor(minor_first, "Test"))
    other_minor = _register(database, _minor("Autre", "Mineur"))
    adult = _register(database, _adult("Grand", "Adulte"))
    rights = Rights("parent", True, True)
    with pytest.raises(BusinessError):
        if case == "self":
            service.add_guardian(database, child, child, rights, None)
        elif case == "minor guardian":
            service.add_guardian(database, child, other_minor, rights, None)
        else:
            service.add_guardian(database, adult, child, rights, None)


def test_duplicate_guardianship_and_bad_rights_are_refused(database):
    child, adult = _register(database, _minor()), _register(database, _adult("Paul", "Durand"))
    service.add_guardian(database, child, adult, Rights("parent", True, False), None)
    with pytest.raises(BusinessError, match="déjà responsable"):
        service.add_guardian(database, child, adult, Rights("parent", True, False), None)
    with pytest.raises(BusinessError):
        service.update_guardian(database, child, adult, Rights("cousin", True, False), None, None)


def test_structure_with_referents(database):
    school = service.create_household(database, "structure", "École des Tilleuls", "", None)
    teacher = _register(
        database, _adult("Inès", "Moreau"), household=HouseholdChoice(household_id=school, role="referent")
    )
    pupil = _register(database, _minor("Noé", "Garnier"), household=HouseholdChoice(household_id=school))
    with database.session() as session:
        sheet = repo.household_sheet(session, school)
    assert {(member.patron_id, member.role) for member in sheet.members} == {(teacher, "referent"), (pupil, "member")}
    with pytest.raises(RegistrationRefused):
        _register(database, _minor("Eve", "Garnier"), household=HouseholdChoice(household_id=school, role="child"))


def test_leaving_a_household_keeps_the_identity_and_empty_household_is_kept(settings, database):
    patron_id = _register(database, _adult(), household=HouseholdChoice(new_name="Famille Seule"))
    household = _rows(settings, "SELECT id FROM households")[0][0]
    with pytest.raises(BusinessError, match="encore des membres"):
        service.set_household_archived(database, household, True, None, None)
    service.remove_member(database, household, patron_id, None)
    assert _patron(database, patron_id).first_name == "Paul"
    with database.session() as session:
        assert [card.id for card in repo.to_handle(session, service.today()).empty_households] == [household]
    service.set_household_archived(database, household, True, None, None)
    assert _rows(settings, "SELECT archived_at IS NOT NULL FROM households") == [(1,)]


# --- Majorité -----------------------------------------------------------------------------------------------------
def test_majority_is_confirmed_by_a_person_and_keeps_links(database):
    parent = _register(database, _adult("Paul", "Lambert", phone="06 11 22 33 44"))
    teen = _register(
        database,
        _minor("Jade", "Lambert", birth="2008-01-15"),
        guardian=GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True),
    )
    with database.session() as session:
        assert [row.id for row in repo.to_handle(session, service.today()).majorities] == [teen]
    assert _patron(database, teen).category == "minor"  # rien n'a basculé seul
    service.confirm_majority(database, teen, 1, None)
    assert _patron(database, teen).category == "adult"
    assert [link.other_id for link in _sheet(database, teen).guardians] == [parent]  # le lien reste, proposé au retrait
    with pytest.raises(BusinessError, match="déjà"):
        service.confirm_majority(database, teen, None, None)


# --- Coordonnées --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("phone", "territory", "stored"),
    [
        ("0696 12 34 56", "", "+596696123456"),
        ("0639 12 34 56", "", "+262639123456"),
        ("+687 28 12 34", "", "+687281234"),
        ("+44 20 7946 0958", "", "+442079460958"),
        ("41 23 45", "saint_pierre", "+508412345"),
    ],
)
def test_overseas_and_international_numbers(settings, database, phone, territory, stored):
    patron_id = _register(database, _adult(phone=phone, phone_territory=territory))
    assert _rows(settings, "SELECT value FROM contacts WHERE patron_id = ?", patron_id) == [(stored,)]


def test_ambiguous_number_must_be_chosen(database):
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _adult(phone="41 23 45"))
    choices = caught.value.review.phone_choices["telephone"]
    assert [item.territory for item in choices] == ["saint_pierre", "wallis", "caledonie"]
    with pytest.raises(RegistrationRefused) as caught:
        _register(database, _adult(phone="+33 6 90 12 34 56"))
    assert [item.e164 for item in caught.value.review.phone_choices["telephone"]] == ["+590690123456"]


def test_search_by_contact_only_with_personal_permission(database):
    patron_id = _register(database, _adult(email="paul.martin@example.org", phone="06 12 34 56 78"))
    with database.session() as session:
        assert repo.search(session, "paul.martin@example.org", personal=False) == []
        assert repo.search(session, "06 12 34 56 78", personal=False) == []
        assert [row.id for row in repo.search(session, "paul.martin@example.org", personal=True)] == [patron_id]
        assert [row.id for row in repo.search(session, "0612345678", personal=True)] == [patron_id]
        assert [row.id for row in repo.search(session, "P-1", personal=False)] == [patron_id]
        assert [row.id for row in repo.search(session, "l-000001", personal=False)] == [patron_id]


def test_search_by_an_old_card_says_so(database):
    patron_id = _register(database, _adult())
    service.end_card(database, patron_id, "lost", replace=True, user_id=None)
    with database.session() as session:
        rows = repo.search(session, "L-000001", personal=False)
    assert [row.id for row in rows] == [patron_id] and rows[0].card_note


# --- Modifications concurrentes, journal --------------------------------------------------------------------------
def test_concurrent_modification_is_refused(database):
    patron_id = _register(database, _adult())
    service.update_identity(database, patron_id, IdentityEdit("Paul", "Martin-Durand"), 1, None)
    with pytest.raises(ConflictError):
        service.update_identity(database, patron_id, IdentityEdit("Paul", "Autre"), 1, None)
    assert _patron(database, patron_id).last_name == "Martin-Durand"


def test_the_journal_never_holds_personal_values(settings, database):
    patron_id = _register(
        database,
        _adult("Camille", "Rivière", email="camille@example.org", phone="06 12 34 56 78", birth="1990-04-02",
               address=AddressInput("3 rue Haute", "", "75001", "Paris"), notes="Préfère le samedi"),
    )  # fmt: skip
    service.update_identity(database, patron_id, IdentityEdit("Camille", "Rivière-Haut", "1990"), None, None)
    service.end_card(database, patron_id, "lost", replace=True, user_id=None)
    journal = " ".join(str(row) for row in _rows(settings, "SELECT action, details FROM audit_log"))
    for value in (
        "Camille",
        "Rivière",
        "camille@example.org",
        "612345678",
        "1990",
        "rue Haute",
        "Paris",
        "samedi",
        "L-0000",
    ):
        assert value not in journal, value
    with database.session() as session, pytest.raises(ValueError, match="personnelle"):
        audit.record(session, "patron.update", "patron", patron_id, details={"email": "x@example.org"})


# --- Conservation et anonymisation --------------------------------------------------------------------------------
@pytest.fixture
def admin(settings):
    return make_user(settings, "admin.essai", "admin")


def _anonymize(database, patron_id, user, *, number="P-000001", password=PASSWORD, reason="request", text=""):
    return service.anonymize(database, patron_id, AnonymizeInput(number, password, reason, text), user)


@pytest.mark.parametrize(
    ("number", "password", "reason", "text", "fields"),
    [
        ("L-000001", PASSWORD, "request", "", {"numero"}),
        ("P-000099", PASSWORD, "request", "", {"numero"}),
        ("P-000001", "mauvais-mot-de-passe", "request", "", {"mot_de_passe"}),
        ("P-000001", PASSWORD, "", "", {"motif"}),
        ("P-000001", PASSWORD, "other", "", {"motif"}),
    ],
)
def test_anonymization_with_invalid_confirmation(settings, database, admin, number, password, reason, text, fields):
    patron_id = _register(database, _adult())
    with pytest.raises(FormErrors) as caught:
        _anonymize(database, patron_id, admin, number=number, password=password, reason=reason, text=text)
    assert set(caught.value.fields) == fields
    if number.startswith("L-"):
        assert "numéro de carte" in caught.value.fields["numero"]
    assert _patron(database, patron_id).status == "active"


def test_anonymization_with_an_active_hold_is_refused(database, admin):
    patron_id = _register(database, _adult())
    service.place_hold(database, patron_id, "Litige en cours", "", admin)
    with pytest.raises(BusinessError, match="conservation"):
        _anonymize(database, patron_id, admin)
    hold = _sheet(database, patron_id).active_hold
    service.lift_hold(database, hold.id, admin)
    _anonymize(database, patron_id, admin)
    assert _patron(database, patron_id).status == "anonymized"


def test_expired_hold_no_longer_blocks(database, admin):
    patron_id = _register(database, _adult())
    service.place_hold(database, patron_id, "Délai légal", "01/01/2020", admin)
    _anonymize(database, patron_id, admin)


def test_anonymization_without_an_active_card(database, admin):
    patron_id = _register(database, _adult())
    service.end_card(database, patron_id, "revoked", replace=False, user_id=None)
    _anonymize(database, patron_id, admin)
    assert _patron(database, patron_id).status == "anonymized"


def test_anonymization_leaves_nothing_identifying(settings, database, admin):
    """Vérification colonne par colonne de chaque table concernée."""
    parent = _register(
        database,
        _adult("Hélène", "Vasseur", email="helene.vasseur@example.org", phone="06 12 34 56 78", birth="1985-03-12",
               address=AddressInput("8 rue Neuve", "", "59000", "Lille"), notes="Bénévole le mercredi",
               membership_expires_on="31/08/2027"),
        household=HouseholdChoice(new_name="Famille Vasseur"),
    )  # fmt: skip
    child = _register(
        database,
        _minor("Mila", "Vasseur", birth="2016"),
        household=HouseholdChoice(household_id=1),
        guardian=GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True),
    )
    service.end_card(database, parent, "lost", replace=True, user_id=None)
    service.export(database, parent, admin)
    result = _anonymize(database, parent, admin, reason="other", text="Départ de la commune")
    assert (result.contacts, result.households, result.guardianships, result.cards) == (3, 1, 1, 2)
    row = _rows(settings, "SELECT * FROM patrons WHERE id = ?", parent)[0]
    columns = [item[1] for item in _rows(settings, "PRAGMA table_info(patrons)")]
    values = dict(zip(columns, row, strict=True))
    assert {key: values[key] for key in ("first_name", "last_name", "name_norm", "sort_key")} == dict.fromkeys(
        ("first_name", "last_name", "name_norm", "sort_key"), ""
    )
    assert values["birth"] is None and values["notes"] is None and values["membership_expires_on"] is None
    assert values["status"] == "anonymized" and values["anonymized_at"]
    assert values["number"] == "P-000001" and values["category"] == "adult"  # traçabilité et statistiques
    assert _rows(settings, "SELECT COUNT(*) FROM contacts WHERE patron_id = ?", parent) == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM household_members WHERE patron_id = ?", parent) == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM guardianships WHERE ? IN (minor_id, guardian_id)", parent) == [(0,)]
    assert _rows(settings, "SELECT number, patron_id, end_reason FROM patron_cards ORDER BY number") == [
        ("L-000001", None, "lost"),
        ("L-000002", child, None),
        ("L-000003", None, "anonymized"),
    ]
    dump = " ".join(str(item) for item in _rows(settings, "SELECT * FROM patrons WHERE id = ?", parent))
    dump += " ".join(str(item) for item in _rows(settings, "SELECT details FROM audit_log"))
    for value in ("Hélène", "Vasseur", "helene", "612345678", "1985", "Neuve", "Lille", "mercredi", "2027"):
        assert value not in dump, value
    entry = json.loads(_rows(settings, "SELECT details FROM audit_log WHERE action = 'patron.anonymize'")[0][0])
    assert entry["motif"] == "other" and entry["coordonnees_effacees"] == 3
    assert _sheet(database, child).needs_guardian is True  # l'enfant a perdu son seul responsable : à traiter
    with pytest.raises(BusinessError, match="déjà anonymisé"):
        _anonymize(database, parent, admin)
    with pytest.raises(BusinessError, match="anonymisé"):
        service.update_identity(database, parent, IdentityEdit("Hélène", "Vasseur"), None, None)


# --- Export -------------------------------------------------------------------------------------------------------
def test_export_has_everything_about_the_person_and_nothing_about_others(settings, database, admin):
    parent = _register(database, _adult("Rosa", "Lopez", phone="06 12 34 56 78", email="rosa@example.org"))
    child = _register(
        database,
        _minor("Ana", "Lopez"),
        guardian=GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True),
    )
    data = service.export(database, child, admin)
    assert (
        data["lecteur"]["numero"] == "P-000002" and data["lecteur"]["identifiant"] == _patron(database, child).public_id
    )
    assert data["responsables"] == [
        {
            "numero": "P-000001",
            "nom": "Rosa Lopez",
            "relation": "Parent",
            "peut_emprunter": True,
            "peut_emprunter_seul": False,
            "a_contacter": True,
        }
    ]
    text = json.dumps(data, ensure_ascii=False)
    assert "rosa@example.org" not in text and "06 12 34 56 78" not in text
    parent_data = service.export(database, parent, admin)
    assert {item["type"] for item in parent_data["coordonnees"]} == {"email", "phone"}
    assert parent_data["responsable_de"][0]["numero"] == "P-000002"
    exports = _rows(settings, "SELECT entity_id, user_id FROM audit_log WHERE action = 'patron.export'")
    assert exports == [(child, admin), (parent, admin)]


# --- Statuts et adhésion ------------------------------------------------------------------------------------------
def test_status_changes_and_membership(database):
    patron_id = _register(database, _adult(membership_expires_on="01/01/2020"))
    with database.session() as session:
        assert [row.id for row in repo.to_handle(session, service.today()).expired] == [patron_id]
    service.renew_membership(database, patron_id, "", None, None)
    assert _patron(database, patron_id).membership_expires_on > service.today().isoformat()
    service.change_status(database, patron_id, "archive", None, None)
    with database.session() as session:
        assert repo.directory(session, "tous", 1, service.today()).total == 0
        assert repo.directory(session, "archives", 1, service.today()).total == 1
    service.change_status(database, patron_id, "reactivate", None, None)
    with pytest.raises(BusinessError, match="Impossible"):
        service.change_status(database, patron_id, "reactivate", None, None)
