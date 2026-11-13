# SPDX-License-Identifier: EUPL-1.2
"""Lecteurs dans un vrai navigateur : recherche instantanée, vues, sélecteur au clavier et à la douchette, téléphone.

Personnes inventées, courriels en example.org."""

from __future__ import annotations

import re
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from biblyo.core.db import Database
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import GuardianInput, HouseholdChoice, PersonInput, Registration

from ..factories import PASSWORD, make_notice
from .conftest import ADMIN, VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}


@pytest.fixture(scope="module")
def people(server: Server) -> Iterator[SimpleNamespace]:
    database = Database(server.settings.database_path)

    def register(first: str, last: str, category: str = "adult", **options) -> int:
        person = PersonInput(first_name=first, last_name=last, category=category, birth=options.pop("birth", ""))
        return service.register(database, Registration(person=person, homonyms_confirmed=True, **options), None)

    parent = register("Hélène", "Zéphyrin")
    twin = register("Hélène", "Zéphyrin")
    child = register(
        "Oscar",
        "Zéphyrin",
        "minor",
        birth="2016",
        household=HouseholdChoice(new_name="Famille Zéphyrin", kind="family", role="child"),
        guardian=GuardianInput(number="P-000001", relation="parent", can_borrow_for=True, is_contact=True),
    )
    with database.session() as session:
        from biblyo.modules.patrons.models import PatronCard

        card = session.query(PatronCard).filter_by(patron_id=twin).one().number
    yield SimpleNamespace(parent=parent, twin=twin, child=child, twin_card=card)
    database.dispose()


@pytest.fixture
def page(browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": 1440, "height": 900})
    instance = context.new_page()
    yield instance
    context.close()


def _search(page: Page, server: Server, text: str) -> None:
    page.goto(f"{server.base_url}/lecteurs")
    page.locator("#rd-q").press_sequentially(text, delay=15)


def test_instant_search_keeps_the_focus_and_leaves_no_trace(page: Page, server: Server, people) -> None:
    problems = watch(page)
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.url))
    page.goto(f"{server.base_url}/lecteurs")
    page.evaluate("window.marque = 1")
    before = page.evaluate("history.length")
    page.locator("#rd-q").press_sequentially("zephyrin oscar", delay=15)
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.locator("tbody tr")).to_contain_text("Oscar Zéphyrin")
    expect(page.locator("#rd-q")).to_be_focused()
    assert page.evaluate("window.marque") == 1  # aucun rechargement
    expect(page).to_have_url(f"{server.base_url}/lecteurs")
    assert not [url for url in sent if "zephyrin" in url.lower()]
    assert page.evaluate("history.length") == before  # chaque frappe n'empile rien dans l'historique
    stored = page.evaluate("JSON.stringify(Object.assign({}, localStorage, sessionStorage))")
    assert "zephyrin" not in stored.lower() and "Zéphyrin" not in stored
    assert problems == []


def test_chips_and_pages_keep_the_search(page: Page, server: Server, people) -> None:
    _search(page, server, "zephyrin")
    expect(page.locator("tbody tr")).to_have_count(3)
    expect(page.get_by_text("Homonyme", exact=True)).to_have_count(2)
    page.get_by_role("button", name="Mineurs").click()
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.locator("#rd-q")).to_have_value("zephyrin")
    expect(page).to_have_url(f"{server.base_url}/lecteurs?vue=mineurs")  # le filtre, jamais le texte cherché
    page.get_by_role("button", name="Foyers et structures").click()
    expect(page.locator(".hh-card")).to_have_count(1)
    expect(page.locator(".hh-card")).to_contain_text("Famille Zéphyrin")


