# SPDX-License-Identifier: EUPL-1.2
"""Recherche bibliographique libre (E3) et création d'une notice depuis un résultat (PA-2, D19)."""

from __future__ import annotations

import re

import pytest

from tests.factories import make_notice
from tests.web.test_enrichment_screen import PETIT_PRINCE, _notice, _rows, install_network

ROLES = ("admin", "librarian", "volunteer", "viewer")


@pytest.fixture
def network(app, settings):
    return install_network(app, settings)


def _type_id(settings) -> int:
    return _rows(settings, "SELECT id FROM document_types WHERE code = 'livre'")[0][0]


def _free_search(browser, **criteria) -> str:
    response = browser.post("/sources", data=criteria)
    assert response.status_code == 303 and "recherche=" in response.headers["location"], response.headers
    page = browser.get(response.headers["location"])
    assert page.status_code == 200
    return page.text


def _proposal(html: str) -> int:
    return int(re.search(r"/sources/propositions/(\d+)/creer", html).group(1))


def _create_form(settings, values: dict, people: list[tuple[str, str]]) -> dict:
    data = {k: v for k, v in values.items() if v is not None}
    data["type"] = str(_type_id(settings))
    data["contributeur_nom"] = [name for name, _role in people] + [""]
    data["contributeur_role"] = [role for _name, role in people] + ["author"]
    data["contributeur_choix"] = [""] * (len(people) + 1)
    data["contributeur_dates"] = [""] * (len(people) + 1)
    return data


def _prefilled(page: str) -> dict:
    """Valeurs des champs simples du formulaire de création, telles que le navigateur les enverrait."""
    found = {}
    for tag in re.findall(r"<input [^>]*>", page):
        name = re.search(r'name="([a-z_]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and value and 'type="hidden"' not in tag and 'type="checkbox"' not in tag:
            found[name.group(1)] = value.group(1)
    for name in ("depuis", "isbn_verifie"):
        match = re.search(rf'name="{name}" value="([^"]*)"', page)
        if match:
            found[name] = match.group(1)
    found["langue"] = re.search(r'<option value="([a-z]+)" selected>', page.split('id="langue"')[1]).group(1)
    resume = re.search(r'<textarea class="in" id="resume" name="resume"[^>]*>([^<]*)</textarea>', page)
    found["resume"] = resume.group(1) if resume else ""
    return found


# --- Recherche libre --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
def test_free_search_needs_the_search_permission(client_as, role, network):
    browser = client_as(role)
    allowed = role in ("admin", "librarian")
    assert browser.get("/sources").status_code == (200 if allowed else 403)
    assert browser.post("/sources", data={"isbn": PETIT_PRINCE}).status_code == (303 if allowed else 403)
    if not allowed:
        assert network.calls == []


def test_free_search_by_isbn_offers_creation(client_as, network):
    html = _free_search(client_as("librarian"), isbn=PETIT_PRINCE)
    assert "Correspondance exacte" in html and "Le petit prince" in html and "Créer une notice" in html
    assert "Comparer et choisir" not in html


def test_empty_free_search_is_refused_without_calling_sources(client_as, network):
    response = client_as("librarian").post("/sources", data={"isbn": "", "titre": ""})
    assert response.status_code == 303 and "recherche=" not in response.headers["location"]
    assert network.calls == []


def test_the_navigation_leads_to_the_free_search(client_as, network):
    html = client_as("librarian").get("/").text
    assert 'href="/sources"' in html and "/a-venir/enrichissement" not in html


# --- Création ---------------------------------------------------------------------------------------------------------
def test_exact_result_prefills_the_form_and_records_provenance(client_as, settings, network):
    browser = client_as("librarian")
    proposal = _proposal(_free_search(browser, isbn=PETIT_PRINCE))
    confirm = browser.get(f"/sources/propositions/{proposal}/creer")
    assert confirm.status_code == 200 and "Continuer vers le formulaire" in confirm.text
    form_page = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1")
    assert form_page.status_code == 200 and "Prérempli depuis la BnF" in form_page.text
    values = _prefilled(form_page.text)
    assert values["titre"] == "Le petit prince" and values["isbn"] == PETIT_PRINCE
    assert values["editeur"] == "Gallimard" and values["annee"] == "2007" and values["pages"] == "113"
    assert values["resume"] == ""  # résumé Electre : non repris
    assert re.search(r'id="serie"[^>]*value=""', form_page.text)  # série jamais préremplie
    assert "La source indique la série" in form_page.text and "Electre" in form_page.text
    values["editeur"] = "Gallimard jeunesse"  # la personne corrige un champ
    created = browser.post(
        "/catalogue/notices", data=_create_form(settings, values, [("Antoine de Saint-Exupéry", "author")])
    )
    assert created.status_code == 303, created.text[:500]
    work = int(re.search(r"/catalogue/notices/(\d+)", created.headers["location"]).group(1))
    notice = _notice(settings, work)
    assert notice.primary.isbn13 == (PETIT_PRINCE,) and notice.primary.publisher.label == "Gallimard jeunesse"
    fields = {r[0] for r in _rows(settings, "SELECT field FROM field_provenance")}
    assert {"title", "year", "pages", "isbn"} <= fields and "publisher" not in fields  # corrigé : pas de provenance
    assert _rows(settings, "SELECT status FROM edition_isbn_candidates") == [("confirmed",)]
    assert _rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", proposal) == [("applied",)]
    assert _rows(settings, "SELECT edition_id IS NOT NULL FROM enrichment_searches") == [(1,)]
    assert "tel que repris" in browser.get(f"/catalogue/notices/{work}?onglet=enrichissement").text


def test_an_ambiguous_result_needs_the_edition_confirmed_first(client_as, settings, network):
    browser = client_as("librarian")
    html = _free_search(browser, titre="Le petit prince", auteur="Saint-Exupéry", annee="2015")
    proposal = _proposal(html)
    assert _rows(settings, "SELECT match_class FROM enrichment_proposals WHERE id = ?", proposal)[0][0] in (
        "uncertain",
        "multiple",
    )
    direct = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}")
    assert direct.status_code == 303 and direct.headers["location"] == f"/sources/propositions/{proposal}/creer"
    confirm = browser.get(f"/sources/propositions/{proposal}/creer").text
    assert 'name="edition_verifiee"' in confirm and "required" in confirm and "Résultat ambigu" in confirm
    page = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1")
    assert page.status_code == 200
    values = _prefilled(page.text)
    assert values["isbn"] == ""  # ISBN non vérifié : jamais prérempli pour un résultat non exact
    assert "ISBN proposé par la source : 9782070612758" in page.text
    created = browser.post("/catalogue/notices", data=_create_form(settings, values, []))
    assert created.status_code == 303
    assert _rows(settings, "SELECT status, isbn13 FROM edition_isbn_candidates") == [("proposed", PETIT_PRINCE)]
    assert _rows(settings, "SELECT COUNT(*) FROM edition_identifiers WHERE value = ?", PETIT_PRINCE) == [(0,)]


