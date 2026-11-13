# SPDX-License-Identifier: EUPL-1.2
"""Prêter depuis la fiche d'un livre : bouton, choix des exemplaires, comptoir prérempli, retour au livre."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

RIGHTS = frozenset({rules.LEND, rules.RENEW})
HOLDER = "Hortense Valmont"


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _book(settings, database, count=1, title="Carnet de Jean"):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None) if count else []
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, ids


def _person(database, first, last, category="adult", card=""):
    email = f"{first.lower()}@example.org" if category == "adult" else ""
    person = PersonInput(first_name=first, last_name=last, category=category, email=email, card_number=card)
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _lend(database, patron, copy_id):
    return service.lend(database, LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))


def _lend_button(html):
    """L'adresse du bouton « Prêter » de la fiche, cherchée lien par lien."""
    for href, body in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        if '<span class="lbl">Prêter</span>' in body:
            return href.replace("&amp;", "&")
    return None


def _counts(settings):
    return _rows(settings, "SELECT (SELECT COUNT(*) FROM loans), (SELECT COUNT(*) FROM loan_operations)")[0]


# --- Le bouton de la fiche ----------------------------------------------------------------------------------------
def test_one_loanable_copy_opens_the_counter_with_it(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    html = client_as("volunteer").get(f"/catalogue/notices/{work}").text
    assert _lend_button(html) == f"/comptoir?exemplaire={copy_id}&livre={work}"
    assert "/a-venir/prets" not in html and "btn-soon" not in html.split("hero-acts")[1].split("</div>")[0]


@pytest.mark.parametrize("case", ["several", "none"])
def test_otherwise_the_button_opens_the_choice(settings, database, client_as, case):
    work, ids = _book(settings, database, 2)
    if case == "none":
        for copy_id in ids:
            _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", copy_id)
    assert _lend_button(client_as("volunteer").get(f"/catalogue/notices/{work}").text) == f"/comptoir/livre/{work}"


def test_en_place_is_not_enough_a_reference_only_copy_does_not_count(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    _execute(settings, "UPDATE copies SET is_loanable = 0 WHERE id = ?", copy_id)
    assert _lend_button(client_as("volunteer").get(f"/catalogue/notices/{work}").text) == f"/comptoir/livre/{work}"


@pytest.mark.parametrize(
    ("role", "shown"), [("admin", True), ("librarian", True), ("volunteer", True), ("viewer", False)]
)
def test_the_button_follows_the_permissions(settings, database, client_as, role, shown):
    work, _ids = _book(settings, database)
    assert (_lend_button(client_as(role).get(f"/catalogue/notices/{work}").text) is not None) is shown


def test_no_button_without_any_copy(settings, database, client_as):
    work, _ids = _book(settings, database, 0)
    assert _lend_button(client_as("volunteer").get(f"/catalogue/notices/{work}").text) is None


# --- Le choix des exemplaires et les raisons --------------------------------------------------------------------------
@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 200), ("viewer", 403)])
def test_choice_access(settings, database, client_as, role, status):
    work, _ids = _book(settings, database)
    assert client_as(role).get(f"/comptoir/livre/{work}").status_code == status


def test_choice_unknown_book(client_as):
    assert client_as("volunteer").get("/comptoir/livre/999999").status_code == 404


def test_choice_explains_every_copy_without_naming_the_holder(settings, database, client_as):
    work, (free, lent, repair, withdrawn, onsite, damaged) = _book(settings, database, 6)
    holder = _person(database, *HOLDER.split())
    loan = _lend(database, holder, lent).loan_ids[0]
    _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", repair)
    _execute(settings, "UPDATE copies SET status = 'withdrawn' WHERE id = ?", withdrawn)
    _execute(settings, "UPDATE copies SET is_loanable = 0 WHERE id = ?", onsite)
    _execute(settings, "UPDATE copies SET condition = 'damaged' WHERE id = ?", damaged)
    html = client_as("volunteer").get(f"/comptoir/livre/{work}").text
    boxes = set(re.findall(r'name="exemplaire" value="(\d+)"', html))
    assert boxes == {str(free), str(damaged)}  # seuls ceux qui peuvent sortir se cochent
    for text in ("déjà en prêt, retour prévu le", "en réparation", "retiré", "se consulte sur place", "État signalé"):
        assert text in html, text
    assert HOLDER not in html and f"/prets/{loan}" not in html and "Enregistrer le retour" in html
    assert f"/prets/{loan}" in client_as("librarian").get(f"/comptoir/livre/{work}").text  # avec l'historique


