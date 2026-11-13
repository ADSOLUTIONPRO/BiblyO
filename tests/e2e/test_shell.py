# SPDX-License-Identifier: EUPL-1.2
"""Coquille : trois formes selon la largeur, clavier, thème, fenêtres, aucune violation de CSP."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from .conftest import ADMIN, NEXT_STOP, Server, watch

pytestmark = pytest.mark.e2e

PAGES = ("/", "/compte", "/admin/utilisateurs", "/admin/utilisateurs/nouveau", "/_composants", "/a-venir/statistiques")


@pytest.mark.parametrize("width", [320, 390, 700, 701, 1024, 1180, 1181, 1440, 1920])
def test_no_horizontal_overflow(admin_page: Page, server: Server, width: int) -> None:
    admin_page.set_viewport_size({"width": width, "height": 800})
    for path in PAGES:
        admin_page.goto(f"{server.base_url}{path}")
        overflow = admin_page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"{path} déborde de {overflow} px à {width} px"


@pytest.mark.parametrize(
    ("width", "sidebar", "labels", "bottom"),
    [(390, False, False, True), (1024, True, False, False), (1440, True, True, False)],
)
def test_shell_form_by_width(
    admin_page: Page, server: Server, width: int, sidebar: bool, labels: bool, bottom: bool
) -> None:
    admin_page.set_viewport_size({"width": width, "height": 900})
    admin_page.goto(f"{server.base_url}/")
    side = admin_page.locator(".side")
    if sidebar:
        expect(side).to_be_visible()
        assert side.bounding_box()["width"] == (252 if labels else 76)
    else:
        expect(side).to_be_hidden()
    assert admin_page.locator(".side .wm").is_visible() is labels
    assert admin_page.locator(".bnav").is_visible() is bottom


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (1440, 900), (1366, 768), (1280, 720), (1024, 768)])
def test_sidebar_footer_stays_on_screen(admin_page: Page, server: Server, width: int, height: int) -> None:
    admin_page.set_viewport_size({"width": width, "height": height})
    admin_page.goto(f"{server.base_url}/")
    theme = admin_page.locator(".side-foot button.nav-i").bounding_box()
    assert theme["y"] + theme["height"] <= height
    last = admin_page.locator(".side-scroll .nav-i").last
    last.scroll_into_view_if_needed()
    bottom = last.bounding_box()["y"] + last.bounding_box()["height"]
    assert bottom <= admin_page.locator(".side-foot").bounding_box()["y"] + 1


def test_sidebar_does_not_scroll_at_reference_size(admin_page: Page, server: Server) -> None:
    admin_page.set_viewport_size({"width": 1440, "height": 900})
    admin_page.goto(f"{server.base_url}/")
    excess = admin_page.evaluate(
        "(() => { const e = document.querySelector('.side-scroll'); return e.scrollHeight - e.clientHeight; })()"
    )
    assert excess <= 0


def test_very_short_screen_keeps_every_entry_reachable(admin_page: Page, server: Server) -> None:
    admin_page.set_viewport_size({"width": 1440, "height": 480})
    admin_page.goto(f"{server.base_url}/")
    theme = admin_page.locator(".side-foot button.nav-i")
    theme.scroll_into_view_if_needed()
    assert theme.bounding_box()["y"] + theme.bounding_box()["height"] <= 480


def test_no_csp_violation_or_script_error(admin_page: Page, server: Server) -> None:
    problems = watch(admin_page)
    for path in (*PAGES, "/a-propos", "/nexiste-pas"):
        admin_page.goto(f"{server.base_url}{path}")
        admin_page.wait_for_load_state("load")
    assert problems == []


def test_fonts_and_stylesheet_are_local(admin_page: Page, server: Server) -> None:
    requested: list[str] = []
    admin_page.on("request", lambda request: requested.append(request.url))
    admin_page.goto(f"{server.base_url}/")
    admin_page.wait_for_load_state("networkidle")
    assert requested
    assert all(url.startswith((server.base_url, "data:")) for url in requested), requested
    assert admin_page.evaluate("document.fonts.check('600 14px Manrope')")
    assert admin_page.evaluate("document.fonts.check('650 20px \"Bricolage Grotesque\"')")


def test_skip_link_is_the_first_stop(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.keyboard.press(NEXT_STOP)
    focused = admin_page.locator(":focus")
    expect(focused).to_have_text("Aller au contenu")
    admin_page.keyboard.press("Enter")
    admin_page.wait_for_function("location.hash === '#contenu'")


def test_keyboard_reaches_navigation_with_visible_focus(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.keyboard.press(NEXT_STOP)
    admin_page.keyboard.press(NEXT_STOP)
    outline = admin_page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline == "solid"


def test_account_menu_with_the_keyboard(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    button = admin_page.locator("button.av")
    button.focus()
    expect(button).to_have_attribute("aria-expanded", "false")
    admin_page.keyboard.press("Enter")
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(admin_page.locator(":focus")).to_have_text("Mon compte")
    admin_page.keyboard.press("ArrowDown")
    expect(admin_page.locator(":focus")).to_have_text("Utilisateurs")
    admin_page.keyboard.press("Escape")
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(button).to_be_focused()


def test_account_menu_closes_on_outside_click(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.click("button.av")
    menu = admin_page.locator(".top .menu")
    expect(menu).to_be_visible()
    admin_page.click("main h1")
    expect(menu).to_be_hidden()


def test_palette_filters_and_navigates(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.keyboard.press("Control+k")
    palette = admin_page.locator("#palette")
    expect(palette).to_be_visible()
    entry = palette.locator("input")
    expect(entry).to_be_focused()
    admin_page.keyboard.type("utilis")
    expect(palette.locator(".it:visible")).to_have_count(1)
    admin_page.keyboard.press("Enter")
    admin_page.wait_for_url(f"{server.base_url}/admin/utilisateurs")


def test_palette_says_when_nothing_matches_and_closes_with_escape(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.click(".cmdk-btn")
    palette = admin_page.locator("#palette")
    expect(palette).to_be_visible()
    admin_page.keyboard.type("zzzzz")
    expect(palette.get_by_text("Aucun résultat")).to_be_visible()
    admin_page.keyboard.press("Escape")
    expect(palette).to_be_hidden()


def test_palette_ignores_accents(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.keyboard.press("/")
    expect(admin_page.locator("#palette")).to_be_visible()
    admin_page.keyboard.type("etageres")
    expect(admin_page.locator("#palette .it:visible")).to_have_text("Étagères")


def test_keyboard_shortcuts(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.locator("main h1").click()
    admin_page.keyboard.press("?")
    expect(admin_page.locator("#raccourcis")).to_be_visible()
    admin_page.keyboard.press("Escape")
    admin_page.keyboard.press("g")
    admin_page.keyboard.press("u")
    admin_page.wait_for_url(f"{server.base_url}/admin/utilisateurs")


def test_unusual_key_after_g_is_harmless(admin_page: Page, server: Server) -> None:
    problems = watch(admin_page)
    admin_page.goto(f"{server.base_url}/")
    admin_page.keyboard.press("g")
    admin_page.keyboard.press('"')
    admin_page.keyboard.press("g")
    admin_page.keyboard.press("]")
    assert problems == []


def test_shortcuts_stay_quiet_while_typing(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/compte")
    admin_page.fill("#nom", "")
    admin_page.locator("#nom").type("g?u/")
    expect(admin_page.locator("#nom")).to_have_value("g?u/")
    expect(admin_page.locator("#raccourcis")).to_be_hidden()
    expect(admin_page.locator("#palette")).to_be_hidden()


def test_theme_toggle_persists_without_flash(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    root = admin_page.locator("html")
    expect(root).to_have_attribute("data-theme", "light")
    background = admin_page.evaluate("getComputedStyle(document.body).backgroundColor")
    admin_page.click(".side-foot button.nav-i")
    expect(root).to_have_attribute("data-theme", "dark")
    assert admin_page.evaluate("getComputedStyle(document.body).backgroundColor") != background
    expect(admin_page.locator(".side-foot button.nav-i .lb")).to_have_text("Thème clair")
    response = admin_page.goto(f"{server.base_url}/compte")
    assert '<html lang="fr" data-theme="dark">' in response.text()
    expect(root).to_have_attribute("data-theme", "dark")


def test_system_theme_applies_without_a_choice(browser: Browser, server: Server) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], color_scheme="dark")
    page = context.new_page()
    page.goto(f"{server.base_url}/")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    context.close()


def test_phone_sheet_lists_pages_and_returns_focus(admin_page: Page, server: Server) -> None:
    admin_page.set_viewport_size({"width": 390, "height": 844})
    admin_page.goto(f"{server.base_url}/")
    opener = admin_page.locator(".bnav button[data-open-dialog=plus]")
    opener.click()
    sheet = admin_page.locator("#plus")
    expect(sheet).to_be_visible()
    expect(sheet.get_by_role("link", name="Utilisateurs")).to_be_visible()
    expect(sheet.get_by_role("link", name="Statistiques")).to_contain_text("À venir")
    admin_page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(opener).to_be_focused()


def test_phone_touch_targets(admin_page: Page, server: Server) -> None:
    admin_page.set_viewport_size({"width": 390, "height": 844})
    admin_page.goto(f"{server.base_url}/")
    for element in admin_page.locator(".bnav a, .bnav button").all():
        assert element.bounding_box()["height"] >= 48


def test_modal_closes_by_button_and_backdrop(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/_composants")
    modal = admin_page.locator("#c-modale")
    admin_page.get_by_role("button", name="Ouvrir une fenêtre").click()
    expect(modal).to_be_visible()
    modal.get_by_role("button", name="Fermer").click()
    expect(modal).to_be_hidden()
    admin_page.get_by_role("button", name="Ouvrir une fenêtre").click()
    expect(modal).to_be_visible()
    admin_page.mouse.click(5, 5)
    expect(modal).to_be_hidden()


def test_toast_after_redirect_can_be_closed(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/compte")
    admin_page.click("form[action='/compte/profil'] button[type=submit]")
    toast = admin_page.locator("#toasts .toast")
    expect(toast).to_contain_text("Votre profil a été mis à jour.")
    toast.get_by_role("button", name="Fermer").click()
    expect(toast).to_have_count(0)


def test_toast_from_server_event(admin_page: Page, server: Server) -> None:
    admin_page.goto(f"{server.base_url}/")
    admin_page.evaluate(
        "document.dispatchEvent(new CustomEvent('toast', {detail: {kind: 'ok', text: '<b>Texte</b> sûr'}}))"
    )
    toast = admin_page.locator("#toasts .toast")
    expect(toast).to_contain_text("<b>Texte</b> sûr")  # affiché comme du texte, jamais interprété


def test_replacing_tokens_changes_the_look_without_touching_screens(admin_page: Page, server: Server) -> None:
    """Critère du PC3 : mêmes gabarits, autres jetons, autre apparence. Thème de test servi à la place du vrai."""

    def with_test_theme(route: Route) -> None:
        response = route.fetch()
        theme = ":root,[data-theme]{--accent:#FF00AA;--side-bg:#003300;--bg:#FFFFCC;--surface:#EEEEFF;--line:#00AAFF}"
        route.fulfill(response=response, body=response.text() + theme)

    selector = "form[action='/compte/profil'] button[type=submit]"
    admin_page.goto(f"{server.base_url}/compte")
    original_html = admin_page.locator(selector).evaluate("element => element.outerHTML")
    original = admin_page.evaluate(f"getComputedStyle(document.querySelector({selector!r})).backgroundColor")

    admin_page.route("**/static/dist/app.css*", with_test_theme)
    admin_page.goto(f"{server.base_url}/compte")
    assert admin_page.locator(selector).evaluate("element => element.outerHTML") == original_html
    colours = admin_page.evaluate(
        f"""({{
            button: getComputedStyle(document.querySelector({selector!r})).backgroundColor,
            side: getComputedStyle(document.querySelector('.side')).backgroundColor,
            body: getComputedStyle(document.body).backgroundColor,
            card: getComputedStyle(document.querySelector('.card')).backgroundColor,
        }})"""
    )
    assert colours["button"] != original
    assert colours == {
        "button": "rgb(255, 0, 170)",
        "side": "rgb(0, 51, 0)",
        "body": "rgb(255, 255, 204)",
        "card": "rgb(238, 238, 255)",
    }