def test_a_verified_isbn_is_prefilled_and_confirmed(client_as, settings, network):
    browser = client_as("librarian")
    proposal = _proposal(_free_search(browser, titre="Le petit prince", auteur="Saint-Exupéry", annee="2015"))
    page = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1&isbn_verifie=1")
    values = _prefilled(page.text)
    assert values["isbn"] == PETIT_PRINCE and values["isbn_verifie"] == "1"
    created = browser.post("/catalogue/notices", data=_create_form(settings, values, []))
    assert created.status_code == 303
    assert _rows(settings, "SELECT status FROM edition_isbn_candidates") == [("confirmed",)]


def test_existing_notices_are_flagged_before_creation(client_as, settings, network):
    existing = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    browser = client_as("librarian")
    proposal = _proposal(_free_search(browser, isbn=PETIT_PRINCE))
    confirm = browser.get(f"/sources/propositions/{proposal}/creer").text
    assert "Ce livre est peut-être déjà au catalogue" in confirm and f"/catalogue/notices/{existing}" in confirm
    assert "même ISBN" in confirm and "enrichir cette notice plutôt" in confirm
    form_page = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1").text
    assert "Notices proches déjà au catalogue" in form_page


def test_creation_needs_the_edit_permission(client_as, settings, network):
    admin = client_as("admin")
    proposal = _proposal(_free_search(admin, isbn=PETIT_PRINCE))
    volunteer = client_as("volunteer")
    assert volunteer.get(f"/sources/propositions/{proposal}/creer").status_code == 403
    assert volunteer.get(f"/catalogue/notices/nouvelle?depuis={proposal}").status_code == 403


def test_an_unknown_or_treated_proposal_cannot_prefill(client_as, settings, network):
    browser = client_as("librarian")
    assert browser.get("/catalogue/notices/nouvelle?depuis=99999").status_code == 404
    assert browser.get("/sources/propositions/99999/creer").status_code == 404
    proposal = _proposal(_free_search(browser, isbn=PETIT_PRINCE))
    page = browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1").text
    browser.post("/catalogue/notices", data=_create_form(settings, _prefilled(page), []))
    assert browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}").status_code == 404


def test_a_form_error_keeps_the_source_banner(client_as, settings, network):
    browser = client_as("librarian")
    proposal = _proposal(_free_search(browser, isbn=PETIT_PRINCE))
    values = _prefilled(browser.get(f"/catalogue/notices/nouvelle?depuis={proposal}&edition_verifiee=1").text)
    values["annee"] = "deux mille sept"
    response = browser.post("/catalogue/notices", data=_create_form(settings, values, []))
    assert response.status_code == 400 and "Prérempli depuis la BnF" in response.text
    assert f'name="depuis" value="{proposal}"' in response.text
    assert _rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", proposal) == [("pending",)]