def test_pagination_by_keyboard_without_any_term_in_the_address(page: Page, server: Server, people) -> None:
    database = Database(server.settings.database_path)
    for index in range(30):
        person = PersonInput(first_name=f"Prenom{index:02d}", last_name="Quenneville", category="adult")
        service.register(database, Registration(person=person, homonyms_confirmed=True), None)
    database.dispose()
    _search(page, server, "quenneville")
    expect(page.locator("tbody tr")).to_have_count(25)
    following = page.get_by_role("button", name="Page suivante")
    following.focus()
    page.keyboard.press("Enter")
    expect(page.locator("tbody tr")).to_have_count(5)
    expect(page.locator(".pager [aria-current=page]")).to_have_text("2")
    expect(page.locator("#rd-q")).to_have_value("quenneville")
    expect(page).to_have_url(f"{server.base_url}/lecteurs")
    page.locator("#rd-par-page").select_option("50")
    expect(page.locator("tbody tr")).to_have_count(30)


def test_views_and_pages_keep_their_address_without_a_search(page: Page, server: Server, people) -> None:
    page.goto(f"{server.base_url}/lecteurs")
    page.get_by_role("button", name="Mineurs").click()
    expect(page).to_have_url(f"{server.base_url}/lecteurs?vue=mineurs")
    page.go_back()
    expect(page).to_have_url(f"{server.base_url}/lecteurs")
    expect(page.get_by_role("button", name="Tous")).to_have_attribute("aria-pressed", "true")


def test_picker_by_keyboard(page: Page, server: Server, people) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/_composants")
    field = page.locator("#demo_lecteur-choix")
    field.press_sequentially("zephyrin", delay=15)
    options = page.locator("#demo_lecteur-options [role=option]")
    expect(options).to_have_count(3)
    expect(field).to_have_attribute("aria-expanded", "true")
    field.press("ArrowDown")
    field.press("ArrowDown")
    expect(options.nth(1)).to_have_attribute("aria-selected", "true")
    chosen = options.nth(1).get_attribute("data-id")
    field.press("Enter")
    expect(page.locator("input[name=demo_lecteur]")).to_have_value(chosen or "")
    expect(page.locator(".picked")).to_be_visible()
    expect(options).to_have_count(0)
    page.locator(".picked button").click()
    expect(page.locator("#demo_lecteur-choix")).to_be_focused()
    expect(page.locator("input[name=demo_lecteur]")).to_have_value("")
    assert problems == []


def test_picker_with_a_scanner_needs_no_wait(page: Page, server: Server, people) -> None:
    page.goto(f"{server.base_url}/_composants")
    field = page.locator("#demo_lecteur-choix")
    field.fill(people.twin_card)  # la douchette tape d'un coup, puis Entrée, avant la fin du délai de recherche
    field.press("Enter")
    expect(page.locator("input[name=demo_lecteur]")).to_have_value(str(people.twin))
    expect(page.locator(".picked")).to_contain_text("Hélène Zéphyrin")


def test_phone_width_has_one_column_and_no_overflow(browser: Browser, server: Server, people) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": 390, "height": 844})
    instance = context.new_page()
    _search(instance, server, "zephyrin")
    expect(instance.locator("tbody tr")).to_have_count(3)
    assert instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    instance.goto(f"{server.base_url}/lecteurs?vue=foyers")
    assert instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    _search(instance, server, "zephyrin")
    expect(instance.get_by_text("naissance 2016").first).to_be_visible()  # les repères restent lisibles sur téléphone
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_directory_with_data_is_accessible(browser: Browser, server: Server, people, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], bypass_csp=True, color_scheme=theme)
    instance = context.new_page()
    for text in ("zephyrin", "zzz"):
        _search(instance, server, text)
        instance.wait_for_load_state("networkidle")
        instance.wait_for_timeout(500)
        results = Axe().run(instance).response["violations"]
        found = [item["id"] for item in results if item["impact"] in BLOCKING]
        assert found == [], text
    instance.goto(f"{server.base_url}/lecteurs?vue=foyers")
    instance.wait_for_load_state("networkidle")
    results = Axe().run(instance).response["violations"]
    assert [item["id"] for item in results if item["impact"] in BLOCKING] == []
    context.close()


