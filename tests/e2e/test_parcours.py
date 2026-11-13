# SPDX-License-Identifier: EUPL-1.2
"""PA-1 : jeton, installation, connexion, création d'un bénévole, refus d'accès, déconnexion. Et le mode maintenance."""

from __future__ import annotations

import re
import urllib.request

import pytest
from playwright.sync_api import Browser, expect

from .conftest import NEXT_STOP, Server, watch

pytestmark = pytest.mark.e2e
ADMIN_PASSWORD = "Lecture-Partagee-42"
NEW_PASSWORD = "Comptoir-Du-Mardi-77"


def test_maintenance_then_installation_then_refusal(browser: Browser, empty_server: Server) -> None:
    base = empty_server.base_url
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    problems = watch(page)

    # Base absente : maintenance, aucun schéma créé.
    response = page.goto(f"{base}/")
    assert response.status == 503
    expect(page.locator("h1")).to_have_text("biblyO est en maintenance")
    expect(page.get_by_text("Base de données non initialisée")).to_be_visible()
    assert not empty_server.settings.database_path.exists()
    assert page.request.get(f"{base}/sante").status == 503

    # Mise à jour explicite : l'application sort de maintenance sans redémarrage.
    upgraded = empty_server.biblyo("db", "upgrade")
    assert upgraded.returncode == 0, upgraded.stderr
    token_output = empty_server.biblyo("install", "token")
    assert token_output.returncode == 0, token_output.stderr
    token = re.search(r"Jeton d’installation : (\S+)", token_output.stdout).group(1)
    page.wait_for_timeout(2100)  # durée de vie de l'état du schéma en développement

    # Installation : un mauvais jeton est refusé, le bon est à usage unique.
    response = page.goto(f"{base}/")
    assert response.status == 200
    page.wait_for_url(f"{base}/installation")
    page.fill("#jeton", "mauvais-jeton")
    page.fill("#bibliotheque", "Bibliothèque d’essai")
    page.fill("#nom", "Camille Essai")
    page.fill("#identifiant", "camille")
    page.fill("#mot_de_passe", ADMIN_PASSWORD)
    page.fill("#confirmation", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    expect(page.get_by_text("Jeton incorrect.")).to_be_visible()
    page.fill("#jeton", token)
    page.fill("#mot_de_passe", ADMIN_PASSWORD)
    page.fill("#confirmation", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/")
    expect(page.get_by_text("Installation terminée")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Camille")
    expect(page.locator(".org")).to_contain_text("Bibliothèque d’essai")

    # L'installation est fermée.
    page.goto(f"{base}/installation")
    page.wait_for_url(f"{base}/")

    # Création d'un bénévole : le mot de passe provisoire n'est montré qu'une fois.
    page.goto(f"{base}/admin/utilisateurs/nouveau")
    page.fill("#identifiant", "dominique")
    page.fill("#nom", "Dominique Essai")
    page.get_by_label("Accueil / bénévole").check()
    page.click("form[action='/admin/utilisateurs/nouveau'] button[type=submit]")
    temporary = page.locator("main code").inner_text()
    assert len(temporary) >= 10

    # Déconnexion depuis le menu du compte.
    page.click("button.av")
    page.get_by_role("button", name="Se déconnecter").first.click()
    page.wait_for_url(f"{base}/connexion")

    # Le bénévole se connecte et doit changer son mot de passe.
    page.fill("#identifiant", "dominique")
    page.fill("#mot_de_passe", temporary)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/compte")
    expect(page.get_by_text("Choisissez un nouveau mot de passe")).to_be_visible()
    page.goto(f"{base}/")
    page.wait_for_url(f"{base}/compte")
    page.fill("#actuel", temporary)
    page.fill("#nouveau", NEW_PASSWORD)
    page.fill("#confirmation", NEW_PASSWORD)
    page.click("form[action='/compte/mot-de-passe'] button[type=submit]")
    expect(page.get_by_text("Votre mot de passe a été changé")).to_be_visible()

    # Refus côté serveur, par appel direct à l'adresse.
    response = page.goto(f"{base}/admin/utilisateurs")
    assert response.status == 403
    expect(page.locator("h1")).to_have_text("Accès refusé")
    expect(page.locator(".side")).not_to_contain_text("Utilisateurs")

    # Déconnexion : l'ancienne session ne vaut plus rien.
    page.click("button.av")
    page.get_by_role("button", name="Se déconnecter").first.click()
    page.wait_for_url(f"{base}/connexion")
    page.goto(f"{base}/compte")
    page.wait_for_url(re.compile(r"/connexion"))

    # Reconnexion avec le mot de passe choisi.
    page.fill("#identifiant", "dominique")
    page.fill("#mot_de_passe", NEW_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/compte")

    assert problems == []
    context.close()


def test_data_files_are_private_and_server_does_not_announce_itself(empty_server: Server) -> None:
    assert empty_server.biblyo("db", "upgrade").returncode == 0
    assert empty_server.biblyo("backup", "create").returncode == 0
    private = [empty_server.settings.database_path, *empty_server.settings.backup_dir.glob("biblyo-*.tar.gz")]
    assert len(private) == 2
    for path in private:
        assert path.stat().st_mode & 0o077 == 0, path
    with urllib.request.urlopen(f"{empty_server.base_url}/sante", timeout=5) as response:  # noqa: S310
        assert response.headers.get("server") is None


def test_login_page_gives_the_same_message_for_any_failure(browser: Browser, server: Server) -> None:
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{server.base_url}/connexion")
    page.fill("#identifiant", "personne")
    page.fill("#mot_de_passe", "mauvais mot de passe")
    page.click("button[type=submit]")
    expect(page.get_by_role("alert")).to_contain_text("Identifiant ou mot de passe incorrect.")
    context.close()


def _tab_until(page, predicate: str, limit: int = 60) -> None:
    """Avance à la touche Tab jusqu'à l'élément voulu, comme le ferait une personne au clavier."""
    for _ in range(limit):
        page.keyboard.press(NEXT_STOP)
        if page.evaluate(predicate):
            return
    raise AssertionError(f"Élément inaccessible au clavier : {predicate}")


def test_journey_with_the_keyboard_only(browser: Browser, server: Server) -> None:
    """Critère C10 : connexion, création d'un compte, refus d'accès et déconnexion, sans souris."""
    base = server.base_url
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    problems = watch(page)

    # Connexion : le champ a le focus, Tab passe au suivant, Entrée envoie.
    page.goto(f"{base}/connexion")
    expect(page.locator("#identifiant")).to_be_focused()
    page.keyboard.type("camille")
    page.keyboard.press("Tab")
    expect(page.locator("#mot_de_passe")).to_be_focused()
    page.keyboard.type(ADMIN_PASSWORD)
    page.keyboard.press("Enter")
    page.wait_for_url(f"{base}/")

    # Raccourci vers les utilisateurs, puis création d'un compte.
    page.keyboard.press("g")
    page.keyboard.press("u")
    page.wait_for_url(f"{base}/admin/utilisateurs")
    _tab_until(page, "document.activeElement.getAttribute('href') === '/admin/utilisateurs/nouveau'")
    page.keyboard.press("Enter")
    page.wait_for_url(f"{base}/admin/utilisateurs/nouveau")
    _tab_until(page, "document.activeElement.id === 'identifiant'")
    page.keyboard.type("elise")
    page.keyboard.press("Tab")
    page.keyboard.type("Élise Essai")
    _tab_until(page, "document.activeElement.value === 'viewer'")
    page.keyboard.press("Space")
    _tab_until(page, "document.activeElement.type === 'submit'")
    page.keyboard.press("Enter")
    expect(page.locator("h1")).to_have_text("Mot de passe provisoire")

    # Déconnexion par le menu du compte.
    page.locator("button.av").focus()
    page.keyboard.press("Enter")
    expect(page.locator(":focus")).to_have_text("Mon compte")
    page.keyboard.press("ArrowUp")
    expect(page.locator(":focus")).to_have_text("Se déconnecter")
    page.keyboard.press("Enter")
    page.wait_for_url(f"{base}/connexion")

    assert problems == []
    context.close()
