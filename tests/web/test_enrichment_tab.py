# SPDX-License-Identifier: EUPL-1.2
"""Onglet « Enrichissement » de la fiche, annulation à l'écran, ISBN proposés, réglages des sources."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from tests.factories import make_notice
from tests.web.test_enrichment_screen import (
    PETIT_PRINCE,
    _edition,
    _notice,
    _proposal_id,
    _rows,
    _search,
    install_network,
)

ROLES = ("admin", "librarian", "volunteer", "viewer")


@pytest.fixture
def network(app, settings):
    return install_network(app, settings)


def _applied(browser, work, fields, **extra) -> None:
    _, html = _search(browser, work)
    path = f"/catalogue/notices/{work}/enrichir/propositions/{_proposal_id(html)}"
    response = browser.post(path, data={"action": "appliquer", "champ": list(fields), **extra})
    assert response.status_code == 303


def _tab(browser, work) -> str:
    response = browser.get(f"/catalogue/notices/{work}?onglet=enrichissement")
    assert response.status_code == 200
    return response.text


def _operation(html: str) -> str:
    return re.search(r"/enrichir/operations/([^/]+)/annuler", html).group(1)


# --- Onglet -----------------------------------------------------------------------------------------------------------
def test_the_tab_shows_provenance_history_and_credits(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _applied(browser, work, ["year", "pages", "cover"])
    html = _tab(browser, work)
    assert "Provenance des données" in html and "ark:/12148/cb41023439w" in html
    assert "Année" in html and "Pages" in html and "tel que repris" in html
    assert "service Couvertures" in html and "Notice de référence" in html
    assert "Historique des enrichissements" in html and "Annuler cet enrichissement" in html
    assert "Accepté :" in html and "Refusé :" in html
    assert "Licence Ouverte" in html


@pytest.mark.parametrize("role", ROLES)
def test_every_role_reads_the_tab_but_only_reviewers_act(client_as, settings, network, role):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    _applied(client_as("admin"), work, ["year"])
    html = _tab(client_as(role), work)
    assert "Provenance des données" in html
    assert ("Annuler cet enrichissement" in html) == (role in ("admin", "librarian"))


def test_a_hand_change_is_marked_in_the_provenance(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _applied(browser, work, ["year"])
    view = _notice(settings, work)
    catalogue.update_edition(
        Database(settings.database_path),
        view.primary.id,
        catalogue.EditionInput(isbn=PETIT_PRINCE, year="2008"),
        view.primary.version,
        None,
    )
    assert "modifié depuis" in _tab(browser, work)


def test_the_cover_credit_shows_in_the_zoom(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _applied(browser, work, ["cover"])
    page = browser.get(f"/catalogue/notices/{work}").text
    assert "Couverture : Bibliothèque nationale de France, service Couvertures" in page


# --- Annulation à l'écran ---------------------------------------------------------------------------------------------
def test_undo_without_conflict_restores_at_once(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE, publisher="Galimard")
    browser = client_as("librarian")
    _applied(browser, work, ["year", "pages", "publisher"])
    assert _notice(settings, work).primary.year == 2007
    response = browser.post(f"/catalogue/notices/{work}/enrichir/operations/{_operation(_tab(browser, work))}/annuler")
    assert response.status_code == 303
    view = _notice(settings, work)
    assert (view.primary.year, view.primary.pages, view.primary.publisher.label) == (None, None, "Galimard")
    assert "valeurs d’avant sont rétablies" in browser.get(response.headers["location"]).text
    assert "Annuler cet enrichissement" not in _tab(browser, work)


def test_undo_with_a_later_change_asks_field_by_field(client_as, settings, network):
    """V18 à l'écran : aucune restauration aveugle."""
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _applied(browser, work, ["year", "pages"])
    view = _notice(settings, work)
    catalogue.update_edition(
        Database(settings.database_path),
        view.primary.id,
        catalogue.EditionInput(isbn=PETIT_PRINCE, year="2010", pages="113"),
        view.primary.version,
        None,
    )
    operation = _operation(_tab(browser, work))
    path = f"/catalogue/notices/{work}/enrichir/operations/{operation}/annuler"
    page = browser.post(path)
    assert page.status_code == 200
    assert "Champs modifiés depuis" in page.text and "2010" in page.text and "2007" in page.text
    assert "Déjà rétablis, car inchangés depuis : Pages" in page.text  # 113 écrit, 113 actuel : rétabli sans conflit
    view = _notice(settings, work)
    assert (view.primary.year, view.primary.pages) == (2010, None)  # l'année modifiée attend un choix
    keep = browser.post(path, data={"choix": "1"})
    assert keep.status_code == 303 and _notice(settings, work).primary.year == 2010
    force = browser.post(path, data={"choix": "1", "forcer": ["year"]})
    assert force.status_code == 303 and _notice(settings, work).primary.year is None


