# SPDX-License-Identifier: EUPL-1.2
"""Exemplaires à l'écran : fiche, statuts, déplacement sans JavaScript, notice, catalogue, permissions par rôle."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _edition(settings, work):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def place(database):
    room = holdings.create_location(database, "room", None, LocationInput("Salle jeunesse"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble BD 1"), None)
    shelves = [
        holdings.create_location(database, "shelf", furniture, LocationInput(f"Étagère {n}", capacity="2"), None)
        for n in (1, 2)
    ]
    return SimpleNamespace(room=room, furniture=furniture, shelves=shelves)


def _copies(settings, database, title="Le Lotus bleu", count=1, **options):
    work = make_notice(settings, title, **options)
    return work, catalogue.add_copies(database, _edition(settings, work), count, "", None)


def _form(barcode, **changes):
    return {"code_barres": barcode, "inventaire": "", "cote": "BD TIN 1", "version": "", **changes}


# --- Permissions, pour chaque rôle --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "view", "edit"),
    [("admin", 200, 303), ("librarian", 200, 303), ("volunteer", 200, 303), ("viewer", 200, 403)],
)
def test_copy_sheet_by_role(client_as, settings, database, role, view, edit):
    _work, (copy,) = _copies(settings, database)
    browser = client_as(role)
    assert browser.get(f"/exemplaires/{copy}").status_code == view
    assert browser.post(f"/exemplaires/{copy}", data=_form("EX-000001", notes="Vu")).status_code == edit


@pytest.mark.parametrize(
    ("role", "action", "expected"),
    [
        ("volunteer", "repair", 303),
        ("volunteer", "lost", 403),
        ("volunteer", "withdraw", 403),
        ("librarian", "lost", 303),
        ("librarian", "withdraw", 303),
        ("admin", "withdraw", 303),
        ("viewer", "repair", 403),
    ],
)
def test_status_permissions_by_role(client_as, settings, database, role, action, expected):
    _work, (copy,) = _copies(settings, database)
    response = client_as(role).post(f"/exemplaires/{copy}/statut", data={"action": action, "motif": "Abîmé"})
    assert response.status_code == expected


@pytest.mark.parametrize(("role", "expected"), [("volunteer", 200), ("librarian", 200), ("viewer", 403)])
def test_move_page_by_role(client_as, settings, database, place, role, expected):
    _work, (copy,) = _copies(settings, database)
    assert client_as(role).get(f"/exemplaires/{copy}/deplacer").status_code == expected


def test_anonymous_is_sent_to_login(client, settings, database):
    _work, (copy,) = _copies(settings, database)
    assert client.get(f"/exemplaires/{copy}").status_code == 303


def test_viewer_sees_no_write_control(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    page = client_as("viewer").get(f"/exemplaires/{copy}").text
    assert 'name="code_barres"' not in page and "Déplacer" not in page and "Retirer du fonds" not in page
    assert "EX-000001" in page and "Fiche de l’exemplaire" in page


def test_volunteer_is_not_offered_what_is_refused(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    page = client_as("volunteer").get(f"/exemplaires/{copy}").text
    assert "Envoyer en réparation" in page
    assert "Retirer du fonds" not in page and "Déclarer perdu" not in page


def test_unknown_copy_and_action(client_as):
    browser = client_as("admin")
    assert browser.get("/exemplaires/999").status_code == 404
    assert browser.get("/exemplaires/999/deplacer").status_code == 404
    assert browser.post("/exemplaires/999/statut", data={"action": "nope"}).status_code == 400


# --- Fiche --------------------------------------------------------------------------------------------------------
def test_edit_the_copy_sheet(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    browser = client_as("volunteer")
    response = browser.post(
        f"/exemplaires/{copy}",
        data=_form(
            "cl 2026 42",
            inventaire="INV-1",
            cote="bd tin 9",
            etat="worn",
            mode_acquisition="donation",
            date_acquisition="03/2019",
            prix="12,5",
            sur_place="1",
            version="1",
        ),
    )
    assert response.status_code == 303
    page = browser.get(f"/exemplaires/{copy}").text
    for fragment in ("CL202642", "INV-1", "BD TIN 9", "2019-03", "12,50", "Consultation sur place"):
        assert fragment in page, fragment


def test_form_errors_are_shown_next_to_the_fields(client_as, settings, database):
    _work, (_first, second) = _copies(settings, database, count=2)
    page = client_as("admin").post(f"/exemplaires/{second}", data=_form("EX-000001", prix="douze"))
    assert page.status_code == 400
    assert "Ce code est déjà attribué" in page.text and "(EX-000001)" in page.text
    assert "Écrivez le prix en euros" in page.text
    assert 'value="douze"' in page.text  # la saisie n'est pas perdue


def test_an_outdated_sheet_is_refused(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    browser = client_as("admin")
    browser.post(f"/exemplaires/{copy}", data=_form("EX-000001", notes="Premier", version="1"))
    page = browser.post(f"/exemplaires/{copy}", data=_form("EX-000001", notes="Second", version="1"))
    assert page.status_code == 409
    assert "vient d’être modifié" in page.text and 'value="EX-000001"' in page.text


# --- Statuts ------------------------------------------------------------------------------------------------------
def test_withdrawal_needs_a_reason(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    page = client_as("librarian").post(f"/exemplaires/{copy}/statut", data={"action": "withdraw"})
    assert page.status_code == 400
    assert "Indiquez le motif du retrait." in page.text


def test_withdrawal_frees_the_place_and_keeps_the_history(client_as, settings, database, place):
    _work, copies = _copies(settings, database, count=2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("librarian")
    response = browser.post(f"/exemplaires/{copies[0]}/statut", data={"action": "withdraw", "motif": "Trop abîmé"})
    assert response.status_code == 303
    page = browser.get(f"/exemplaires/{copies[0]}").text
    assert "Retiré" in page and "Trop abîmé" in page and "n’a plus de place" in page
    assert "Rangé : Salle jeunesse / Meuble BD 1 / Étagère 1" in page  # l'historique reste
    assert "Après" not in browser.get(f"/exemplaires/{copies[1]}").text


def test_impossible_status_is_explained(client_as, settings, database):
    _work, (copy,) = _copies(settings, database)
    page = client_as("admin").post(f"/exemplaires/{copy}/statut", data={"action": "found"})
    assert page.status_code == 400
    assert "Impossible depuis le statut" in page.text


# --- Déplacer, sans JavaScript ------------------------------------------------------------------------------------
def test_move_without_javascript(client_as, settings, database, place):
    _work, copies = _copies(settings, database, count=2)
    holdings.shelve(database, [copies[0]], place.shelves[0], user_id=None)
    browser = client_as("volunteer")
    page = browser.get(f"/exemplaires/{copies[1]}/deplacer").text
    assert "Salle jeunesse / Meuble BD 1 / Étagère 1 (1/2)" in page
    data = {"emplacement": place.shelves[0], "rang": "1", "version": "1"}
    assert browser.post(f"/exemplaires/{copies[1]}/deplacer", data=data).status_code == 303
    sheet = browser.get(f"/exemplaires/{copies[0]}").text
    assert "position 2" in sheet and "Après « Le Lotus bleu »" in sheet


def test_move_is_refused_with_an_explanation(client_as, settings, database, place):
    _work, (copy,) = _copies(settings, database)
    browser = client_as("admin")
    assert browser.post(f"/exemplaires/{copy}/deplacer", data={}).status_code == 400
    bad_rank = browser.post(f"/exemplaires/{copy}/deplacer", data={"emplacement": place.shelves[0], "rang": "abc"})
    assert bad_rank.status_code == 400 and "Indiquez un rang" in bad_rank.text
    furniture = browser.post(f"/exemplaires/{copy}/deplacer", data={"emplacement": place.furniture})
    assert furniture.status_code == 400 and "se range sur une étagère" in furniture.text


def test_outdated_move_is_refused(client_as, settings, database, place):
    _work, (copy,) = _copies(settings, database)
    browser = client_as("admin")
    browser.post(f"/exemplaires/{copy}/deplacer", data={"emplacement": place.shelves[0], "version": "1"})
    page = browser.post(f"/exemplaires/{copy}/deplacer", data={"emplacement": place.shelves[1], "version": "1"})
    assert page.status_code == 409
    assert "Rien n’a été enregistré" in page.text


def test_full_shelf_is_signalled_not_refused(client_as, settings, database, place):
    _work, copies = _copies(settings, database, count=3)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    response = client_as("admin").post(f"/exemplaires/{copies[2]}/deplacer", data={"emplacement": place.shelves[0]})
    assert response.status_code == 303
    assert response.cookies.get("biblyo_flash") == "copy_moved_over"


def test_lost_copy_cannot_be_moved(client_as, settings, database, place):
    _work, (copy,) = _copies(settings, database)
    holdings.change_status(database, copy, "lost", user_id=None)
    browser = client_as("admin")
    assert "déclarez-le d’abord retrouvé" in browser.get(f"/exemplaires/{copy}/deplacer").text
    page = browser.post(f"/exemplaires/{copy}/deplacer", data={"emplacement": place.shelves[0]})
    assert page.status_code == 400


def test_unshelve_from_the_sheet(client_as, settings, database, place):
    _work, (copy,) = _copies(settings, database)
    holdings.shelve(database, [copy], place.shelves[0], user_id=None)
    browser = client_as("volunteer")
    assert browser.post(f"/exemplaires/{copy}/sans-place", data={}).status_code == 303
    assert "Non rangé" in browser.get(f"/exemplaires/{copy}").text


# --- Notice et catalogue ------------------------------------------------------------------------------------------
def test_notice_tells_where_to_find_each_copy(client_as, settings, database, place):
    work, copies = _copies(settings, database, "Le Lotus bleu", 3, series="Tintin", volume="5")
    _other, (neighbour,) = _copies(settings, database, "Le Voisin")
    holdings.shelve(database, [neighbour, copies[0]], place.shelves[0], user_id=None)
    browser = client_as("viewer")
    overview = browser.get(f"/catalogue/notices/{work}").text
    assert "Salle jeunesse / Meuble BD 1 / Étagère 1" in overview and "position 2" in overview
    assert "Après « Le Voisin »" in overview
    assert "Non rangé" in overview and "lot L2" not in overview
    tab = browser.get(f"/catalogue/notices/{work}?onglet=exemplaires").text
    assert "BD TIN 1" in tab and "En place" in tab and "En préparation" in tab


def test_notice_offers_to_move_and_delete_only_when_it_makes_sense(client_as, settings, database, place):
    work, copies = _copies(settings, database, count=2)
    holdings.shelve(database, [copies[0]], place.shelves[0], user_id=None)
    tab = client_as("volunteer").get(f"/catalogue/notices/{work}?onglet=exemplaires").text
    assert f"/exemplaires/{copies[0]}/deplacer" in tab
    assert f'data-open-dialog="suppr-copie-{copies[0]}"' not in tab
    assert f'data-open-dialog="suppr-copie-{copies[1]}"' in tab
    assert "/exemplaires/" not in client_as("viewer", username="v2").get(f"/catalogue/notices/{work}").text.split(
        "hero-acts"
    )[1].split("</div>")[0]


def test_single_copy_notice_moves_directly(client_as, settings, database):
    work, (copy,) = _copies(settings, database)
    page = client_as("volunteer").get(f"/catalogue/notices/{work}").text
    assert f'href="/exemplaires/{copy}/deplacer"' in page


def test_catalogue_list_shows_call_number_and_place(client_as, settings, database, place):
    _work, copies = _copies(settings, database, "Le Lotus bleu", 2, series="Tintin", volume="5")
    holdings.shelve(database, [copies[0]], place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[1]], place.shelves[1], user_id=None)
    page = client_as("viewer").get("/catalogue?view=list").text
    assert "BD TIN 1" in page and "Salle jeunesse / Meuble BD 1 / Étagère 1 (+1)" in page


def test_catalogue_filters_by_place(client_as, settings, database, place):
    _shelved, (one,) = _copies(settings, database, "Rangé")
    _loose, _copy = _copies(settings, database, "En attente")
    _gone, (lost,) = _copies(settings, database, "Disparu")
    holdings.shelve(database, [one], place.shelves[0], user_id=None)
    holdings.change_status(database, lost, "lost", user_id=None)
    browser = client_as("viewer")

    def titles(query):
        page = browser.get(f"/catalogue?view=list&emplacement={query}").text
        return {title for title in ("Rangé", "En attente", "Disparu") if f">{title}</a>" in page}

    assert titles(str(place.room)) == {"Rangé"}  # descendants compris
    assert titles(str(place.shelves[0])) == {"Rangé"}
    assert titles(str(place.shelves[1])) == set()
    assert titles("non-ranges") == {"En attente"}  # un exemplaire perdu n'est pas « non rangé »
    assert titles("n'importe-quoi") == {"Rangé", "En attente", "Disparu"}  # valeur inconnue ignorée


def test_search_finds_a_call_number(client_as, settings, database):
    _work, _copy_ids = _copies(settings, database, "Le Lotus bleu", series="Tintin", volume="5")
    page = client_as("viewer").get("/catalogue?view=list&q=cote:BD TIN 1").text
    assert ">Le Lotus bleu</a>" in page


# --- Suivi UX de L1 : U1 et U3 ------------------------------------------------------------------------------------
def test_sparse_notice_has_a_compact_header_without_empty_facts(client_as, settings):
    sparse = make_notice(settings, "Presque rien")
    full = make_notice(settings, "Complet", authors=("Hergé",), series="Tintin", volume="1", publisher="Casterman")
    browser = client_as("viewer")
    page = browser.get(f"/catalogue/notices/{sparse}").text
    assert "notice-hero is-sparse" in page and ">Éditeur<" not in page
    assert "is-sparse" not in browser.get(f"/catalogue/notices/{full}").text


def test_homonyms_are_flagged(client_as, settings):
    from biblyo.core.db import Database as Db
    from biblyo.modules.catalogue.models import Agent

    make_notice(settings, "Premier", authors=("Anne Martin",))
    make_notice(settings, "Autre auteur", authors=("Paul Durand",))
    database = Db(settings.database_path)
    with database.session(write=True) as session:
        twin = session.query(Agent).filter(Agent.display_name == "Anne Martin").one()
        session.add(
            Agent(
                display_name=twin.display_name,
                sort_name=twin.sort_name,
                name_norm=twin.name_norm,
                created_at=twin.created_at,
            )
        )
    database.dispose()
    page = client_as("viewer").get("/catalogue/contributeurs").text
    assert page.count("Homonyme") == 2 and "dates inconnues" in page
    assert page.index("Paul Durand") > 0 and "Homonyme" not in page.split("Paul Durand")[1].split("</div>")[0]


def test_cover_upload_is_labelled(client_as, settings):
    """L'onglet « Éditions » mène à la page Couverture de chaque édition (conception 17 § 3), qui nomme son champ."""
    work = make_notice(settings)
    browser = client_as("admin")
    page = browser.get(f"/catalogue/notices/{work}?onglet=editions").text
    assert 'aria-label="Ajouter une couverture à cette édition"' in page
    cover_page = browser.get(f"/catalogue/notices/{work}/couverture").text
    assert '<label for="couverture">' in cover_page and "Choisir une image" in cover_page
