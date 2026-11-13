# SPDX-License-Identifier: EUPL-1.2
"""Recherche de couvertures à l'écran (essais 7 et 8) : propositions, image servie par biblyO, choix, aperçu
exigeant la confirmation d'une autre édition et le droit d'utilisation d'une image d'Open Library."""

from __future__ import annotations

import re
import sqlite3

import httpx
import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle

from ..factories import make_notice
from ..services.test_cover_search import Network

#: Open Library exige une adresse de contact de l'instance : sans elle, la source reste inactive.
CONTACT_SQL = (
    "INSERT OR REPLACE INTO settings (key, value, updated_at) "
    "VALUES ('enrichment.contact', '\"bibliotheque@exemple.invalid\"', '2026-11-13T00:00:00+00:00')"
)

HX = {"HX-Request": "true"}


@pytest.fixture
def network(app, settings) -> Network:
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
    app.state.enrichment_previews = None
    return net


def _edition(settings, work):
    database = Database(settings.database_path)
    with database.session() as session:
        view = repo.load_notice(session, work)
    database.dispose()
    return view.primary


def _execute(settings, query):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(query)
    connection.commit()
    connection.close()


def _choose(browser, work, page: str, index: int = 0):
    forms = re.findall(
        r'<form method="post" action="(/catalogue/notices/\d+/couverture/proposition)">(.*?)</form>', page, re.S
    )
    action, body = forms[index]
    data = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)">', body))
    data = {k: v.replace("&amp;", "&").replace("&#39;", "'") for k, v in data.items() if k != "_csrf"}
    return browser.post(action, data=data), data


def test_the_proposals_show_images_served_by_biblyo_with_their_edition(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn="9782070612758")
    edition = _edition(settings, work).id
    browser = client_as("librarian")
    page = browser.get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX)
    assert page.status_code == 200
    assert "Même édition établie : ISBN identique" in page.text and "Choisir" in page.text
    image = re.search(r'src="(/catalogue/couvertures/image\?[^"]+taille=vignette)"', page.text).group(1)
    served = browser.get(image.replace("&amp;", "&"))
    assert served.status_code == 200 and served.headers["content-type"] == "image/webp"
    assert "Open Library" in page.text and "activable par l’administrateur" in page.text


def test_without_htmx_the_search_opens_the_cover_page(client_as, settings, network):
    work = make_notice(settings)
    edition = _edition(settings, work).id
    response = client_as("librarian").get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}")
    assert response.status_code == 303 and response.headers["location"].endswith("&rechercher=1")


def test_the_image_route_refuses_anything_but_a_known_reference(client_as, network):
    browser = client_as("librarian")
    for query in (
        "source=bnf&ref=../../etc",
        "source=ailleurs&ref=1",
        "source=openlibrary&ref=abc",
        "source=bnf&ref=ark:/12148/cb41023439w&taille=enorme",
    ):
        assert browser.get(f"/catalogue/couvertures/image?{query}").status_code == 404, query
    assert (
        client_as("viewer").get("/catalogue/couvertures/image?source=bnf&ref=ark:/12148/cb41023439w").status_code == 403
    )


def test_choosing_a_same_edition_cover_saves_it_with_the_bnf_conditions(client_as, settings, network):
    """Essai 8."""
    work = make_notice(settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn="9782070612758")
    edition = _edition(settings, work).id
    browser = client_as("librarian")
    page = browser.get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX).text
    response, _data = _choose(browser, work, page)
    assert response.status_code == 303
    preview = browser.get(response.headers["location"]).text
    assert "Conditions de la source" in preview and "J’ai vérifié sur le livre" not in preview
    token = response.headers["location"].rsplit("/", 1)[1]
    saved = browser.post(
        f"/catalogue/notices/{work}/couverture/apercu/{token}", data={"action": "enregistrer", "attendue": ""}
    )
    assert saved.status_code == 303
    cover = _edition(settings, work).cover
    assert cover.source == "bnf" and cover.origin == "bnf" and "BnF" in cover.license_note


def test_another_edition_needs_an_explicit_confirmation(client_as, settings, network):
    """Le Horla, sans ISBN : une image d'une autre édition ne s'enregistre qu'avec la case de vérification."""
    work = make_notice(settings, "Le Horla", authors=("Guy de Maupassant",))
    edition = _edition(settings, work).id
    browser = client_as("librarian")
    page = browser.get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX).text
    assert "Même édition établie : ISBN identique" not in page
    response, data = _choose(browser, work, page)
    assert data["match"] in ("presumed", "undetermined")
    token = response.headers["location"].rsplit("/", 1)[1]
    url = f"/catalogue/notices/{work}/couverture/apercu/{token}"
    refused = browser.post(url, data={"action": "enregistrer", "attendue": ""})
    assert refused.status_code == 400 and "correspond bien à votre édition" in refused.text
    assert _edition(settings, work).cover is None
    assert browser.post(url, data={"action": "enregistrer", "attendue": "", "correspondance": "1"}).status_code == 303
    assert _edition(settings, work).cover is not None


def test_an_open_library_image_needs_the_right_of_use(client_as, settings, network):
    _execute(settings, "UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'")
    _execute(settings, CONTACT_SQL)
    network.no_image = {"ark:/12148/cb41023439w", "9782070612758"}
    work = make_notice(settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn="9782070612758")
    edition = _edition(settings, work).id
    browser = client_as("librarian")
    page = browser.get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX).text
    response, data = _choose(browser, work, page)
    assert data["source"] == "openlibrary" and data["match"] == "same_edition"
    url = response.headers["location"]
    preview = browser.get(url).text
    assert "n’accorde aucun droit" in preview and "Photo prise pour le Club" not in preview
    refused = browser.post(url, data={"action": "enregistrer", "attendue": ""})
    assert refused.status_code == 400 and "votre droit de l’utiliser" in refused.text
    club = browser.post(url, data={"action": "enregistrer", "attendue": "", "droit": "club"})
    assert club.status_code == 400 and _edition(settings, work).cover is None  # une image tierce n'est pas une photo
    assert browser.post(url, data={"action": "enregistrer", "attendue": "", "droit": "editeur"}).status_code == 303
    cover = _edition(settings, work).cover
    assert cover.source == "openlibrary" and "Image fournie ou autorisée par l’éditeur" in cover.license_note


def test_an_empty_search_says_what_to_do(client_as, settings, network):
    work = make_notice(settings, "Astérix le Gaulois", authors=("René Goscinny",))
    edition = _edition(settings, work).id
    page = (
        client_as("librarian")
        .get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX)
        .text
    )
    assert "Aucune proposition" in page and "Photographiez la couverture" in page


def test_a_source_outage_is_said_plainly(client_as, settings, network):
    network.down = True
    work = make_notice(settings, "Le Petit Prince", isbn="9782070612758")
    edition = _edition(settings, work).id
    page = (
        client_as("librarian")
        .get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}", headers=HX)
        .text
    )
    assert "Les sources n’ont pas répondu" in page and "Indisponible" in page
