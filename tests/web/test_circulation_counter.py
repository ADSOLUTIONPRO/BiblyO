# SPDX-License-Identifier: EUPL-1.2
"""Le comptoir de prêt, route par route et rôle par rôle, dans le HTML brut. Personnes et livres inventés."""

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

EMAIL = "lea.parent@example.org"
PHONE = "0612345678"
RIGHTS = frozenset({rules.LEND, rules.RENEW})


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
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


def _copies(settings, database, count=1, title="Le Lotus bleu"):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return ids


def _code(settings, copy_id):
    return _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]


def _adult(database, first="Anne", last="Durand", email=EMAIL, card=""):
    person = PersonInput(first_name=first, last_name=last, category="adult", email=email, phone=PHONE, card_number=card)
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _minor(database, first="Léa", last="Durand"):
    person = PersonInput(first_name=first, last_name=last, category="minor")
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _link(settings, minor, adult, *, can_borrow_for=1, is_contact=1, alone=0):
    _execute(
        settings,
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
        " can_borrow_alone, version, created_at, updated_at) VALUES (?, ?, 'parent', ?, ?, 1, ?, 1, 'x', 'x')",
        minor,
        adult,
        can_borrow_for,
        is_contact,
        alone,
    )


def _card(settings, patron):
    return _rows(settings, "SELECT number FROM patron_cards WHERE patron_id = ? AND ended_on IS NULL", patron)[0][0]


def _hidden(html: str) -> list[tuple[str, str]]:
    return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html)


def _state(html: str) -> list[tuple[str, str]]:
    return [item for item in _hidden(html) if item[0] not in ("_csrf", "code")]


def _form(pairs) -> dict[str, list[str]]:
    """httpx attend un dictionnaire : un même nom peut y porter plusieurs valeurs."""
    found: dict[str, list[str]] = {}
    for name, value in pairs:
        found.setdefault(name, []).append(value)
    return found


def _held(html: str) -> list[tuple[str, str]]:
    """Ce que le comptoir garde en cours : lecteur, exemplaires, codes en attente. Le jeton n'en fait pas partie, ni
    l'état d'affichage du catalogue (« parcourir »)."""
    return [item for item in _state(html) if item[0] not in ("jeton", "parcourir")]


def _beyond_the_directory(html: str) -> str:
    """La page sans la liste des lecteurs (UX-Circulation 2.0) : chaque lecteur en service y figure, sans aucun lien
    avec le code scanné. Ce qui reste ne doit jamais relier une carte ou un exemplaire à une personne."""
    found = re.sub(r'<section class="card desk-reader".*?</section>', "", html, count=1, flags=re.S)
    assert found != html or "desk-reader" not in html
    return found


def _scan(browser, html: str | None, code: str) -> str:
    response = browser.post("/comptoir/scan", data=_form([*(_state(html) if html else []), ("code", code)]))
    assert response.status_code == 200, response.text
    return response.text


def _lend_direct(settings, database, patron, copy_id):
    return service.lend(database, LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))


# --- Accès --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 200), ("viewer", 403)])
def test_counter_access_by_role(client_as, role, status):
    assert client_as(role).get("/comptoir").status_code == status


def test_anonymous_is_sent_to_login(client):
    assert client.get("/comptoir").status_code in (302, 303)


def test_counter_is_in_the_navigation(client_as):
    html = client_as("volunteer").get("/").text
    assert 'href="/comptoir"' in html


@pytest.mark.parametrize("path", ["/comptoir/scan", "/comptoir/choix", "/comptoir/etat", "/comptoir/valider"])
def test_writes_are_closed_to_a_viewer(client_as, path):
    assert client_as("viewer").post(path, data={"code": "X"}).status_code == 403


def test_a_page_without_csrf_is_refused(client_as):
    browser = client_as("volunteer")
    del browser.headers["X-CSRF-Token"]
    assert browser.post("/comptoir/scan", data={"code": "X"}).status_code == 403


# --- Identifier (D5, D11) -----------------------------------------------------------------------------------------
def test_a_card_selects_the_reader_with_marks_and_no_contact(settings, database, client_as):
    patron = _adult(database, card="CARTE-1")
    html = _scan(client_as("volunteer"), None, "carte-1")
    assert "Anne Durand" in html and "P-000001" in html
    assert EMAIL not in html and "0612" not in html and "+33" not in html
    assert ("lecteur", str(patron)) in _state(html)


