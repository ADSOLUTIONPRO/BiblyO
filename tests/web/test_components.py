# SPDX-License-Identifier: EUPL-1.2
"""Design system : chaque composant se rend, avec ses libellés et ses attributs d'accessibilité."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core.pagination import page_numbers
from biblyo.core.templating import Templates


@pytest.fixture
def env():
    return Templates().env


def _macro(env, module: str, name: str):
    return getattr(env.get_template(f"components/{module}.html").module, name)


def test_catalogue_renders_for_a_connected_account(client_as):
    response = client_as("viewer").get("/_composants")
    assert response.status_code == 200
    for fragment in ("Principal", "Champ en erreur", "En retard", "Fenêtre modale", "Aucun résultat", 'role="img"'):
        assert fragment in response.text


def test_catalogue_requires_a_session(client):
    assert client.get("/_composants").status_code == 303


def test_catalogue_is_404_in_production(settings, client_as):
    browser = client_as("admin")
    settings.env = "production"
    with TestClient(create_app(settings), raise_server_exceptions=False, follow_redirects=False) as production:
        assert production.get("/_composants").status_code == 404  # même sans session
        production.cookies.update(browser.cookies)
        assert production.get("/_composants").status_code == 404


@pytest.mark.parametrize("variant", ["p", "s", "g", "d", "dg"])
def test_button_variants(env, variant):
    html = _macro(env, "buttons", "button")("Enregistrer", variant)
    assert f"btn-{variant}" in html
    assert 'type="button"' in html
    assert "Enregistrer" in html


def test_button_states(env):
    button = _macro(env, "buttons", "button")
    assert "disabled" in button("Non", disabled=True)
    assert "btn-sm" in button("Petit", size="sm")
    assert "<kbd>P</kbd>" in button("Prêt", kbd="P")
    assert 'type="submit"' in button("Envoyer", type="submit")
    assert 'aria-hidden="true"' in button("Avec icône", icon_name="scan")


def test_link_button_is_a_link(env):
    html = _macro(env, "buttons", "link_button")("Voir", "/page", "s")
    assert html.startswith('<a href="/page"')


def test_icon_button_has_an_accessible_name(env):
    assert 'aria-label="Rechercher"' in _macro(env, "buttons", "icon_button")("Rechercher", "search")


def test_field_links_label_help_and_error(env):
    html = _macro(env, "forms", "field")("nom", "Nom", help="Aide", error="Erreur", required=True)
    assert '<label for="nom">' in html
    assert 'id="nom"' in html
    assert 'aria-invalid="true"' in html
    assert 'aria-describedby="nom-aide nom-erreur"' in html
    assert 'id="nom-aide"' in html
    assert 'id="nom-erreur"' in html
    assert " required" in html


def test_field_without_message_has_no_dangling_reference(env):
    html = _macro(env, "forms", "field")("nom", "Nom")
    assert "aria-describedby" not in html
    assert "aria-invalid" not in html


def test_password_field_never_echoes_its_value(env):
    html = _macro(env, "forms", "field")("secret", "Mot de passe", value="valeur-secrete", type="password")
    assert "valeur-secrete" not in html


def test_field_value_is_escaped(env):
    template = env.from_string('{% from "components/forms.html" import field %}{{ field("n", "Nom", value=value) }}')
    html = template.render(value='"><script>x</script>')
    assert "<script>" not in html
    assert "&#34;&gt;&lt;script&gt;" in html


def test_select_marks_the_choice(env):
    html = _macro(env, "forms", "select")("r", "Rôle", [("a", "Un"), ("b", "Deux")], "b")
    assert '<option value="b" selected>' in html
    assert '<option value="a">' in html


def test_checkbox_states(env):
    checkbox = _macro(env, "forms", "checkbox")
    assert " checked" in checkbox("c", "Case", "1", True)
    assert " disabled" in checkbox("c", "Case", "1", disabled=True)
    assert 'for="c-1"' in checkbox("c", "Case", "1")


def test_textarea(env):
    html = _macro(env, "forms", "textarea")("t", "Texte", value="Contenu", error="Erreur")
    assert ">Contenu</textarea>" in html
    assert 'aria-invalid="true"' in html


@pytest.mark.parametrize("kind", ["ok", "loan", "due", "late", "info", "neutral"])
def test_badge_carries_a_label_not_only_a_colour(env, kind):
    html = _macro(env, "feedback", "badge")("Disponible", kind)
    assert f"m-{kind}" in html
    assert "Disponible" in html


@pytest.mark.parametrize(("kind", "role"), [("info", "status"), ("ok", "status"), ("due", "status"), ("late", "alert")])
def test_alert_roles(env, kind, role):
    assert f'role="{role}"' in _macro(env, "feedback", "alert")("Message", kind)


def test_toast_can_be_closed(env):
    html = _macro(env, "feedback", "toast")("Enregistré")
    assert "data-close-toast" in html
    assert "Fermer" in html


def test_empty_state_and_skeleton(env):
    assert "Rien ici" in _macro(env, "feedback", "empty_state")("search", "Rien ici")
    assert 'aria-hidden="true"' in _macro(env, "feedback", "skeleton")(2)


def test_ring_and_bar_have_no_inline_style(env):
    ring = _macro(env, "layout", "ring")(68, "68 % rangés", 72, "she")
    bar = _macro(env, "layout", "progress_bar")(40, "40 % vérifiés", "inv")
    for html, label in ((ring, "68 % rangés"), (bar, "40 % vérifiés")):
        assert 'role="img"' in html
        assert f'aria-label="{label}"' in html
        assert "style=" not in html


def test_page_header_has_the_single_heading(env):
    html = _macro(env, "layout", "page_header")("Titre", "Sous-titre", "cat", "book")
    assert len(re.findall(r"<h1\b", html)) == 1
    assert "m-cat" in html


def test_kpi_tile_is_a_link_only_with_a_target(env):
    tile = _macro(env, "layout", "kpi_tile")
    assert tile("Titres", 3, href="/catalogue").startswith("<a ")
    assert tile("Titres", 3).startswith("<div ")


def test_tabs_mark_the_current_one(env):
    items = [{"label": "Un", "href": "/a", "current": True, "count": 2}, {"label": "Deux", "href": "/b"}]
    html = _macro(env, "layout", "tabs")(items, "Exemple")
    assert 'aria-label="Exemple"' in html
    assert html.count('aria-current="page"') == 1
    assert "<em>2</em>" in html


def test_chip_and_segmented(env):
    assert 'aria-current="true"' in _macro(env, "layout", "chip")("Filtre", "/a", True)
    html = _macro(env, "layout", "segmented")([{"label": "A", "href": "/a", "current": True}], "Affichage")
    assert 'role="group"' in html


def test_pagination(env):
    pagination = _macro(env, "layout", "pagination")
    html = pagination(6, page_numbers(6, 12), "/liste?page=")
    assert 'aria-label="Pagination"' in html
    assert '<span aria-current="page">6</span>' in html
    assert 'rel="prev"' in html
    assert 'rel="next"' in html
    assert "…" in html
    assert 'rel="prev"' not in pagination(1, page_numbers(1, 3), "/l?p=")
    assert 'rel="next"' not in pagination(3, page_numbers(3, 3), "/l?p=")
    assert "<nav" not in pagination(1, page_numbers(1, 1), "/l?p=")


def test_page_numbers():
    assert page_numbers(1, 0) == []
    assert page_numbers(1, 1) == [1]
    assert page_numbers(2, 3) == [1, 2, 3]
    assert page_numbers(6, 12) == [1, None, 5, 6, 7, None, 12]
    assert page_numbers(1, 12) == [1, 2, None, 12]


def test_modal_and_sheet_are_native_dialogs(env):
    template = env.from_string(
        '{% from "components/overlays.html" import modal, sheet %}'
        '{% call modal("m", "Titre") %}Corps{% endcall %}{% call sheet("s", "Titre") %}Corps{% endcall %}'
    )
    html = template.render()
    assert '<dialog class="modal" id="m" aria-labelledby="m-titre">' in html
    assert '<dialog class="sheet" id="s" aria-labelledby="s-titre">' in html
    assert html.count("data-close-dialog") == 2
    assert html.count('aria-label="Fermer"') == 2


def test_dropdown_declares_its_alpine_component(env):
    template = env.from_string(
        '{% from "components/overlays.html" import dropdown %}'
        '{% call dropdown("Menu") %}<a class="mi-i" href="/">Lien</a>{% endcall %}'
    )
    html = template.render()
    assert 'x-data="dropdown"' in html
    assert ':aria-expanded="expanded"' in html
    assert " hidden " in html  # fermé sans JavaScript


def test_icon_is_decorative_unless_labelled(env):
    icon = _macro(env, "icons", "icon")
    assert 'aria-hidden="true"' in icon("home")
    labelled = icon("home", 18, "Accueil")
    assert 'role="img"' in labelled
    assert 'aria-label="Accueil"' in labelled


# --- Étagères (lot L2) ------------------------------------------------------------------------------------------------
def _shelves():
    from biblyo.modules.devtools.routes import shelf_demo

    return shelf_demo()


def test_shelf_unit_is_an_accessible_drawing(env):
    unit = _shelves()["demo_unit"]
    html = _macro(env, "shelves", "shelf_unit")(unit, heading=2)
    assert "style=" not in html
    assert html.count("<ol class=\"spines\">") == 3  # le bac vide n'a pas de liste
    assert "Aucun exemplaire sur ce niveau." in html
    assert 'aria-label="Position 1 : Le Village des brumes, tome 1, cote BD VIL 1, en place"' in html
    assert 'aria-label="Position 3 : Le Lac des brumes, tome 3, cote BD VIL 1, en réparation"' in html
    assert html.count('aria-current="location"') == 1
    assert "dépassée de 2" in html and "+14 places" in html
    assert html.count('class="slot"') == 6 + 6  # six places sur l'étagère 1, six (puis « +14 ») dans le bac
    assert "Étagère 2 · 4/4<span class=\"sr-only\"> — 4 / 4 places, pleine</span>" in html
    assert '<h2 id="unit-1-t">' in html and '<h3 class="level-lab"' in html
    assert "g-start" in html and "g-end" in html


def test_location_tree_and_room_plan(env):
    demo = _shelves()
    tree = _macro(env, "shelves", "location_tree")(demo["demo_tree"], "Emplacements")
    assert '<nav class="loc-tree" aria-label="Emplacements">' in tree
    assert tree.count('aria-current="page"') == 1
    assert "6/4" in tree and "désactivé" in tree
    plan = _macro(env, "shelves", "room_plan")(demo["demo_room"], "Plan")
    assert "is-plan [--room-cols:8]" in plan
    assert "md:row-start-3 md:col-start-1 md:row-span-2 md:col-span-1" in plan
    assert "Hors plan" in plan and "Présentoir" in plan
    assert "style=" not in tree + plan


def test_fill_bar_is_svg_without_style(env):
    from biblyo.modules.holdings.rules import Fill

    fill_bar = _macro(env, "shelves", "fill_bar")
    assert 'width="50"' in fill_bar(Fill(5, 10), "5 sur 10") and 'role="img"' in fill_bar(Fill(5, 10), "5 sur 10")
    assert 'aria-hidden="true"' in fill_bar(Fill(5, 10))
    assert "fill-over" in fill_bar(Fill(12, 10))
    assert fill_bar(Fill(5, None)).strip() == ""


def test_component_page_shows_the_shelves(client_as):
    page = client_as("viewer").get("/_composants").text
    assert "Meuble BD 1" in page and "Arbre des emplacements" in page and "Plan de la salle" in page