def test_register_an_adult_in_two_steps(page: Page, server: Server, people) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/lecteurs/nouveau")
    expect(page.locator("#titre-responsable")).to_be_hidden()
    page.fill("#prenom", "Yvonne")
    page.fill("#nom", "Quillard")
    page.fill("#telephone", "0612345678")
    page.get_by_role("button", name="Vérifier").click()
    expect(page.get_by_role("heading", name="Vérifiez ces informations")).to_be_visible()
    page.get_by_role("button", name="Corriger").click()
    expect(page.locator("#prenom")).to_have_value("Yvonne")
    page.get_by_role("button", name="Vérifier").click()
    page.get_by_role("button", name="Enregistrer").click()
    expect(page.get_by_role("heading", name="Yvonne Quillard")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("06 12 34 56 78")  # masquées tant qu'on ne le demande pas
    page.get_by_role("button", name="Afficher les coordonnées").click()
    expect(page.locator("#zone-coordonnees")).to_contain_text("06 12 34 56 78")
    page.get_by_role("button", name="Masquer").click()
    expect(page.locator("body")).not_to_contain_text("06 12 34 56 78")
    assert problems == []


def test_register_a_minor_shows_guardian_and_hides_contacts(page: Page, server: Server, people) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/lecteurs/nouveau")
    page.get_by_label("Mineur").check()
    expect(page.locator("#titre-responsable")).to_be_visible()
    expect(page.locator("#titre-coordonnees")).to_be_hidden()
    page.fill("#prenom", "Basile")
    page.fill("#nom", "Quillard")
    field = page.locator("#resp_id-choix")
    field.press_sequentially("yvonne quillard", delay=15)
    expect(page.locator("#resp_id-options [role=option]")).to_have_count(1)
    field.press("ArrowDown")
    field.press("Enter")
    expect(page.locator(".picked").first).to_contain_text("Yvonne Quillard")
    page.get_by_label("Peut emprunter pour cet enfant").check()
    page.get_by_role("button", name="Vérifier").click()
    expect(page.locator(".dlist")).to_contain_text("Yvonne Quillard")
    page.get_by_role("button", name="Enregistrer").click()
    expect(page.get_by_role("heading", name="Basile Quillard")).to_be_visible()
    expect(page.locator("body")).to_contain_text("Peut emprunter")
    assert problems == []


@pytest.mark.parametrize("width", [390, 1024])
def test_sheet_and_form_fit_small_screens(browser: Browser, server: Server, people, width: int) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": width, "height": 844})
    instance = context.new_page()
    for path in (f"/lecteurs/{people.child}", f"/lecteurs/{people.parent}", "/lecteurs/nouveau"):
        instance.goto(f"{server.base_url}{path}")
        overflow = instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, path
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_sheet_and_form_are_accessible(browser: Browser, server: Server, people, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], bypass_csp=True, color_scheme=theme)
    instance = context.new_page()
    for path in (f"/lecteurs/{people.child}", f"/lecteurs/{people.parent}", "/lecteurs/nouveau"):
        instance.goto(f"{server.base_url}{path}")
        instance.wait_for_load_state("networkidle")
        results = Axe().run(instance).response["violations"]
        found = [f"{item['id']} {item['nodes'][0]['target']}" for item in results if item["impact"] in BLOCKING]
        assert found == [], path
    context.close()


def test_family_in_one_entry_then_guardian_rights(page: Page, server: Server, people) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/foyers/nouveau")
    page.fill("#nom_foyer", "Famille Vasseur")
    page.fill("#a0_prenom", "Agathe")
    page.fill("#a0_nom", "Vasseur")
    page.fill("#a0_telephone", "0612345678")
    page.locator("#a0_emprunte-1").check()
    page.fill("#e0_prenom", "Milo")
    page.fill("#e0_nom", "Vasseur")
    page.fill("#e0_naissance", "2017")
    page.get_by_role("button", name="Vérifier").click()
    expect(page.get_by_role("heading", name="Famille Vasseur")).to_be_visible()
    page.get_by_role("button", name="Enregistrer").click()
    expect(page.get_by_role("heading", name="Famille Vasseur", exact=True)).to_be_visible()
    expect(page.locator("body")).to_contain_text("aucun droit")
    page.get_by_role("link", name="Milo Vasseur").click()
    expect(page.locator("body")).to_contain_text("Peut emprunter")
    summary = page.locator("summary[aria-label^='Modifier les droits']")
    summary.click()
    page.locator("input[name=contact]").first.check()
    page.get_by_role("button", name="Enregistrer les droits").click()
    expect(page.locator("body")).to_contain_text("À contacter")
    assert problems == []