def test_undo_of_another_notice_is_refused(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    other = make_notice(settings, "Autre")
    browser = client_as("librarian")
    _applied(browser, work, ["year"])
    operation = _operation(_tab(browser, work))
    assert browser.post(f"/catalogue/notices/{other}/enrichir/operations/{operation}/annuler").status_code == 404
    assert browser.post(f"/catalogue/notices/{work}/enrichir/operations/inconnue/annuler").status_code == 404
    assert (
        client_as("volunteer").post(f"/catalogue/notices/{work}/enrichir/operations/{operation}/annuler").status_code
        == 403
    )
    assert _notice(settings, work).primary.year == 2007


def test_undo_restores_the_previous_cover(client_as, settings, network):
    """La couverture téléversée (bleue) est remplacée par celle de la BnF (rouge), puis rétablie par l'annulation."""
    import io

    from PIL import Image

    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    buffer = io.BytesIO()
    Image.new("RGB", (300, 450), (10, 10, 200)).save(buffer, "JPEG")
    database = Database(settings.database_path)
    catalogue.set_cover(database, _edition(settings, work), buffer.getvalue(), settings.media_dir, None)
    database.dispose()
    before = _notice(settings, work).primary.cover.sha256
    browser = client_as("librarian")
    _applied(browser, work, ["cover"], couverture_remplacer="1")
    assert _notice(settings, work).primary.cover.sha256 != before
    browser.post(f"/catalogue/notices/{work}/enrichir/operations/{_operation(_tab(browser, work))}/annuler")
    assert _notice(settings, work).primary.cover.sha256 == before


# --- ISBN proposés ----------------------------------------------------------------------------------------------------
def _proposed_isbn(client_as, settings, network) -> tuple[int, object]:
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2015"
    )
    browser = client_as("librarian")
    _, html = _search(browser, work, isbn="")
    path = f"/catalogue/notices/{work}/enrichir/propositions/{_proposal_id(html)}"
    browser.post(path, data={"action": "appliquer", "champ": ["isbn"]})
    return work, browser


def test_a_proposed_isbn_waits_in_the_tab_and_can_be_confirmed(client_as, settings, network):
    work, browser = _proposed_isbn(client_as, settings, network)
    assert _notice(settings, work).primary.isbn13 == ()
    html = _tab(browser, work)
    assert "ISBN proposés et rejetés" in html and PETIT_PRINCE in html and "proposé" in html
    assert "Contredit :" in html  # l'année diffère
    candidate = re.search(r"/enrichir/isbn/(\d+)", html).group(1)
    assert (
        client_as("volunteer")
        .post(f"/catalogue/notices/{work}/enrichir/isbn/{candidate}", data={"decision": "confirmer"})
        .status_code
        == 403
    )
    response = browser.post(f"/catalogue/notices/{work}/enrichir/isbn/{candidate}", data={"decision": "confirmer"})
    assert response.status_code == 303
    assert _notice(settings, work).primary.isbn13 == (PETIT_PRINCE,)
    assert _rows(settings, "SELECT status FROM edition_isbn_candidates") == [("confirmed",)]
    assert "il entre dans la recherche" in browser.get(response.headers["location"]).text


def test_a_proposed_isbn_can_be_rejected(client_as, settings, network):
    work, browser = _proposed_isbn(client_as, settings, network)
    candidate = re.search(r"/enrichir/isbn/(\d+)", _tab(browser, work)).group(1)
    browser.post(f"/catalogue/notices/{work}/enrichir/isbn/{candidate}", data={"decision": "rejeter"})
    assert _rows(settings, "SELECT status FROM edition_isbn_candidates") == [("rejected",)]
    assert _notice(settings, work).primary.isbn13 == ()
    assert (
        browser.post(f"/catalogue/notices/{work}/enrichir/isbn/{candidate}", data={"decision": "confirmer"}).status_code
        == 303
    )
    assert _notice(settings, work).primary.isbn13 == ()  # une décision prise ne se rejoue pas


def test_a_proposed_isbn_never_reaches_the_search(client_as, settings, network):
    work, browser = _proposed_isbn(client_as, settings, network)
    page = browser.get(f"/catalogue?q={PETIT_PRINCE}").text
    assert f"/catalogue/notices/{work}" not in page


# --- Réglages des sources ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
def test_sources_settings_need_their_permission(client_as, role, network):
    browser = client_as(role)
    expected = 200 if role in ("admin", "librarian") else 403
    assert browser.get("/parametres/enrichissement").status_code == expected


def _settings_form(**overrides):
    data = {
        "source": ["bnf", "sudoc", "openlibrary"],
        "bnf_active": "1",
        "bnf_ordre": "1",
        "bnf_intervalle": "1000",
        "bnf_lot": "5000",
        "bnf_delai": "10000",
        "bnf_couvertures": "1",
        "sudoc_active": "1",
        "sudoc_ordre": "2",
        "sudoc_intervalle": "1000",
        "sudoc_lot": "2000",
        "sudoc_delai": "15000",
        "openlibrary_ordre": "3",
        "openlibrary_intervalle": "1500",
        "openlibrary_lot": "3000",
        "openlibrary_delai": "15000",
        "openlibrary_couvertures": "1",
        "enregistrer": "1",
    }
    data.update(overrides)
    return data