def test_a_permanent_number_selects_the_reader(settings, database, client_as):
    patron = _adult(database)
    assert ("lecteur", str(patron)) in _state(_scan(client_as("volunteer"), None, "p-1"))


def test_a_name_selects_when_it_is_the_only_one(settings, database, client_as):
    patron = _adult(database)
    assert ("lecteur", str(patron)) in _state(_scan(client_as("volunteer"), None, "Durand"))


def test_homonyms_require_an_explicit_choice(settings, database, client_as):
    first = _adult(database, email="a1@example.org")
    second = _adult(database, email="a2@example.org")
    html = _scan(client_as("volunteer"), None, "Anne Durand")
    assert "Quel résultat" in html and not [item for item in _state(html) if item[0] == "lecteur"]
    assert f'value="name:{first}"' in html and f'value="name:{second}"' in html


def test_an_unknown_code_is_said_and_adds_nothing(client_as):
    html = _scan(client_as("volunteer"), None, "INCONNU-99")
    assert "Code inconnu" in html and not _held(html)


def test_an_ended_card_is_refused_without_naming_anyone(settings, database, client_as):
    patron = _adult(database)
    card = _card(settings, patron)
    patrons.end_card(database, patron, "lost", replace=False, user_id=None)
    html = _scan(client_as("volunteer"), None, card)
    assert "plus en service" in html and "Durand" not in _beyond_the_directory(html) and not _held(html)
    assert card not in html  # la carte perdue n'est montrée nulle part, pas même dans la liste


@pytest.mark.parametrize("status", ["archived", "anonymized"])
def test_a_withdrawn_reader_is_not_proposed(settings, database, client_as, status):
    patron = _adult(database)
    card = _card(settings, patron)
    _execute(settings, "UPDATE patrons SET status = ? WHERE id = ?", status, patron)
    html = _scan(client_as("volunteer"), None, card)
    assert "Code inconnu" in html and "Durand" not in html and not _held(html)


def test_a_forged_reader_id_selects_nobody(settings, database, client_as):
    patron = _adult(database)
    _execute(settings, "UPDATE patrons SET status = 'archived' WHERE id = ?", patron)
    response = client_as("volunteer").post("/comptoir/etat", data={"lecteur": str(patron)})
    assert "Durand" not in response.text and ("lecteur", str(patron)) not in _state(response.text)


def test_a_search_never_enters_an_address(client_as):
    response = client_as("volunteer").post("/comptoir/scan", data={"code": "Durand"})
    assert "Durand" not in str(response.url.query) and response.url.path == "/comptoir/scan"


# --- Ambiguïtés entre types de codes (D5) ---------------------------------------------------------------------------
def _same_code(settings, database):
    (copy_id,) = _copies(settings, database)
    patron = _adult(database)
    _execute(settings, "UPDATE patron_cards SET number = ? WHERE patron_id = ?", _code(settings, copy_id), patron)
    return copy_id, patron


def test_a_code_for_a_card_and_a_copy_adds_nothing_until_chosen(settings, database, client_as):
    copy_id, _patron = _same_code(settings, database)
    html = _scan(client_as("volunteer"), None, _code(settings, copy_id))
    assert "Quel résultat" in html and "Carte" in html and "Exemplaire" in html
    assert not [item for item in _state(html) if item[0] in ("lecteur", "ex")]


def test_the_following_scans_wait_for_the_choice(settings, database, client_as):
    copy_id, _patron = _same_code(settings, database)
    (other,) = _copies(settings, database, title="Tintin")
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, copy_id))
    html = _scan(browser, html, _code(settings, other))  # la douchette continue : rien ne s'ajoute
    assert ("attente", _code(settings, other)) in _state(html)
    assert not [item for item in _state(html) if item[0] == "ex"]
    response = browser.post("/comptoir/choix", data=_form([*_state(html), ("choix", f"copy:{copy_id}")]))
    state = _state(response.text)
    assert ("ex", str(copy_id)) in state and ("ex", str(other)) in state  # dans l'ordre, sans ressaisie
    assert not [item for item in state if item[0] in ("attente", "doute")]


def test_a_choice_outside_the_candidates_is_ignored(settings, database, client_as):
    copy_id, _patron = _same_code(settings, database)
    stranger = _adult(database, "Marc", "Roy", "m@example.org")
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, copy_id))
    response = browser.post("/comptoir/choix", data=_form([*_state(html), ("choix", f"card:{stranger}")]))
    assert ("lecteur", str(stranger)) not in _state(response.text)