def test_structure_gets_a_referent_through_the_picker(page: Page, server: Server, people) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/foyers/structure")
    page.fill("#nom", "École du Parc")
    page.get_by_role("button", name="Créer").click()
    expect(page.get_by_role("heading", name="École du Parc")).to_be_visible()
    page.get_by_text("Ajouter un référent ou un membre").click()
    field = page.locator("#membre_id-choix")
    field.press_sequentially("zephyrin helene", delay=15)
    expect(page.locator("#membre_id-options [role=option]")).to_have_count(2)
    field.press("ArrowDown")
    field.press("Enter")
    page.get_by_role("button", name="Ajouter", exact=True).click()
    expect(page.locator("body")).to_contain_text("Référent")
    assert problems == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_family_and_household_pages_are_accessible(browser: Browser, server: Server, people, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], bypass_csp=True, color_scheme=theme)
    instance = context.new_page()
    for path in ("/foyers/nouveau", "/foyers/structure", "/foyers/1"):
        instance.goto(f"{server.base_url}{path}")
        instance.wait_for_load_state("networkidle")
        results = Axe().run(instance).response["violations"]
        found = [f"{item['id']} {item['nodes'][0]['target']}" for item in results if item["impact"] in BLOCKING]
        assert found == [], path
    context.close()


@pytest.mark.parametrize("width", [390, 1024])
def test_family_and_household_fit_small_screens(browser: Browser, server: Server, people, width: int) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": width, "height": 844})
    instance = context.new_page()
    for path in ("/foyers/nouveau", "/foyers/1"):
        instance.goto(f"{server.base_url}{path}")
        overflow = instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, path
    context.close()


def test_export_then_anonymize_in_the_browser(page: Page, server: Server, people) -> None:
    problems = watch(page)
    database = Database(server.settings.database_path)
    person = PersonInput(first_name="Rémi", last_name="Tessier", category="adult", email="remi.tessier@example.org")
    patron = service.register(database, Registration(person=person), None)
    database.dispose()
    page.goto(f"{server.base_url}/lecteurs/{patron}")
    page.get_by_role("link", name="Exporter ses données").click()
    expect(page.locator("body")).to_contain_text("remi.tessier@example.org")
    with page.expect_download() as download:
        page.get_by_role("link", name="Télécharger le fichier").click()
    assert download.value.suggested_filename.startswith("P-") and download.value.suggested_filename.endswith(".json")
    page.goto(f"{server.base_url}/lecteurs/{patron}")
    page.get_by_role("link", name="Anonymiser…").click()
    expect(page.locator("body")).to_contain_text("jusqu’à leur suppression")
    number = page.locator(".ph p").inner_text().split(" · ")[-1]
    page.locator("#motif").select_option("request")
    page.fill("#numero", number)
    page.fill("#mot_de_passe", PASSWORD)
    page.get_by_role("button", name="Anonymiser définitivement").click()
    expect(page.get_by_role("heading", name="Lecteur anonymisé")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Tessier")
    # WebKit signale un téléchargement comme un chargement interrompu : c'est son fonctionnement normal.
    assert [problem for problem in problems if "export.json" not in problem] == []


def test_lifecycle_pages_are_accessible_and_fit(browser: Browser, server: Server, people) -> None:
    for width, theme in ((390, "light"), (1440, "dark")):
        context = browser.new_context(
            storage_state=server.storage[ADMIN],
            bypass_csp=True,
            color_scheme=theme,
            viewport={"width": width, "height": 844},
        )
        instance = context.new_page()
        for path in (
            f"/lecteurs/{people.parent}/anonymiser",
            f"/lecteurs/{people.parent}/export",
            "/lecteurs/a-traiter",
        ):
            instance.goto(f"{server.base_url}{path}")
            instance.wait_for_load_state("networkidle")
            overflow = instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 0, path
            results = Axe().run(instance).response["violations"]
            found = [f"{item['id']} {item['nodes'][0]['target']}" for item in results if item["impact"] in BLOCKING]
            assert found == [], path
        context.close()


def _open_palette(page: Page) -> None:
    page.locator(".cmdk-btn").click()
    expect(page.locator("#palette")).to_be_visible()


def test_palette_finds_homonyms_and_opens_the_sheet_by_keyboard(page: Page, server: Server, people) -> None:
    problems = watch(page)
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.url))
    page.goto(f"{server.base_url}/")
    _open_palette(page)
    box = page.locator("#palette-q")
    box.press_sequentially("helene zephyrin", delay=15)
    results = page.locator("#palette-lecteurs a[data-server]")
    expect(results).to_have_count(2)
    expect(results.first).to_contain_text("Homonyme")
    expect(results.first).to_contain_text(re.compile(r"P-\d{6}"))  # numéro permanent, quel que soit le rang
    target = results.first.get_attribute("href")
    assert not [url for url in sent if "zephyrin" in url.lower()]  # le texte tapé ne passe dans aucune adresse
    for _step in range(page.locator("#palette .it:visible").count()):
        if page.locator("#palette .it.on").get_attribute("href") == target:
            break
        box.press("ArrowDown")
    box.press("Enter")
    expect(page).to_have_url(f"{server.base_url}{target}")
    assert problems == []