def test_sources_can_be_reordered_disabled_and_slowed(client_as, settings, network):
    browser = client_as("admin")
    response = browser.post(
        "/admin/sources",
        data=_settings_form(bnf_ordre="2", sudoc_ordre="1", sudoc_active="", bnf_intervalle="3000", bnf_couvertures=""),
    )
    assert response.status_code == 303
    assert _rows(
        settings,
        "SELECT code, priority, is_enabled, interval_ms, covers_enabled FROM metadata_sources ORDER BY priority",
    ) == [
        ("sudoc", 1, 0, 1000, 0),
        ("bnf", 2, 1, 3000, 0),
        ("openlibrary", 3, 0, 1500, 1),  # livrée désactivée (révision 0010)
    ]
    assert _rows(settings, "SELECT COUNT(*) FROM audit_log WHERE action = 'enrichment.sources_saved'") == [(1,)]


def test_open_library_stays_off_until_a_contact_address_is_given(client_as, settings, network):
    browser = client_as("admin")
    page = browser.get("/parametres/enrichissement").text
    assert "Renseignez d’abord l’adresse de contact" in page
    refused = browser.post("/admin/sources", data=_settings_form(openlibrary_active="1"))
    assert refused.status_code == 400 and "Renseignez d’abord l’adresse de contact" in refused.text
    assert _rows(settings, "SELECT is_enabled FROM metadata_sources WHERE code = 'openlibrary'") == [(0,)]
    invalid = browser.post("/admin/sources", data=_settings_form(openlibrary_active="1", contact="pas une adresse"))
    assert invalid.status_code == 400 and "adresse électronique valide" in invalid.text
    saved = browser.post("/admin/sources", data=_settings_form(openlibrary_active="1", contact="club@exemple.fr"))
    assert saved.status_code == 303
    assert _rows(settings, "SELECT is_enabled FROM metadata_sources WHERE code = 'openlibrary'") == [(1,)]
    assert _rows(settings, "SELECT value FROM settings WHERE key = 'enrichment.contact'") == [('"club@exemple.fr"',)]
    details = _rows(settings, "SELECT details FROM audit_log WHERE action = 'enrichment.sources_saved'")[-1][0]
    assert '"contact_renseigne": true' in details and "club@exemple.fr" not in details


def test_an_enabled_source_without_contact_is_treated_as_disabled(settings, network):
    from biblyo.core.db import Database
    from biblyo.modules.enrichment.connectors.transport import USER_AGENT, user_agent
    from biblyo.modules.enrichment.sources import SourceHub

    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE metadata_sources SET is_enabled = 1 WHERE code = 'openlibrary'")
    connection.commit()
    connection.close()
    database = Database(settings.database_path)
    try:
        assert "openlibrary" not in {c.code for c in SourceHub(database).enabled()}
    finally:
        database.dispose()
    assert user_agent("") == USER_AGENT and "contact : club@exemple.fr" in user_agent("club@exemple.fr")


@pytest.mark.parametrize(
    ("field", "value"),
    [("bnf_intervalle", "100"), ("sudoc_lot", "500"), ("bnf_delai", "120000"), ("bnf_delai", "abc")],
)
def test_floors_and_ranges_are_enforced(client_as, settings, network, field, value):
    response = client_as("admin").post("/admin/sources", data=_settings_form(**{field: value}))
    assert response.status_code == 400 and "corrigez les champs signalés" in response.text
    assert _rows(settings, "SELECT interval_ms FROM metadata_sources WHERE code = 'bnf'") == [(1000,)]


def test_connection_test_and_cache_clearing(client_as, settings, network):
    browser = client_as("admin")
    page = browser.post("/admin/sources", data={"tester": "bnf"})
    assert page.status_code == 200 and "répond" in page.text
    assert any("catalogue.bnf.fr" in url for url in network.calls)
    network.down = True
    page = browser.post("/admin/sources", data={"tester": "sudoc"})
    assert "ne répond pas" in page.text and "répond normalement" not in page.text
    assert _rows(settings, "SELECT COUNT(*) FROM source_cache WHERE source_code = 'bnf'")[0][0] >= 1
    response = browser.post("/admin/sources", data={"vider": "bnf"})
    assert response.status_code == 303
    assert _rows(settings, "SELECT COUNT(*) FROM source_cache WHERE source_code = 'bnf'") == [(0,)]


def test_a_disabled_source_is_never_queried_by_the_screen(client_as, settings, network):
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE metadata_sources SET is_enabled = 0 WHERE code = 'sudoc'")
    connection.commit()
    connection.close()
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    _search(client_as("librarian"), work)
    assert not any("sudoc" in url for url in network.calls)