def test_a_card_shaped_like_a_number_is_ambiguous_with_the_reader(settings, database, client_as):
    first = _adult(database)
    second = _adult(database, "Marc", "Roy", "m@example.org")
    _execute(settings, "UPDATE patron_cards SET number = 'P-000001' WHERE patron_id = ?", second)
    html = _scan(client_as("volunteer"), None, "P-000001")
    assert "Numéro de lecteur" in html and "Carte" in html
    assert f'value="patron:{first}"' in html and f'value="card:{second}"' in html


def test_an_isbn_with_several_copies_requires_a_choice(settings, database, client_as):
    first, second = _copies(settings, database, 2)
    edition = _rows(settings, "SELECT edition_id FROM copies WHERE id = ?", first)[0][0]
    columns = {row[1] for row in _rows(settings, "PRAGMA table_info(edition_identifiers)")}
    extra = {"created_at": "x"} if "created_at" in columns else {}
    names = ", ".join(["edition_id", "scheme", "value", *extra])
    marks = ", ".join("?" for _ in range(3 + len(extra)))
    _execute(
        settings,
        f"INSERT INTO edition_identifiers ({names}) VALUES ({marks})",  # noqa: S608
        edition,
        "isbn13",
        "9782203001190",
        *extra.values(),
    )
    html = _scan(client_as("volunteer"), None, "978-2-203-00119-0")
    assert f'value="isbn:{first}"' in html and f'value="isbn:{second}"' in html


# --- Liste et validation (D6) ---------------------------------------------------------------------------------------
def test_a_full_loan_with_a_scanner(settings, database, client_as):
    first, second = _copies(settings, database, 2)
    patron = _adult(database, card="CARTE-1")
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, first))
    html = _scan(browser, html, "CARTE-1")
    html = _scan(browser, html, _code(settings, second))
    assert "disabled>Confirmer" not in html.replace("\n", "") and "Confirmer le prêt de 2 livres" in html
    done = browser.post("/comptoir/valider", data=_form(_state(html)))
    assert done.status_code == 200 and "Prêt enregistré" in done.text
    assert sorted(_rows(settings, "SELECT copy_id, patron_id FROM loans")) == [(first, patron), (second, patron)]
    assert not [
        item for item in _state(done.text) if item[0] in ("ex", "lecteur")
    ]  # le comptoir est prêt pour le suivant


def test_a_copy_scanned_twice_is_kept_once(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, copy_id))
    html = _scan(browser, html, _code(settings, copy_id))
    assert "déjà dans la liste" in html and _state(html).count(("ex", str(copy_id))) == 1


def test_a_copy_on_loan_is_refused_with_its_due_date_and_no_holder(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    _lend_direct(settings, database, _adult(database), copy_id)
    html = _scan(client_as("volunteer"), None, _code(settings, copy_id))
    assert "déjà en prêt" in html
    rest = _beyond_the_directory(html)
    assert "Durand" not in rest and "P-000001" not in rest
    assert not [item for item in _state(html) if item[0] == "ex"]


def test_a_copy_taken_between_two_screens_rolls_everything_back(settings, database, client_as):
    first, second = _copies(settings, database, 2)
    patron = _adult(database, card="CARTE-1")
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, first))
    html = _scan(browser, html, _code(settings, second))
    html = _scan(browser, html, "CARTE-1")
    _lend_direct(settings, database, _adult(database, "Marc", "Roy", "m@example.org"), second)
    response = browser.post("/comptoir/valider", data=_form(_state(html)))
    assert response.status_code == 200 and "déjà en prêt" in response.text
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE patron_id = ?", patron) == [(0,)]
    assert ("ex", str(first)) in _state(response.text)  # la liste réapparaît


