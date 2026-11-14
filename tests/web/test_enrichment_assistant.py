# SPDX-License-Identifier: EUPL-1.2
"""Parcours « Enrichir un livre » (L7, T3), sur les réponses réelles relevées le 14 novembre 2026."""

from __future__ import annotations

import html
import re
import sqlite3

import httpx
import pytest

from biblyo.core.db import Database
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice
from tests.services.test_assistant import CERVEAU_CHOC, DU_SCENARIO, Network


@pytest.fixture
def network(app, settings):
    net = Network()
    client = httpx.Client(transport=httpx.MockTransport(net.handler), follow_redirects=False, trust_env=False)
    transport = HttpTransport(registry.HOSTS, resolver=lambda host: ["194.199.8.183"], client=client)
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    app.state.enrichment_hub = SourceHub(
        Database(settings.database_path),
        transport=transport,
        throttle=Throttle(clock=lambda: clock["now"], sleep=tick),
        sleep=tick,
    )
    return net


def rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def edition_of(browser, work) -> int:
    page = browser.get(f"/catalogue/notices/{work}/enrichir")
    return int(re.search(r'name="edition" value="(\d+)"', page.text).group(1))


def search(browser, work) -> tuple[str, str]:
    response = browser.post(
        f"/catalogue/notices/{work}/enrichir/assistant", data={"edition": edition_of(browser, work)}
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert "recherche=" in location and location.endswith("#propositions")
    return location, browser.get(location).text


def test_the_page_shows_the_book_what_is_missing_and_one_main_action(client_as, settings, network):
    work = make_notice(settings, **CERVEAU_CHOC)
    page = client_as("librarian").get(f"/catalogue/notices/{work}/enrichir").text
    assert "Cerveau-choc" in page and "Achille Talon" in page
    assert "Il manque :" in page and "couverture" in page and "ISBN" in page
    assert "Rechercher la couverture et les informations" in page
    assert network.calls == []  # rien n'est demandé tant que la personne n'a pas cliqué


def test_cerveau_choc_cards_put_the_1984_album_first_without_scores_up_front(client_as, settings, network):
    work = make_notice(settings, **CERVEAU_CHOC)
    _, page = search(client_as("librarian"), work)
    page = html.unescape(page)
    first = page.index("Les Idées d'Achille Talon, cerveau-choc")
    assert first < page.index("Choisir cette proposition")
    assert "Édition probable" in page and "Ce n’est pas mon livre" in page
    assert "Pas d’image" in page  # la BnF n'en a pas : dit clairement, jamais une image générique
    assert "Détails techniques" in page  # le score n'est plus au centre ; il reste dans les détails
    assert "enr-score" not in page  # plus d'anneau de score


def test_du_scenario_shows_1981_and_1990_side_by_side(client_as, settings, network):
    work = make_notice(settings, **DU_SCENARIO)
    _, page = search(client_as("librarian"), work)
    assert "DL 1981" in page and "1990" in page
    assert "Plusieurs éditions se ressemblent" in page


def test_reload_does_not_call_the_sources_again(client_as, settings, network):
    work = make_notice(settings, **CERVEAU_CHOC)
    browser = client_as("librarian")
    location, _ = search(browser, work)
    calls = len(network.calls)
    assert browser.get(location).status_code == 200
    assert len(network.calls) == calls


def test_not_my_book_hides_the_edition_and_touches_no_record(client_as, settings, network):
    work = make_notice(settings, **CERVEAU_CHOC)
    browser = client_as("librarian")
    location, page = search(browser, work)
    proposal = int(re.search(r"/propositions/(\d+)/pas-mon-livre", page).group(1))
    search_id = re.search(r"recherche=(\d+)", location).group(1)
    before = rows(settings, "SELECT title, version FROM works WHERE id = ?", work)
    response = browser.post(
        f"/catalogue/notices/{work}/enrichir/propositions/{proposal}/pas-mon-livre", data={"recherche": search_id}
    )
    assert response.status_code == 303
    assert rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", proposal) == [("rejected",)]
    assert rows(settings, "SELECT title, version FROM works WHERE id = ?", work) == before
    assert rows(settings, "SELECT COUNT(*) FROM audit_log WHERE action = 'enrichment.dismissed'") == [(1,)]
    assert f"/propositions/{proposal}/pas-mon-livre" not in browser.get(response.headers["location"]).text


def test_adding_missing_information_requires_a_check_when_not_the_same_edition(client_as, settings, network):
    work = make_notice(settings, **CERVEAU_CHOC)
    browser = client_as("librarian")
    _, page = search(browser, work)
    proposal = int(re.search(r"/enrichir/propositions/(\d+)\"", page).group(1))
    comparison = browser.get(f"/catalogue/notices/{work}/enrichir/propositions/{proposal}").text
    assert "Votre fiche" in comparison and "Proposé par la source" in comparison
    assert "Manquant : sera ajouté" in comparison and "J’ai vérifié : c’est bien mon livre" in comparison
    url = f"/catalogue/notices/{work}/enrichir/propositions/{proposal}"
    refused = browser.post(url, data={"action": "manquants"})
    assert refused.status_code == 303 and refused.headers["location"].endswith(f"/propositions/{proposal}")
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_decisions") == [(0,)]
    applied = browser.post(url, data={"action": "manquants", "verifie": "1"})
    assert applied.status_code == 303 and "onglet=enrichissement" in applied.headers["location"]
    fields = {f for (f,) in rows(settings, "SELECT field FROM enrichment_decisions WHERE decision = 'accepted'")}
    assert fields and not fields & {"isbn", "cover", "contributors", "summary", "title"}
    assert rows(settings, "SELECT title FROM works WHERE id = ?", work) == [("Cerveau-choc",)]  # rien de remplacé


@pytest.mark.parametrize("role", ["viewer"])
def test_those_who_cannot_search_cannot_use_the_assistant(client_as, settings, network, role):
    work = make_notice(settings, **CERVEAU_CHOC)
    browser = client_as(role)
    assert browser.get(f"/catalogue/notices/{work}/enrichir").status_code == 403
    assert browser.post(f"/catalogue/notices/{work}/enrichir/assistant", data={}).status_code == 403
    assert network.calls == []
