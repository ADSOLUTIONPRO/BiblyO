# SPDX-License-Identifier: EUPL-1.2
"""Écran « Enrichir » : permissions, recherche, résultats, comparaison, aperçu, confirmation, sur un réseau simulé."""

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
from tests.factories import make_notice
from tests.sources import read_bytes

PETIT_PRINCE = "9782070612758"
ROLES = ("admin", "librarian", "volunteer", "viewer")
SEARCHERS = ("admin", "librarian")


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 583), (200, 90, 60)).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class Network:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if self.down:
            return httpx.Response(503, content=b"panne")
        if "recupererImage" in url:
            if "cb41023439w" in url:
                return httpx.Response(200, content=_jpeg(), headers={"Content-Type": "image/jpeg"})
            return httpx.Response(500, content=b"id to load is required for loading")
        if f"bib.isbn%20any%20%22{PETIT_PRINCE}" in url:
            return httpx.Response(
                200, content=read_bytes("bnf/isbn-exact-petit-prince"), headers={"Content-Type": "text/xml"}
            )
        if f"isb%3D{PETIT_PRINCE}" in url:
            return httpx.Response(
                200, content=read_bytes("sudoc/sru-isbn-petit-prince"), headers={"Content-Type": "text/xml"}
            )
        if "bib.title%20all%20%22le%20petit%20prince" in url:
            return httpx.Response(
                200, content=read_bytes("bnf/isbn-exact-petit-prince"), headers={"Content-Type": "text/xml"}
            )
        if "isb%3D9782344004319" in url:  # One Piece, tome 72 : titre du tome en 200$i, « Tome [72] » en 200$h
            return httpx.Response(
                200, content=read_bytes("sudoc/sru-isbn-tome-titre-propre"), headers={"Content-Type": "text/xml"}
            )
        default = "sudoc/sru-isbn-inconnu" if "sudoc" in url else "bnf/isbn-inconnu"
        return httpx.Response(200, content=read_bytes(default), headers={"Content-Type": "text/xml"})


@pytest.fixture
def network(app, settings):
    return install_network(app, settings)


def install_network(app, settings) -> Network:
    """Remplace les sources de l'application par le réseau simulé ; rend ce réseau pour inspecter ses appels."""
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


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _notice(settings, work_id):
    database = Database(settings.database_path)
    with database.session() as session:
        view = repo.load_notice(session, work_id)
    database.dispose()
    return view


def _edition(settings, work_id) -> int:
    return _notice(settings, work_id).primary.id