def test_a_forged_validation_is_refused_by_the_service(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    child = _minor(database)
    response = client_as("volunteer").post(
        "/comptoir/valider", data={"lecteur": str(child), "cadre": "alone", "ex": str(copy_id)}
    )
    assert response.status_code == 200 and _rows(settings, "SELECT COUNT(*) FROM loans") == [(0,)]


def test_a_viewer_with_a_custom_lend_right_needs_patrons_view(settings, client_as):
    browser = client_as("viewer")
    assert browser.post("/comptoir/valider", data={}).status_code == 403


# --- Mineurs à l'écran (D1, U10) ------------------------------------------------------------------------------------
def test_the_two_rights_are_clearly_distinct_on_screen(settings, database, client_as):
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, alone=1)
    card = _card(settings, child)
    html = _scan(client_as("volunteer"), None, card)
    assert "Un adulte l’accompagne et emprunte pour lui" in html
    assert "L’enfant emprunte seul" in html and "Autorisé par" in html
    assert 'name="cadre" value="with_adult" checked' not in html  # jamais déduit


def test_only_authorized_adults_are_offered_and_one_is_preselected(settings, database, client_as):
    child, parent = _minor(database), _adult(database)
    stranger = _adult(database, "Marc", "Roy", "m@example.org")
    _link(settings, child, parent)
    _execute(
        settings,
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank, version,"
        " created_at, updated_at) VALUES (?, ?, 'parent', 0, 1, 2, 1, 'x', 'x')",
        child,
        stranger,
    )
    browser = client_as("volunteer")
    html = _scan(browser, None, _card(settings, child))
    response = browser.post("/comptoir/etat", data=_form([*_state(html), ("cadre", "with_adult")]))
    assert f'name="responsable" value="{parent}" checked' in response.text
    assert f'name="responsable" value="{stranger}"' not in response.text


def test_no_right_at_all_says_guardian_needed(settings, database, client_as):
    html = _scan(client_as("volunteer"), None, _card(settings, _minor(database)))
    assert "Responsable à renseigner" in html and "Aucun adulte habilité" in html


def test_child_alone_full_loan(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, can_borrow_for=0, alone=1)
    browser = client_as("volunteer")
    html = _scan(browser, None, _card(settings, child))
    html = _scan(browser, html, _code(settings, copy_id))
    response = browser.post("/comptoir/valider", data=_form([*_state(html), ("cadre", "alone")]))
    assert "Prêt enregistré" in response.text
    assert _rows(settings, "SELECT borrow_mode, responsible_id FROM loans") == [("alone", parent)]


# --- Prêts en cours au comptoir (P2) --------------------------------------------------------------------------------
def test_current_loans_show_four_fields_only(settings, database, client_as):
    first, _second = _copies(settings, database, 2)
    patron = _adult(database, card="CARTE-1")
    _execute(settings, "UPDATE patrons SET notes = 'observation confidentielle' WHERE id = ?", patron)
    lent = _lend_direct(settings, database, patron, first)
    html = _scan(client_as("volunteer"), None, "CARTE-1")
    due = _rows(settings, "SELECT due_on FROM loans WHERE id = ?", lent.loan_ids[0])[0][0]
    shown = f"{due[8:10]}/{due[5:7]}/{due[:4]}"
    assert "Prêts en cours" in html and "Le Lotus bleu" in html and _code(settings, first) in html and shown in html
    assert "Renouveler" in html and "En cours" in html
    for forbidden in ("observation confidentielle", EMAIL, "0612", "+33"):
        assert forbidden not in html


def test_finished_loans_are_not_shown(settings, database, client_as):
    (copy_id,) = _copies(settings, database, title="Ouvrage terminé")
    patron = _adult(database, card="CARTE-1")
    _lend_direct(settings, database, patron, copy_id)
    service.return_copy(database, copy_id, Actor(None, RIGHTS))
    html = _scan(client_as("volunteer"), None, "CARTE-1")
    assert "Prêts en cours" not in html and "desk-loans" not in html
    # Le livre reste proposé au catalogue du comptoir, où il est disponible : jamais comme un prêt de ce lecteur
    assert "Ouvrage terminé" not in re.sub(r'<div id="cp-livres".*?</ul>', "", html, flags=re.S)


