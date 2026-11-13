# SPDX-License-Identifier: EUPL-1.2
"""Fiche, création, modification par section, éditions et exemplaires : routes, droits et messages français."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as repo

from ..factories import make_notice

ROLES = ("admin", "librarian", "volunteer", "viewer")
EDITORS = ("admin", "librarian")


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


def _type_id(settings):
    return _rows(settings, "SELECT id FROM document_types WHERE code = 'bd'")[0][0]


def _form(settings, **extra):
    data = {
        "titre": "Le Temple du Soleil",
        "type": str(_type_id(settings)),
        "contributeur_nom": ["Hergé", ""],
        "contributeur_role": ["author", "author"],
        "contributeur_choix": ["", ""],
        "contributeur_dates": ["", ""],
        "langue": "fre",
    }
    data.update(extra)
    return data


# --- Permissions ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
def test_every_role_can_consult(client_as, settings, role):
    work = make_notice(settings, isbn="978-2-07-040850-4")
    browser = client_as(role)
    for path in (
        "/catalogue",
        f"/catalogue/notices/{work}",
        f"/catalogue/notices/{work}?onglet=notice",
        f"/catalogue/notices/{work}?onglet=editions",
        f"/catalogue/notices/{work}?onglet=exemplaires",
        f"/catalogue/notices/{work}?onglet=prets",
        f"/catalogue/notices/{work}?onglet=enrichissement",
        "/catalogue/series",
    ):
        assert browser.get(path).status_code == 200, path


@pytest.mark.parametrize("role", ROLES)
def test_create_page_and_post_require_edit(client_as, settings, role):
    browser = client_as(role)
    page = browser.get("/catalogue/notices/nouvelle")
    created = browser.post("/catalogue/notices", data=_form(settings))
    if role in EDITORS:
        assert page.status_code == 200
        assert created.status_code == 303
    else:
        assert page.status_code == 403
        assert created.status_code == 403
        assert _rows(settings, "SELECT COUNT(*) FROM works") == [(0,)]


@pytest.mark.parametrize("role", ("volunteer", "viewer"))
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/catalogue/notices/{w}/sections/identification"),
        ("POST", "/catalogue/notices/{w}/sections/publication"),
        ("POST", "/catalogue/notices/{w}/archiver"),
        ("POST", "/catalogue/notices/{w}/desarchiver"),
        ("POST", "/catalogue/notices/{w}/supprimer"),
        ("GET", "/catalogue/notices/{w}/editions/nouvelle"),
        ("POST", "/catalogue/notices/{w}/editions/nouvelle"),
        ("POST", "/catalogue/editions/{e}/principale"),
        ("POST", "/catalogue/editions/{e}/archiver"),
        ("POST", "/catalogue/editions/{e}/desarchiver"),
        ("POST", "/catalogue/editions/{e}/supprimer"),
        ("GET", "/catalogue/suggestions/editeurs?editeur=a"),
        ("GET", "/catalogue/contributeurs/choix?prefixe=contributeur"),
        ("GET", "/catalogue/contributeurs/ligne?prefixe=contributeur"),
    ],
)
def test_writing_routes_are_refused_by_direct_url(client_as, settings, role, method, path):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    response = client_as(role).request(method, path.format(w=work, e=edition))
    assert response.status_code == 403, f"{role} {method} {path}"


@pytest.mark.parametrize("role", ("viewer",))
def test_copies_require_holdings_edit(client_as, settings, role):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    assert (
        client_as(role)
        .post(f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 1})
        .status_code
        == 403
    )
    assert _rows(settings, "SELECT COUNT(*) FROM copies") == [(0,)]


def test_volunteer_can_add_a_copy_but_not_edit_the_notice(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("volunteer")
    added = browser.post(f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 1})
    assert added.status_code == 303
    assert browser.post(f"/catalogue/notices/{work}/sections/contenu", data={"resume": "x"}).status_code == 403


def test_buttons_follow_the_permissions(client_as, settings):
    work = make_notice(settings)
    viewer = client_as("viewer").get(f"/catalogue/notices/{work}").text
    assert "Modifier" not in viewer and "Retirer" not in viewer
    editor = client_as("librarian").get(f"/catalogue/notices/{work}").text
    assert "Modifier" in editor and "Retirer" in editor


def test_anonymous_is_sent_to_login(client, settings):
    work = make_notice(settings)
    response = client.get(f"/catalogue/notices/{work}")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/connexion")


def test_csrf_is_required(app, client_as, settings):
    browser = client_as("admin")
    del browser.headers["X-CSRF-Token"]
    assert browser.post("/catalogue/notices", data=_form(settings)).status_code == 403
    assert _rows(settings, "SELECT COUNT(*) FROM works") == [(0,)]


# --- Création ---------------------------------------------------------------------------------------------------------
def test_create_a_complete_notice(client_as, settings):
    browser = client_as("librarian")
    response = browser.post(
        "/catalogue/notices",
        data=_form(
            settings,
            sous_titre="Les Aventures de Tintin",
            serie="Tintin",
            tome="14",
            isbn="978-2-07-040850-4",
            editeur="Casterman",
            collection="Tintin",
            annee="1949",
            pages="62",
            resume="Tintin au Pérou.",
            categories="BD",
            genres="Aventure",
            mots_cles="Pérou, Incas",
        ),
    )
    assert response.status_code == 303
    notice = _notice(settings, int(response.headers["location"].rsplit("/", 1)[1]))
    assert notice.title == "Le Temple du Soleil"
    assert notice.series.volume_text == "14"
    assert notice.primary.isbn13 == ("9782070408504",)
    assert notice.primary.publisher.label == "Casterman"
    assert notice.keywords == ("Incas", "Pérou")
    page = browser.get(response.headers["location"])
    assert "La notice a été créée." in page.text
    assert "Le Temple du Soleil" in page.text
    assert "Casterman" in page.text


def test_create_without_isbn(client_as, settings):
    assert client_as("admin").post("/catalogue/notices", data=_form(settings)).status_code == 303


def test_errors_are_in_french_and_keep_the_input(client_as, settings):
    page = client_as("admin").post(
        "/catalogue/notices", data=_form(settings, titre="", isbn="123", annee="abc", resume="Mon résumé précieux")
    )
    assert page.status_code == 400
    assert "Indiquez un titre." in page.text
    assert "n’est pas un ISBN valide" in page.text
    assert "Indiquez une année" in page.text
    assert "Mon résumé précieux" in page.text
    assert "Hergé" in page.text


def test_homonyms_show_a_choice_on_the_form(client_as, settings):
    browser = client_as("admin")
    make_notice(settings, "A", authors=("Jean Dupont",))
    first = _form(
        settings,
        titre="B",
        contributeur_nom=["Jean Dupont"],
        contributeur_role=["author"],
        contributeur_choix=["new"],
        contributeur_dates=["1950-2020"],
    )
    assert browser.post("/catalogue/notices", data=first).status_code == 303
    second = _form(
        settings,
        titre="C",
        contributeur_nom=["Jean Dupont"],
        contributeur_role=["author"],
        contributeur_choix=[""],
        contributeur_dates=[""],
    )
    page = browser.post("/catalogue/notices", data=second)
    assert page.status_code == 400
    assert "Plusieurs personnes portent ce nom" in page.text
    assert "1950-2020" in page.text
    assert "Nouvelle personne" in page.text


def test_person_choice_fragment_lists_homonyms(client_as, settings):
    make_notice(settings, "A", authors=("René Goscinny",))
    page = client_as("admin").get("/catalogue/contributeurs/choix?prefixe=contributeur&contributeur_nom=rene+goscinny")
    assert "René Goscinny" in page.text
    assert 'name="contributeur_choix"' in page.text


def test_person_choice_for_a_new_name_keeps_the_form_aligned(client_as):
    page = client_as("admin").get("/catalogue/contributeurs/choix?prefixe=contributeur&contributeur_nom=Personne")
    assert '<input type="hidden" name="contributeur_choix"' in page.text


def test_person_fragments_refuse_an_unknown_prefix(client_as):
    assert client_as("admin").get("/catalogue/contributeurs/ligne?prefixe=x").status_code == 404
    assert client_as("admin").get("/catalogue/contributeurs/choix?prefixe=x").status_code == 404


def test_suggestions_escape_and_filter(client_as, settings):
    make_notice(settings, publisher='Éditions "<b>"')
    page = client_as("admin").get("/catalogue/suggestions/editeurs?editeur=edit")
    assert "&lt;b&gt;" in page.text
    assert "<b>" not in page.text
    assert client_as("admin").get("/catalogue/suggestions/editeurs?editeur=zzz").text.strip() == ""
    assert client_as("admin").get("/catalogue/suggestions/inconnu?x=a").status_code == 404


def test_suggestions_for_a_list_use_the_last_term(client_as, settings):
    make_notice(settings, genres="Aventure")
    assert "Aventure" in client_as("admin").get("/catalogue/suggestions/genres?genres=Humour,+av").text


# --- Modification par section -----------------------------------------------------------------------------------------
def test_edit_page_shows_a_form_only_with_permission(client_as, settings):
    work = make_notice(settings, "Titre")
    path = f"/catalogue/notices/{work}?onglet=notice&modifier=identification"
    assert 'name="titre"' in client_as("admin").get(path).text
    assert 'name="titre"' not in client_as("viewer").get(path).text


@pytest.mark.parametrize("section", ("identification", "serie", "publication", "contenu"))
def test_each_section_opens_in_edit_mode(client_as, settings, section):
    work = make_notice(settings)
    page = client_as("admin").get(f"/catalogue/notices/{work}?modifier={section}")
    assert "Mode édition" in page.text
    assert f'action="/catalogue/notices/{work}/sections/{section}"' in page.text


def test_identity_section_saves(client_as, settings):
    work = make_notice(settings, "Avant", authors=("Hergé",))
    notice = _notice(settings, work)
    agent = str(notice.contributors[0].agent_id)
    response = client_as("admin").post(
        f"/catalogue/notices/{work}/sections/identification",
        data=_form(
            settings,
            titre="Après",
            version=notice.version,
            contributeur_nom=["Hergé"],
            contributeur_role=["artist"],
            contributeur_choix=[agent],
            contributeur_dates=[""],
        ),
    )
    assert response.status_code == 303
    after = _notice(settings, work)
    assert after.title == "Après"
    assert after.contributors[0].role == "artist"
    assert after.contributors[0].agent_id == int(agent)


def test_stale_form_is_refused_with_a_clear_message(client_as, settings):
    work = make_notice(settings, "Avant")
    version = _notice(settings, work).version
    browser = client_as("admin")
    data = _form(settings, titre="Premier", version=version)
    assert browser.post(f"/catalogue/notices/{work}/sections/identification", data=data).status_code == 303
    data = _form(settings, titre="Second", version=version)
    response = browser.post(f"/catalogue/notices/{work}/sections/identification", data=data)
    assert response.status_code == 409
    assert "vient d’être modifiée" in response.text
    assert _notice(settings, work).title == "Premier"


def test_section_errors_keep_the_section_open(client_as, settings):
    work = make_notice(settings)
    notice = _notice(settings, work)
    page = client_as("admin").post(
        f"/catalogue/notices/{work}/sections/publication",
        data={"edition_id": notice.primary.id, "version": notice.primary.version, "isbn": "123", "langue": "fre"},
    )
    assert page.status_code == 400
    assert "Mode édition" in page.text
    assert "n’est pas un ISBN valide" in page.text


def test_unknown_section_is_404(client_as, settings):
    work = make_notice(settings)
    assert client_as("admin").post(f"/catalogue/notices/{work}/sections/inconnue", data={}).status_code == 404


def test_missing_notice_is_404(client_as):
    assert client_as("admin").get("/catalogue/notices/9999").status_code == 404
    assert client_as("admin").get("/catalogue/notices/abc").status_code in (400, 422)


# --- Éditions et exemplaires ------------------------------------------------------------------------------------------
def test_add_a_second_edition(client_as, settings):
    work = make_notice(settings, publisher="Casterman", year="1946")
    browser = client_as("admin")
    assert browser.get(f"/catalogue/notices/{work}/editions/nouvelle").status_code == 200
    response = browser.post(
        f"/catalogue/notices/{work}/editions/nouvelle", data={"editeur": "Gallimard", "annee": "2019", "langue": "fre"}
    )
    assert response.status_code == 303
    page = browser.get(f"/catalogue/notices/{work}?onglet=editions").text
    assert "Casterman" in page and "Gallimard" in page and "Édition principale" in page
    assert "Définir comme principale" in page


def test_change_primary_edition(client_as, settings):
    work = make_notice(settings, year="1946")
    browser = client_as("admin")
    browser.post(f"/catalogue/notices/{work}/editions/nouvelle", data={"annee": "2019", "langue": "fre"})
    other = next(e for e in _notice(settings, work).editions if not e.is_primary)
    assert browser.post(f"/catalogue/editions/{other.id}/principale").status_code == 303
    assert _notice(settings, work).primary.id == other.id


def test_primary_edition_refusal_is_explained(client_as, settings):
    work = make_notice(settings)
    primary = _notice(settings, work).primary.id
    page = client_as("admin").post(f"/catalogue/editions/{primary}/supprimer")
    assert page.status_code == 400
    assert "autre édition principale" in page.text


def test_edit_a_secondary_edition(client_as, settings):
    work = make_notice(settings, publisher="Casterman")
    browser = client_as("admin")
    browser.post(f"/catalogue/notices/{work}/editions/nouvelle", data={"editeur": "Gallimard", "langue": "fre"})
    other = next(e for e in _notice(settings, work).editions if not e.is_primary)
    response = browser.post(
        f"/catalogue/notices/{work}/sections/publication",
        data={"edition_id": other.id, "version": other.version, "editeur": "Dupuis", "langue": "fre"},
    )
    assert response.status_code == 303
    notice = _notice(settings, work)
    assert {e.publisher.label for e in notice.editions} == {"Casterman", "Dupuis"}


def test_add_and_remove_copies(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("admin")
    browser.post(f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 2, "note": "Don"})
    page = browser.get(f"/catalogue/notices/{work}?onglet=exemplaires").text
    assert "EX-000001" in page and "EX-000002" in page and "Don" in page
    copy = _notice(settings, work).primary.copies[0].id
    assert browser.post(f"/catalogue/exemplaires/{copy}/supprimer").status_code == 303
    assert len(_notice(settings, work).primary.copies) == 1
    assert browser.post(f"/catalogue/exemplaires/{copy}/supprimer").status_code == 404


def test_a_copy_with_history_is_not_deleted_and_the_refusal_is_explained(client_as, settings):
    from biblyo.modules.catalogue import service
    from biblyo.modules.holdings import service as holdings

    work = make_notice(settings)
    database = Database(settings.database_path)
    (copy,) = service.add_copies(database, _notice(settings, work).primary.id, 1, "", None)
    holdings.change_status(database, copy, "repair", user_id=None)
    database.dispose()
    page = client_as("admin").post(f"/catalogue/exemplaires/{copy}/supprimer")
    assert page.status_code == 400
    assert "seulement retiré du fonds" in page.text
    assert len(_notice(settings, work).primary.copies) == 1


def test_copy_quantity_error_is_shown(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    page = client_as("admin").post(
        f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 99}
    )
    assert page.status_code == 400
    assert "entre 1 et 50" in page.text


def test_copy_refused_for_an_edition_of_another_notice(client_as, settings):
    work = make_notice(settings, "A")
    other = make_notice(settings, "B")
    edition = _notice(settings, other).primary.id
    response = client_as("admin").post(
        f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 1}
    )
    assert response.status_code == 404


# --- Archivage --------------------------------------------------------------------------------------------------------
def test_archive_then_restore(client_as, settings):
    work = make_notice(settings)
    browser = client_as("admin")
    assert browser.post(f"/catalogue/notices/{work}/archiver").status_code == 303
    page = browser.get(f"/catalogue/notices/{work}").text
    assert "Cette notice est archivée" in page
    assert "Réactiver" in page
    assert browser.post(f"/catalogue/notices/{work}/desarchiver").status_code == 303
    assert not _notice(settings, work).is_archived


def test_archive_refused_is_explained(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("admin")
    browser.post(f"/catalogue/notices/{work}/exemplaires", data={"edition_id": edition, "quantite": 1})
    page = browser.post(f"/catalogue/notices/{work}/archiver")
    assert page.status_code == 400
    assert "exemplaires au fonds" in page.text


def test_delete_goes_back_to_the_catalogue(client_as, settings):
    work = make_notice(settings)
    response = client_as("admin").post(f"/catalogue/notices/{work}/supprimer")
    assert response.status_code == 303
    assert response.headers["location"] == "/catalogue"
    assert _rows(settings, "SELECT COUNT(*) FROM works") == [(0,)]


# --- Rendu ------------------------------------------------------------------------------------------------------------
def test_title_is_escaped_everywhere(client_as, settings):
    work = make_notice(settings, '<script>alert("x")</script>', authors=("<img src=x>",), series="<b>S</b>", volume="1")
    browser = client_as("admin")
    for path in (f"/catalogue/notices/{work}", f"/catalogue/notices/{work}?onglet=notice", "/catalogue"):
        page = browser.get(path).text
        assert "<script>alert" not in page, path
        assert "<img src=x>" not in page, path
        assert "<b>S</b>" not in page, path


def test_duplicate_isbn_is_reported_on_the_notice(client_as, settings):
    first = make_notice(settings, "Premier", isbn="978-2-07-040850-4")
    second = make_notice(settings, "Second", isbn="2-07-040850-7")
    page = client_as("admin").get(f"/catalogue/notices/{second}").text
    assert "Doublon probable." in page
    assert f'href="/catalogue/notices/{first}"' in page


def test_planned_tabs_say_so_without_invented_data(client_as, settings):
    work = make_notice(settings)
    browser = client_as("admin")
    enrichment = browser.get(f"/catalogue/notices/{work}?onglet=enrichissement").text  # livré en L5
    assert "Aucune donnée reprise d’une source" in enrichment and "Aucun enrichissement appliqué" in enrichment
    loans = browser.get(f"/catalogue/notices/{work}?onglet=prets").text  # livré en L4 : l'onglet n'est plus « à venir »
    assert "Prêts à venir" not in loans and "Aucun exemplaire de ce livre n’est sorti" in loans


def test_no_inline_style_or_script_in_catalogue_pages(client_as, settings):
    work = make_notice(settings, series="Alix", volume="1", isbn="978-2-07-040850-4")
    browser = client_as("admin")
    for path in (
        "/catalogue",
        "/catalogue/notices/nouvelle",
        f"/catalogue/notices/{work}",
        f"/catalogue/notices/{work}?modifier=identification",
        f"/catalogue/notices/{work}?onglet=editions",
        f"/catalogue/notices/{work}?onglet=exemplaires",
        "/catalogue/series",
    ):
        html = browser.get(path).text
        assert not re.search(r'\sstyle="', html), path
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert not re.search(r'\son[a-z]+="', html), path


def test_cover_zoom_title_is_escaped_once(client_as, settings):
    work = make_notice(settings, "Tom & Jerry <b>")
    page = client_as("viewer").get(f"/catalogue/notices/{work}").text
    assert '<h2 id="zoom-couverture-titre" class="grow">Couverture : Tom &amp; Jerry &lt;b&gt;</h2>' in page
    assert "&amp;amp;" not in page and "Aucune image jointe" in page