def _search(browser, work, **criteria) -> tuple[int, str]:
    """Lance une recherche et rend l'identifiant de la recherche enregistrée et la page des résultats."""
    response = browser.post(
        f"/catalogue/notices/{work}/enrichir", data={"edition": str(_edition_of(browser, work)), **criteria}
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    search_id = int(re.search(r"recherche=(\d+)", location).group(1))
    page = browser.get(location)
    assert page.status_code == 200
    return search_id, page.text


def _edition_of(browser, work) -> int:
    page = browser.get(f"/catalogue/notices/{work}/enrichir")
    return int(re.search(r'name="edition" value="(\d+)"', page.text).group(1))


def _proposal_id(html: str) -> int:
    return int(re.search(r"/propositions/(\d+)", html).group(1))


# --- Permissions ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
def test_the_screen_requires_the_search_permission(client_as, settings, role, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as(role)
    page = browser.get(f"/catalogue/notices/{work}/enrichir")
    posted = browser.post(f"/catalogue/notices/{work}/enrichir", data={"isbn": PETIT_PRINCE})
    if role in SEARCHERS:
        assert page.status_code == 200 and posted.status_code == 303
    else:
        assert page.status_code == 403 and posted.status_code == 403
        assert network.calls == []


def test_the_notice_shows_the_button_to_those_who_can_search(client_as, settings):
    work = make_notice(settings, "Le Petit Prince")
    assert "/enrichir?edition=" in client_as("librarian").get(f"/catalogue/notices/{work}").text
    assert "/enrichir" not in client_as("volunteer").get(f"/catalogue/notices/{work}").text
    actions = client_as("librarian").get(f"/catalogue/notices/{work}").text.split("hero-acts")[1].split("</div>")[0]
    assert "/a-venir/" not in actions and "btn-soon" not in actions


def test_applying_requires_the_review_permission(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    admin = client_as("admin")
    _, html = _search(admin, work)
    proposal = _proposal_id(html)
    volunteer = client_as("volunteer")
    assert (
        volunteer.post(
            f"/catalogue/notices/{work}/enrichir/propositions/{proposal}",
            data={"action": "appliquer", "champ": ["year"]},
        ).status_code
        == 403
    )
    assert _notice(settings, work).primary.year is None


def test_csrf_is_enforced(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("admin")
    del browser.headers["X-CSRF-Token"]
    assert browser.post(f"/catalogue/notices/{work}/enrichir", data={"isbn": PETIT_PRINCE}).status_code == 403


# --- Recherche et résultats -------------------------------------------------------------------------------------------
def test_search_shows_sources_results_and_comparison(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    search_id, html = _search(browser, work)
    assert "2. Sources interrogées" in html and "3. Résultats" in html
    assert "Correspondance exacte" in html and "1 résultat(s)" in html
    assert "BnF" in html and "Sudoc" in html
    assert "Pourquoi ce score" in html and "Titre identique" in html
    assert "Comparer et choisir" in html
    assert "ark:/12148/cb41023439w" in html and "https://www.sudoc.fr/117074047" in html
    assert "843 SAI" not in html  # aucune donnée d'exemplaire du Sudoc n'atteint l'écran
    assert _rows(settings, "SELECT COUNT(*) FROM enrichment_searches") == [(1,)]
    # Un rechargement de la page des résultats n'appelle aucune source
    calls = len(network.calls)
    assert browser.get(f"/catalogue/notices/{work}/enrichir?recherche={search_id}").status_code == 200
    assert len(network.calls) == calls


def test_two_editions_from_two_sources_are_compared_side_by_side(client_as, settings, network, app):
    """Astérix : une notice de la BnF et deux du Sudoc pour le même ISBN, dont une autre édition."""
    from pathlib import Path

    from biblyo.modules.enrichment.connectors.transport import RecordedTransport

    app.state.enrichment_hub._transport = RecordedTransport(Path("tests/fixtures/sources/enregistrees"))
    work = make_notice(
        settings, "Astérix le Gaulois", authors=("René Goscinny",), series="Astérix", volume="1", isbn="9782012101333"
    )
    _, html = _search(client_as("librarian"), work)
    assert "4. Comparer les éditions" in html and "Hachette" in html and "DL 2006" in html
    assert "Série et tome" in html and "tome 1" in html


def test_a_source_down_is_said_incomplete_never_no_result(client_as, settings, network):
    work = make_notice(settings, "Un titre inconnu", isbn="9782999999992")
    network.down = True
    _, html = _search(client_as("librarian"), work)
    assert "Recherche incomplète" in html and "Indisponible" in html and "incomplète" in html
    assert "Aucune notice correspondante" not in html


def test_no_result_is_said_plainly(client_as, settings, network):
    work = make_notice(settings, "Un titre inconnu", isbn="9782999999992")
    _, html = _search(client_as("librarian"), work)
    assert "Aucune notice correspondante" in html and "Aucun résultat" in html


def test_criteria_come_from_the_form_and_are_kept_with_the_search(client_as, settings, network):
    work = make_notice(settings, "Autre titre")
    browser = client_as("librarian")
    _, html = _search(browser, work, isbn=PETIT_PRINCE, titre="", auteur="")
    # Le titre de la fiche contredit la notice trouvée : elle est montrée comme écartée, et dit pourquoi
    assert "notice(s) écartée(s)" in html and "Titre différent" in html and "score 0 sur 100" in html
    assert "Comparer et choisir" not in html
    assert _rows(settings, "SELECT criteria FROM enrichment_searches")[0][0].startswith('{"isbn": "9782070612758"')
    assert {r[1] for r in _rows(settings, "SELECT match_class, status FROM enrichment_proposals")} == {"rejected"}


def test_search_is_rate_limited_per_account(client_as, settings, network, app):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    for _ in range(30):
        assert browser.post(f"/catalogue/notices/{work}/enrichir", data={"isbn": PETIT_PRINCE}).status_code == 303
    response = browser.post(f"/catalogue/notices/{work}/enrichir", data={"isbn": PETIT_PRINCE})
    assert response.status_code == 303 and "recherche=" not in response.headers["location"]
    assert _rows(settings, "SELECT COUNT(*) FROM enrichment_searches") == [(30,)]


def test_the_sources_cannot_be_reached_through_the_form(client_as, settings, network):
    """Les critères ne sont que des termes : aucune adresse ni requête saisie ne part vers une source."""
    work = make_notice(settings, "Autre titre")
    browser = client_as("librarian")
    _search(browser, work, titre='x") or bib.anywhere all "y', auteur="https://evil.example/")
    for url in network.calls:
        assert "evil.example" not in url and "x%22%29" not in url
        assert url.startswith(("https://catalogue.bnf.fr/", "https://www.sudoc.abes.fr/"))


# --- Comparaison, aperçu, confirmation --------------------------------------------------------------------------------
def test_proposal_page_shows_both_values_with_prechecked_empty_fields_only(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE, publisher="Galimard")
    browser = client_as("librarian")
    _, html = _search(browser, work)
    page = browser.get(f"/catalogue/notices/{work}/enrichir/propositions/{_proposal_id(html)}").text
    assert "5. Différences" in page and "Galimard" in page and "Gallimard" in page
    assert re.search(r'id="champ-year"[^>]*checked', page)  # vide, exact : précoché
    assert not re.search(r'id="champ-publisher"[^>]*checked', page)  # rempli : jamais précoché
    assert "Remplacer" in page and "Non copiable" in page and "Electre" in page
    assert not re.search(r'id="champ-isbn"[^>]*checked', page) or "champ-isbn" not in page
    assert "Série et tome" in page or "Folio junior" in page


def test_preview_then_confirm_applies_only_the_checked_fields(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE, publisher="Galimard")
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    path = f"/catalogue/notices/{work}/enrichir/propositions/{proposal}"
    preview = browser.post(path, data={"action": "apercu", "champ": ["year", "pages"]})
    assert preview.status_code == 200
    assert "6. Aperçu" in preview.text and "2 champ(s) complété(s), 0 remplacé(s)" in preview.text
    assert "Les exemplaires, les cotes, le rangement" in preview.text
    assert (
        "Galimard" not in preview.text.split("Changements à appliquer")[1].split("</table>")[0]
    )  # l'éditeur n'est pas touché
    done = browser.post(path, data={"action": "appliquer", "champ": ["year", "pages"]})
    assert done.status_code == 303 and done.headers["location"].endswith("?onglet=enrichissement")
    view = _notice(settings, work)
    assert (view.primary.publisher.label, view.primary.year, view.primary.pages) == ("Galimard", 2007, 113)
    assert _rows(settings, "SELECT COUNT(*) FROM field_provenance") == [(2,)]
    followed = browser.get(done.headers["location"])
    assert "Les champs cochés ont été appliqués" in followed.text


def test_check_empty_fields_button_checks_only_fillable_fields(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE, publisher="Galimard")
    browser = client_as("librarian")
    _, html = _search(browser, work)
    page = browser.post(
        f"/catalogue/notices/{work}/enrichir/propositions/{_proposal_id(html)}", data={"action": "cocher_vides"}
    )
    assert page.status_code == 200
    assert re.search(r'id="champ-pages"[^>]*checked', page.text)
    assert not re.search(r'id="champ-publisher"[^>]*checked', page.text)
    assert not re.search(r'id="champ-isbn"[^>]*checked', page.text)


def test_a_stale_proposal_is_refused_with_a_message(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    view = _notice(settings, work)
    updated = browser.post(
        f"/catalogue/notices/{work}/sections/contenu",
        data={"resume": "Changé entre-temps", "version": str(view.version)},
    )
    assert updated.status_code == 303, updated.text[:300]
    response = browser.post(
        f"/catalogue/notices/{work}/enrichir/propositions/{proposal}", data={"action": "appliquer", "champ": ["year"]}
    )
    assert response.status_code == 303
    page = browser.get(response.headers["location"])
    assert "périmée" in page.text
    assert _notice(settings, work).primary.year is None
    assert _rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", proposal) == [("stale",)]


def test_a_treated_proposal_redirects_with_a_message(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    browser.post(
        f"/catalogue/notices/{work}/enrichir/propositions/{proposal}", data={"action": "appliquer", "champ": ["year"]}
    )
    response = browser.get(f"/catalogue/notices/{work}/enrichir/propositions/{proposal}")
    assert response.status_code == 303
    assert "déjà été traitée" in browser.get(response.headers["location"]).text


def test_contributors_are_added_with_the_chosen_role(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    page = browser.get(f"/catalogue/notices/{work}/enrichir/propositions/{proposal}").text
    assert 'name="contributeur" value="Antoine de Saint-Exupéry"' in page and "rôle incertain" not in page
    browser.post(
        f"/catalogue/notices/{work}/enrichir/propositions/{proposal}",
        data={
            "action": "appliquer",
            "champ": ["contributors"],
            "contributeur": ["Antoine de Saint-Exupéry"],
            "role_Antoine de Saint-Exupéry": "author",
        },
    )
    assert [(c.name, c.role) for c in _notice(settings, work).contributors] == [("Antoine de Saint-Exupéry", "author")]


def test_a_proposal_of_another_notice_is_not_served(client_as, settings, network):
    first = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    other = make_notice(settings, "Autre")
    browser = client_as("librarian")
    _, html = _search(browser, first)
    assert browser.get(f"/catalogue/notices/{other}/enrichir/propositions/{_proposal_id(html)}").status_code == 404


def test_unknown_notice_is_404(client_as, network):
    assert client_as("librarian").get("/catalogue/notices/999999/enrichir").status_code == 404


# --- Couvertures ------------------------------------------------------------------------------------------------------
def test_the_proposed_cover_is_served_by_biblyo_as_webp(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    page = browser.get(f"/catalogue/notices/{work}/enrichir/propositions/{proposal}").text
    path = f"/catalogue/notices/{work}/enrichir/propositions/{proposal}/couverture"
    assert f'src="{path}"' in page and "openapi.bnf.fr" not in page
    image = browser.get(path)
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    assert image.content[:4] == b"RIFF" and image.headers["cache-control"].startswith("private")
    calls = len([u for u in network.calls if "recupererImage" in u])
    assert browser.get(path).status_code == 200
    assert len([u for u in network.calls if "recupererImage" in u]) == calls  # gardée en mémoire
    assert client_as("volunteer").get(path).status_code == 403


def test_no_cover_route_when_nothing_is_offered(client_as, settings, network):
    work = make_notice(settings, "Un titre inconnu", isbn="9782999999992")
    browser = client_as("librarian")
    _search(browser, work)
    assert browser.get(f"/catalogue/notices/{work}/enrichir/propositions/999/couverture").status_code == 404


def test_replacing_a_cover_shows_a_dedicated_confirmation(client_as, settings, network):
    from biblyo.modules.catalogue import service as catalogue

    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    database = Database(settings.database_path)
    catalogue.set_cover(database, _edition(settings, work), _jpeg(), settings.media_dir, None)
    database.dispose()
    before = _notice(settings, work).primary.cover.sha256
    browser = client_as("librarian")
    _, html = _search(browser, work)
    proposal = _proposal_id(html)
    path = f"/catalogue/notices/{work}/enrichir/propositions/{proposal}"
    assert 'name="couverture_remplacer"' in browser.get(path).text
    preview = browser.post(path, data={"action": "apercu", "champ": ["cover"]})
    assert "remplacement non confirmé : ignoré" in preview.text
    done = browser.post(path, data={"action": "appliquer", "champ": ["cover", "year"]})
    assert done.status_code == 303
    assert _notice(settings, work).primary.cover.sha256 == before
    assert "la couverture n’a pas pu être récupérée" in browser.get(done.headers["location"]).text


def test_applying_a_cover_through_the_screen(client_as, settings, network):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    _, html = _search(browser, work)
    path = f"/catalogue/notices/{work}/enrichir/propositions/{_proposal_id(html)}"
    preview = browser.post(path, data={"action": "apercu", "champ": ["cover"]})
    assert "Image de la BnF, récupérée à la confirmation" in preview.text
    done = browser.post(path, data={"action": "appliquer", "champ": ["cover"]})
    assert done.status_code == 303
    cover = _notice(settings, work).primary.cover
    assert cover is not None and (settings.media_dir / f"{cover.sha256}.webp").is_file()
    assert _rows(settings, "SELECT origin, source_code FROM media_files") == [("bnf", "bnf")]


def test_a_disabled_source_is_listed_as_disabled_even_unchecked(client_as, settings, network):
    """Une case désactivée n'est pas envoyée par le navigateur : la source doit rester dite « Désactivée »."""
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE metadata_sources SET is_enabled = 0 WHERE code = 'sudoc'")
    connection.commit()
    connection.close()
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    _, html = _search(client_as("librarian"), work, sources="bnf")
    assert "Désactivée" in html.split("2. Sources interrogées")[1].split("3. Résultats")[0]


def test_a_volume_title_record_is_exact_and_shows_the_volume_number(client_as, settings, network):
    """Recette sur DEV : le Sudoc titre le tome 72 « Les oubliés de Dressrosa » et écrit « Tome [72] »."""
    work = make_notice(settings, "One Piece", authors=("Eiichirō Oda",), series="One Piece", volume="72",
                       isbn="9782344004319")
    _, html = _search(client_as("librarian"), work)
    assert "Les oubliés de Dressrosa" in html and "Correspondance exacte" in html
    assert "tome 72" in html and "Tome [72" not in html
    assert "Non interrogée" not in html  # au niveau de l'ISBN, les deux sources sont appelées