def test_renew_from_the_counter(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    patron = _adult(database, card="CARTE-1")
    loan = _lend_direct(settings, database, patron, copy_id).loan_ids[0]
    browser = client_as("volunteer")
    html = _scan(browser, None, "CARTE-1")
    response = browser.post(f"/comptoir/renouveler/{loan}", data=_form([*_state(html), (f"version_{loan}", "1")]))
    assert response.status_code == 200
    assert _rows(settings, "SELECT renewals FROM loans") == [(1,)]


def test_renew_is_refused_for_a_reader_who_is_not_selected(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    other = _adult(database, "Marc", "Roy", "m@example.org")
    response = client_as("volunteer").post(f"/comptoir/renouveler/{loan}", data={"lecteur": str(other)})
    assert response.status_code == 403 and _rows(settings, "SELECT renewals FROM loans") == [(0,)]


def test_renew_is_refused_for_a_withdrawn_reader(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    patron = _adult(database)
    loan = _lend_direct(settings, database, patron, copy_id).loan_ids[0]
    _execute(settings, "UPDATE patrons SET status = 'archived' WHERE id = ?", patron)
    response = client_as("volunteer").post(f"/comptoir/renouveler/{loan}", data={"lecteur": str(patron)})
    assert response.status_code == 403


def test_without_override_a_volunteer_gets_no_override_controls(settings, database, client_as):
    html = client_as("volunteer").get("/comptoir").text
    assert "date_pret" not in html
    assert "date_pret" in client_as("librarian").get("/comptoir").text


# --- Retour rapide (D10, R25, R27) ----------------------------------------------------------------------------------
def _return(browser, code):
    return browser.post("/comptoir/retours/scan", data={"code": code}, headers={"HX-Request": "true"})


def test_return_page_access(client_as):
    assert client_as("volunteer").get("/comptoir/retours").status_code == 200
    assert client_as("viewer").get("/comptoir/retours").status_code == 403


def test_a_scan_returns_at_once_and_says_where_it_goes(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    _lend_direct(settings, database, _adult(database), copy_id)
    _execute(
        settings,
        "INSERT INTO locations (id, public_id, kind, name, path, depth, sort_order, is_active, version, created_at,"
        " updated_at, grid_width, grid_height)"
        " VALUES (50, 'l50', 'shelf', 'N', 'Salle A / Meuble 1', 1, 1, 1, 1, 'x', 'x', 1, 1)",
    )
    _execute(settings, "UPDATE copies SET location_id = 50, shelf_position = 7 WHERE id = ?", copy_id)
    response = _return(client_as("volunteer"), _code(settings, copy_id))
    assert "Salle A / Meuble 1" in response.text and "place" in response.text and "7" in response.text
    assert _rows(settings, "SELECT returned_at IS NOT NULL FROM loans") == [(1,)]


def test_a_volunteer_does_not_see_who_returned_a_librarian_does(settings, database, client_as):
    first, second = _copies(settings, database, 2)
    patron = _adult(database)
    _lend_direct(settings, database, patron, first)
    _lend_direct(settings, database, patron, second)
    assert "Durand" not in _return(client_as("volunteer"), _code(settings, first)).text
    assert "Anne Durand" in _return(client_as("librarian"), _code(settings, second)).text


def test_returning_a_copy_not_on_loan_is_no_error(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    response = _return(client_as("volunteer"), _code(settings, copy_id))
    assert response.status_code == 200 and "rien à rendre" in response.text


def test_an_unknown_code_at_return(client_as):
    assert "Code inconnu" in _return(client_as("volunteer"), "NOPE-1").text


def test_a_return_is_cancelled_from_its_line(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    browser = client_as("librarian")
    _return(browser, _code(settings, copy_id))
    response = browser.post(f"/comptoir/retours/{loan}/annuler", headers={"HX-Request": "true"})
    assert "prêt court à nouveau" in response.text or "motif" in response.text


def test_the_author_cancels_the_loan_just_made(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    _adult(database, card="CARTE-1")
    browser = client_as("volunteer")
    html = _scan(browser, None, _code(settings, copy_id))
    html = _scan(browser, html, "CARTE-1")
    done = browser.post("/comptoir/valider", data=_form(_state(html)))
    operation = _rows(settings, "SELECT id FROM loan_operations")[0][0]
    assert f"/comptoir/annuler/{operation}" in done.text
    assert "annulé" in browser.post(f"/comptoir/annuler/{operation}").text
    assert _rows(settings, "SELECT cancelled_at IS NOT NULL FROM loans") == [(1,)]


# --- Fiche d'un prêt et corrections ---------------------------------------------------------------------------------
@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 403), ("viewer", 403)])
def test_the_loan_sheet_requires_the_history_right(settings, database, client_as, role, status):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    assert client_as(role).get(f"/prets/{loan}").status_code == status


def test_the_loan_sheet_says_who_uses_and_who_answers(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent)
    request = LendRequest(child, (LendItem(copy_id),), rules.MODE_WITH_ADULT, parent)
    loan = service.lend(database, request, Actor(None, RIGHTS)).loan_ids[0]
    html = client_as("librarian").get(f"/prets/{loan}").text
    assert "Utilisé par" in html and "Léa Durand" in html and "Responsable" in html and "Anne Durand" in html
    assert EMAIL not in html


def test_a_withdrawn_reader_closes_the_loan_sheet(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    patron = _adult(database)
    loan = _lend_direct(settings, database, patron, copy_id).loan_ids[0]
    _execute(settings, "UPDATE patrons SET status = 'archived' WHERE id = ?", patron)
    assert client_as("librarian").get(f"/prets/{loan}").status_code == 200  # le bibliothécaire a patrons.archive
    custom = client_as("volunteer")
    assert custom.get(f"/prets/{loan}").status_code == 403


def test_a_volunteer_cannot_correct_through_the_address(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    browser = client_as("volunteer")
    for path in ("echeance", "date-retour", "annuler", "lecteur"):
        assert browser.post(f"/prets/{loan}/{path}", data={"echeance": "2027-05-01"}).status_code == 403


def test_a_librarian_corrects_the_due_date_with_a_reason(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    browser = client_as("librarian")
    refused = browser.post(f"/prets/{loan}/echeance", data={"echeance": "2030-05-01"})
    assert refused.status_code == 400
    response = browser.post(f"/prets/{loan}/echeance", data={"echeance": "2030-05-01", "motif": "wrong_due"})
    assert response.status_code == 303 and _rows(settings, "SELECT due_on FROM loans") == [("2030-05-01",)]


def test_a_wrong_reader_is_replaced_from_the_sheet(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    loan = _lend_direct(settings, database, _adult(database), copy_id).loan_ids[0]
    right = _adult(database, "Marc", "Roy", "m@example.org")
    response = client_as("librarian").post(
        f"/prets/{loan}/lecteur", data={"nouveau": str(right), "cadre": "self", "motif": "wrong_reader"}
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT patron_id FROM loans WHERE cancelled_at IS NULL") == [(right,)]


# --- Droits explicites (D1, D2, C15) --------------------------------------------------------------------------------
def test_the_right_to_borrow_alone_is_set_and_revoked_on_the_sheet(settings, database, client_as):
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, can_borrow_for=0)
    browser = client_as("librarian")
    page = browser.get(f"/lecteurs/{child}").text
    assert "L’enfant peut emprunter seul" in page
    data = {"relation": "parent", "contact": "1", "seul": "1", "rang": "1", "version": "1"}
    assert browser.post(f"/lecteurs/{child}/responsables/{parent}", data=data).status_code == 303
    assert _rows(settings, "SELECT can_borrow_alone, can_borrow_for FROM guardianships") == [(1, 0)]
    assert "Enfant autorisé seul" in browser.get(f"/lecteurs/{child}").text
    entry = _rows(settings, "SELECT details FROM audit_log WHERE action = 'patron.guardian_updated'")[-1][0]
    assert '"seul": true' in entry and "Durand" not in entry
    del data["seul"]
    data["version"] = "2"
    browser.post(f"/lecteurs/{child}/responsables/{parent}", data=data)
    assert _rows(settings, "SELECT can_borrow_alone FROM guardianships") == [(0,)]


def test_a_volunteer_cannot_change_the_right_after_registration(settings, database, client_as):
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent)
    data = {"relation": "parent", "contact": "1", "seul": "1", "rang": "1", "version": "1"}
    assert client_as("volunteer").post(f"/lecteurs/{child}/responsables/{parent}", data=data).status_code == 403


def test_the_family_form_does_not_offer_borrowing_alone(client_as):
    assert "peut emprunter seul" not in client_as("librarian").get("/foyers/nouveau").text


def test_the_export_carries_the_right(settings, database, client_as):
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, alone=1)
    response = client_as("admin").get(f"/lecteurs/{child}/export.json")
    assert response.status_code == 200 and '"peut_emprunter_seul":true' in response.text


def test_the_structure_right_is_explicit_and_not_inferred(settings, database, client_as):
    adult = _adult(database)
    _execute(
        settings,
        "INSERT INTO households (id, public_id, kind, name, name_norm, version, created_at, updated_at)"
        " VALUES (9, 'h9', 'structure', 'École', 'ecole', 1, 'x', 'x')",
    )
    _execute(
        settings,
        "INSERT INTO household_members (household_id, patron_id, role, joined_on) VALUES (9, ?, 'referent', 'x')",
        adult,
    )
    browser = client_as("librarian")
    assert _rows(settings, "SELECT can_borrow_for_structure FROM household_members") == [(0,)]
    assert browser.post(f"/foyers/9/membres/{adult}/structure", data={"structure": "1"}).status_code == 303
    assert _rows(settings, "SELECT can_borrow_for_structure FROM household_members") == [(1,)]
    assert "Peut emprunter pour la structure" in browser.get("/foyers/9").text
    assert client_as("volunteer").post(f"/foyers/9/membres/{adult}/structure", data={}).status_code == 403


def test_the_counter_shows_the_count_and_the_limit_and_overdues(settings, database, client_as):
    (copy_id,) = _copies(settings, database)
    patron = _adult(database, card="CARTE-1")
    _lend_direct(settings, database, patron, copy_id)
    _execute(settings, "UPDATE loans SET due_on = '2020-01-01'")
    html = _scan(client_as("volunteer"), None, "CARTE-1")
    assert "1 / 5 prêts" in html and "1 en retard" in html


# --- Opérations distinctes et soumissions répétées (correctif de PC4) ------------------------------------------------
def _ready(settings, database, browser):
    (copy_id,) = _copies(settings, database, title="Opérations distinctes")
    patron = _adult(database, "Odile", "Distincte", "odile@example.org", card="CARTE-OD")
    html = _scan(browser, None, _code(settings, copy_id))
    html = _scan(browser, html, "CARTE-OD")
    return copy_id, patron, html


def _counts(settings):
    return _rows(settings, "SELECT (SELECT COUNT(*) FROM loans), (SELECT COUNT(*) FROM loan_operations)")[0]


@pytest.mark.parametrize("extra", [{}, {"valider": "1"}, {"action": "valider"}, {"formaction": "/comptoir/valider"}])
def test_scan_choice_and_state_requests_never_record_anything(settings, database, client_as, extra):
    browser = client_as("volunteer")
    _copy_id, _patron, html = _ready(settings, database, browser)
    for path in ("/comptoir/scan", "/comptoir/choix", "/comptoir/etat"):
        browser.post(path, data=_form([*_state(html), ("code", "INCONNU-1"), *extra.items()]))
        browser.post(path, data=_form([*_state(html), *extra.items()]))
    assert _counts(settings) == (0, 0)


def test_a_request_carrying_a_scan_never_validates(settings, database, client_as):
    browser = client_as("volunteer")
    copy_id, _patron, html = _ready(settings, database, browser)
    response = browser.post("/comptoir/valider", data=_form([*_state(html), ("code", _code(settings, copy_id))]))
    assert "Un scan était en cours" in response.text and _counts(settings) == (0, 0)


def test_a_request_carrying_a_scan_never_cancels_nor_renews(settings, database, client_as):
    browser = client_as("volunteer")
    copy_id, patron, html = _ready(settings, database, browser)
    browser.post("/comptoir/valider", data=_form(_state(html)))
    operation, loan = _rows(settings, "SELECT operation_id, id FROM loans")[0]
    scan = {"code": "EX-999999", "lecteur": str(patron), f"version_{loan}": "1"}
    assert "Un scan était en cours" in browser.post(f"/comptoir/annuler/{operation}", data=scan).text
    assert "Un scan était en cours" in browser.post(f"/comptoir/renouveler/{loan}", data=scan).text
    service.return_copy(database, copy_id, Actor(None, RIGHTS))
    hx = {"HX-Request": "true"}
    assert "Un scan était en cours" in browser.post(f"/comptoir/retours/{loan}/annuler", data=scan, headers=hx).text
    assert _rows(settings, "SELECT cancelled_at, renewals, returned_at IS NOT NULL FROM loans") == [(None, 0, 1)]


def test_a_repeated_validation_records_once(settings, database, client_as):
    browser = client_as("volunteer")
    _copy_id, _patron, html = _ready(settings, database, browser)
    data = _form(_state(html))
    assert data["jeton"] and len(data["jeton"][0]) >= 16
    first = browser.post("/comptoir/valider", data=data)
    second = browser.post("/comptoir/valider", data=data)
    assert "Prêt enregistré" in first.text and "déjà enregistré" in second.text
    assert _counts(settings) == (1, 1)


def test_a_token_belongs_to_one_account(settings, database, client_as):
    browser, other = client_as("volunteer"), client_as("volunteer")
    _copy_id, _patron, html = _ready(settings, database, browser)
    data = _form(_state(html))
    browser.post("/comptoir/valider", data=data)
    replay = other.post("/comptoir/valider", data=data)
    assert "déjà enregistré" not in replay.text and "déjà en prêt" in replay.text  # la base refuse, rien n'est écrit
    assert _counts(settings) == (1, 1)


def test_a_refused_validation_can_be_retried(settings, database, client_as):
    browser = client_as("volunteer")
    copy_id, _patron, html = _ready(settings, database, browser)
    _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", copy_id)
    data = _form(_state(html))
    assert "réparation" in browser.post("/comptoir/valider", data=data).text
    _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    assert "Prêt enregistré" in browser.post("/comptoir/valider", data=data).text  # le jeton refusé était libéré
    assert _counts(settings) == (1, 1)


def test_repeated_renewal_cancellation_and_return_act_once(settings, database, client_as):
    browser = client_as("volunteer")
    copy_id, patron, html = _ready(settings, database, browser)
    browser.post("/comptoir/valider", data=_form(_state(html)))
    operation, loan = _rows(settings, "SELECT operation_id, id FROM loans")[0]
    renew = {"lecteur": str(patron), f"version_{loan}": "1"}
    browser.post(f"/comptoir/renouveler/{loan}", data=renew)
    browser.post(f"/comptoir/renouveler/{loan}", data=renew)  # même version : refusé (R12)
    assert _rows(settings, "SELECT renewals FROM loans") == [(1,)]
    hx = {"HX-Request": "true"}
    browser.post("/comptoir/retours/scan", data={"code": _code(settings, copy_id)}, headers=hx)
    again = browser.post("/comptoir/retours/scan", data={"code": _code(settings, copy_id)}, headers=hx)
    assert "rien à rendre" in again.text
    assert _rows(settings, "SELECT COUNT(*) FROM loan_operations WHERE kind = 'return'") == [(1,)]
    browser.post(f"/comptoir/retours/{loan}/annuler", headers=hx)
    second = browser.post(f"/comptoir/retours/{loan}/annuler", headers=hx)
    assert "pas de retour à annuler" in second.text
    assert _rows(settings, "SELECT COUNT(*) FROM loan_events WHERE kind = 'cancelled'") == [(1,)]
    assert operation


# --- Échéance : seul un choix de la personne compte (vu en recette sur DEV) -------------------------------------------
def _with(html, **changes):
    pairs = [item for item in _state(html) if item[0] not in changes]
    return _form([*pairs, *changes.items()])


def test_backdating_recomputes_the_due_date_unless_it_was_chosen(settings, database, client_as):
    from datetime import date, timedelta

    browser = client_as("librarian")
    copy_id, _patron, html = _ready(settings, database, browser)
    today = date.today()
    first_due = (today + timedelta(days=28)).isoformat()
    assert f'value="{first_due}"' in html
    loaned = (today - timedelta(days=35)).isoformat()
    later = browser.post("/comptoir/etat", data=_with(html, date_pret=loaned)).text
    expected = (today - timedelta(days=7)).isoformat()
    assert f'name="retour_{copy_id}" value="{expected}"' in later  # recalculée, pas gardée
    chosen = (today - timedelta(days=10)).isoformat()
    kept = browser.post("/comptoir/etat", data=_with(later, date_pret=loaned, **{f"retour_{copy_id}": chosen})).text
    assert f'name="retour_{copy_id}" value="{chosen}"' in kept  # une date changée par la personne reste
    done = browser.post(
        "/comptoir/valider", data=_with(later, date_pret=loaned, motif="late_entry")
    )
    assert "Prêt enregistré" in done.text
    assert _rows(settings, "SELECT due_on FROM loans") == [(expected,)]
    events = _rows(settings, "SELECT details FROM loan_events WHERE kind = 'override'")
    assert events and '"R16"' in events[0][0] and '"R17"' not in events[0][0]  # aucune prolongation non voulue


def test_reader_marks_never_start_with_a_separator(settings, database, client_as):
    _adult(database, card="CARTE-SANS")  # ni naissance ni foyer ni responsable
    html = _scan(client_as("volunteer"), None, "CARTE-SANS")
    assert '<span class="faint small block"> ·' not in html and '<span class="faint small block">·' not in html