def test_palette_finds_a_reader_by_card(page: Page, server: Server, people) -> None:
    page.goto(f"{server.base_url}/")
    _open_palette(page)
    page.locator("#palette-q").press_sequentially(people.twin_card, delay=10)
    expect(page.locator("#palette-lecteurs a[data-server]")).to_have_count(1)
    expect(page.locator("#palette-lecteurs a[data-server]")).to_have_attribute("href", f"/lecteurs/{people.twin}")


def test_palette_still_finds_notices_next_to_readers(page: Page, server: Server, people) -> None:
    database = Database(server.settings.database_path)
    person = PersonInput(first_name="Carole", last_name="Licorne", category="adult")
    service.register(database, Registration(person=person), None)
    database.dispose()
    make_notice(server.settings, "Le Secret de la Licorne")
    page.goto(f"{server.base_url}/")
    _open_palette(page)
    page.locator("#palette-q").press_sequentially("licorne", delay=15)
    expect(page.locator("#palette-notices a[data-server]")).to_have_count(1)
    expect(page.locator("#palette-lecteurs a[data-server]")).to_have_count(1)


def test_palette_for_a_volunteer(browser: Browser, server: Server, people) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport={"width": 1440, "height": 900})
    instance = context.new_page()
    instance.goto(f"{server.base_url}/")
    _open_palette(instance)
    instance.locator("#palette-q").press_sequentially("zephyrin", delay=15)
    expect(instance.locator("#palette-lecteurs a[data-server]")).to_have_count(3)
    assert "example.org" not in instance.locator("#palette-lecteurs").inner_html()
    context.close()


@pytest.mark.parametrize(("width", "theme"), [(390, "light"), (1440, "dark")])
def test_palette_with_readers_is_accessible_and_fits(
    browser: Browser, server: Server, people, width: int, theme: str
) -> None:
    context = browser.new_context(
        storage_state=server.storage[ADMIN],
        bypass_csp=True,
        color_scheme=theme,
        viewport={"width": width, "height": 844},
    )
    instance = context.new_page()
    instance.goto(f"{server.base_url}/lecteurs")
    instance.get_by_role("button", name=re.compile("Rechercher")).first.click()
    expect(instance.locator("#palette")).to_be_visible()
    instance.locator("#palette-q").press_sequentially("zephyrin", delay=15)
    expect(instance.locator("#palette-lecteurs a[data-server]")).to_have_count(3)
    assert instance.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    results = Axe().run(instance).response["violations"]
    found = [f"{item['id']} {item['nodes'][0]['target']}" for item in results if item["impact"] in BLOCKING]
    assert found == []
    context.close()