def test_choice_when_nothing_can_leave(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", copy_id)
    html = client_as("volunteer").get(f"/comptoir/livre/{work}").text
    assert "Aucun exemplaire ne peut sortir maintenant" in html and "Prêter les exemplaires cochés" not in html


def test_choice_for_a_book_without_copy(settings, database, client_as):
    work, _ids = _book(settings, database, 0)
    assert "aucun exemplaire physique" in client_as("librarian").get(f"/comptoir/livre/{work}").text


# --- Le comptoir prérempli : contrôlé comme un scan, jamais validé ----------------------------------------------------
def test_the_counter_opens_with_the_copy_and_records_nothing(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    html = client_as("volunteer").get(f"/comptoir?exemplaire={copy_id}&livre={work}").text
    assert f'name="ex" value="{copy_id}"' in html and f'name="livre" value="{work}"' in html
    assert "Choisissez d’abord le lecteur" in html and "Revenir à « Carnet de Jean » sans prêter" in html
    assert _counts(settings) == (0, 0)


@pytest.mark.parametrize(
    ("change", "message"), [("lent", "déjà en prêt"), ("repair", "réparation"), ("withdrawn", "retiré")]
)
def test_a_copy_that_cannot_leave_is_refused_at_the_counter(settings, database, client_as, change, message):
    work, (copy_id,) = _book(settings, database)
    if change == "lent":
        _lend(database, _person(database, *HOLDER.split()), copy_id)
    else:
        _execute(
            settings,
            "UPDATE copies SET status = ? WHERE id = ?",
            "in_repair" if change == "repair" else change,
            copy_id,
        )
    html = client_as("volunteer").get(f"/comptoir?exemplaire={copy_id}&livre={work}").text
    held = re.findall(r'<input type="hidden" name="ex" value="(\d+)">', html)
    rest = re.sub(r'<section class="card desk-reader".*?</section>', "", html, count=1, flags=re.S)
    assert message in html and str(copy_id) not in held and HOLDER not in rest  # la liste des lecteurs mise à part


def test_unknown_or_forged_identifiers_add_nothing(settings, database, client_as):
    html = client_as("volunteer").get("/comptoir?exemplaire=999999&exemplaire=abc&livre=xyz").text
    assert "Code inconnu" in html and 'name="ex"' not in html and 'name="livre"' not in html


def test_after_validation_the_counter_leads_back_to_the_book(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    _person(database, "Paul", "Martin", card="CARTE-PM")
    browser = client_as("volunteer")
    browser.get(f"/comptoir?exemplaire={copy_id}&livre={work}")
    state = {"ex": str(copy_id), "livre": str(work), "code": "CARTE-PM"}
    html = browser.post("/comptoir/scan", data=state).text
    assert "1 livre · Paul Martin" in html
    token = re.search(r'name="jeton" value="([^"]+)"', html).group(1)
    reader = re.search(r'name="lecteur" value="(\d+)"', html).group(1)
    done = browser.post(
        "/comptoir/valider",
        data={"ex": str(copy_id), "livre": str(work), "lecteur": reader, "cadre": "self", "jeton": token},
    )
    assert "Prêt enregistré" in done.text and f'href="/catalogue/notices/{work}"' in done.text
    assert _counts(settings) == (1, 1)


# --- Ce qui manque avant de valider -----------------------------------------------------------------------------------
def test_the_counter_says_why_validation_waits(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database)
    child = _person(database, "Léa", "Durand", "minor", card="CARTE-LD")
    mother, father = _person(database, "Anne", "Durand"), _person(database, "Marc", "Durand")
    for adult in (mother, father):
        _execute(
            settings,
            "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
            " version, created_at, updated_at) VALUES (?, ?, 'parent', 1, 1, 1, 1, 'x', 'x')",
            child,
            adult,
        )
    browser = client_as("volunteer")
    alone = browser.post("/comptoir/scan", data={"code": "CARTE-LD"}).text
    assert "Ajoutez au moins un livre" not in alone and "Indiquez comment l’enfant emprunte" in alone
    adult = browser.post("/comptoir/etat", data={"lecteur": str(child), "ex": str(copy_id), "cadre": "with_adult"}).text
    assert "Choisissez l’adulte qui répond de ce prêt" in adult
    assert "ne peut pas répondre de ce prêt" not in adult  # aucune alerte sur un adulte que personne n'a choisi
    empty = browser.post("/comptoir/etat", data={"lecteur": str(mother)}).text
    assert "Ajoutez au moins un livre" in empty


def test_a_minor_authorized_alone_borrows_from_a_book(settings, database, client_as):
    work, (copy_id,) = _book(settings, database)
    child = _person(database, "Emma", "Delmas", "minor", card="CARTE-ED")
    parent = _person(database, "Claire", "Roux")
    _execute(
        settings,
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
        " can_borrow_alone, version, created_at, updated_at) VALUES (?, ?, 'parent', 0, 1, 1, 1, 1, 'x', 'x')",
        child,
        parent,
    )
    browser = client_as("volunteer")
    html = browser.post("/comptoir/scan", data={"ex": str(copy_id), "livre": str(work), "code": "CARTE-ED"}).text
    html = browser.post(
        "/comptoir/etat", data={"ex": str(copy_id), "livre": str(work), "lecteur": str(child), "cadre": "alone"}
    ).text
    token = re.search(r'name="jeton" value="([^"]+)"', html).group(1)
    data = {"ex": str(copy_id), "livre": str(work), "lecteur": str(child), "cadre": "alone", "jeton": token}
    assert "Prêt enregistré" in browser.post("/comptoir/valider", data=data).text
    assert _rows(settings, "SELECT borrow_mode, responsible_id FROM loans") == [("alone", parent)]


def test_a_previous_reader_is_proposed_never_resumed_silently(settings, database, client_as):
    """UX-Circulation 2.0 (6.3) : le lecteur laissé en cours dans l'onglet est proposé ; rien n'est choisi sans clic."""
    _work, (copy_id,) = _book(settings, database)
    _other, (earlier,) = _book(settings, database, title="Le Livre d’avant")
    reader = _person(database, "Paul", "Martin")
    browser = client_as("volunteer")
    proposed = browser.post(
        "/comptoir/etat", data={"ex": str(copy_id), "propose_lecteur": str(reader), "propose_ex": str(earlier)}
    ).text
    assert "Un prêt était en préparation pour Paul Martin" in proposed and "Le Livre d’avant" in proposed
    assert 'name="lecteur" value=' not in proposed and "Reprendre ce lecteur" in proposed
    data = {"ex": str(copy_id), "propose_lecteur": str(reader), "propose_ex": str(earlier), "reprendre": "1"}
    resumed = browser.post("/comptoir/etat", data=data).text
    assert f'name="lecteur" value="{reader}"' in resumed and "Un prêt était en préparation" not in resumed
    assert f'name="ex" value="{earlier}"' in resumed and f'name="ex" value="{copy_id}"' in resumed  # le panier reste
    data = {"ex": str(copy_id), "propose_lecteur": str(reader), "ignorer": "1"}
    ignored = browser.post("/comptoir/etat", data=data).text
    assert 'name="lecteur" value=' not in ignored and "Un prêt était en préparation" not in ignored


# --- L'onglet « Prêts » de la fiche -----------------------------------------------------------------------------------
def test_the_loans_tab_by_role(settings, database, client_as):
    work, (lent, returned, _free) = _book(settings, database, 3)
    holder = _person(database, *HOLDER.split())
    loan = _lend(database, holder, lent).loan_ids[0]
    _lend(database, holder, returned)
    service.return_copy(database, returned, Actor(None, RIGHTS))
    path = f"/catalogue/notices/{work}?onglet=prets"
    volunteer = client_as("volunteer").get(path).text
    assert "Prêts à venir" not in volunteer and "En prêt" in volunteer and "En cours" in volunteer
    assert HOLDER not in volunteer and f"/prets/{loan}" not in volunteer and "Derniers prêts terminés" not in volunteer
    librarian = client_as("librarian").get(path).text
    assert HOLDER in librarian and f"/prets/{loan}" in librarian and "Derniers prêts terminés" in librarian
    viewer = client_as("viewer").get(path).text
    assert "Votre compte ne donne pas accès aux prêts" in viewer and HOLDER not in viewer
    assert "Prêts à venir" not in viewer
