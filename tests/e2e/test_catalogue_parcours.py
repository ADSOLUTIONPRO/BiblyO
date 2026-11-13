# SPDX-License-Identifier: EUPL-1.2
"""Parcours métier dans un vrai navigateur : fiche, édition, exemplaire, couverture, séries, droits, clavier."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Browser, Page, expect

from ..factories import make_notice
from .conftest import VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e


def _cover(path: Path) -> Path:
    Image.new("RGB", (400, 600), (46, 90, 136)).save(path, "PNG")
    return path


def _notice_id(page: Page) -> str:
    return re.search(r"/catalogue/notices/(\d+)", page.url).group(1)


def test_describe_a_book_from_creation_to_retrieval(admin_page: Page, server: Server, tmp_path: Path) -> None:
    problems = watch(admin_page)
    base = server.base_url
    page = admin_page
    page.goto(f"{base}/catalogue/notices/nouvelle")
    page.fill("#titre", "L’Été des œuvres perdues")
    page.select_option("#type", label="Bande dessinée")
    page.locator("input[name=contributeur_nom]").first.fill("Léa Fictive")
    page.fill("#serie", "Chroniques d’essai")
    page.fill("#tome", "3")
    page.fill("#isbn", "979-10-90636-07-1")
    page.fill("#editeur", "Éditions Fictives")
    page.fill("#annee", "2021")
    page.fill("#categories", "Parcours")
    page.wait_for_load_state("networkidle")  # WebKit signale comme erreur une suggestion interrompue par l'envoi
    page.get_by_role("button", name="Créer la notice").click()
    page.wait_for_url(re.compile(r"/catalogue/notices/\d+$"))
    expect(page.locator("h1")).to_have_text("L’Été des œuvres perdues")
    notice = _notice_id(page)

    # Couverture importée depuis la page Couverture, vue en aperçu, enregistrée, puis affichée.
    page.goto(f"{base}/catalogue/notices/{notice}/couverture")
    upload = page.locator("form[action$='/couverture/fichier']")
    upload.locator("input[type=file]").set_input_files(_cover(tmp_path / "couverture.png"))
    upload.get_by_label("Photo prise pour le Club").check()
    upload.get_by_role("button", name="Voir l’aperçu", exact=True).click()
    page.get_by_role("button", name="Enregistrer la couverture").click()
    page.wait_for_url(re.compile(r"/catalogue/notices/\d+\?edition="))
    expect(page.locator("img.cover-img[src^='/medias/']").first).to_be_visible()
    page.goto(f"{base}/catalogue/notices/{notice}?onglet=editions")

    # Seconde édition : la même œuvre, un autre ISBN.
    page.get_by_role("link", name="Ajouter une édition").click()
    page.fill("#isbn", "0-8044-2957-X")
    page.fill("#editeur", "Autre Maison")
    page.fill("#annee", "2024")
    page.wait_for_load_state("networkidle")  # WebKit signale comme erreur une suggestion interrompue par l'envoi
    page.get_by_role("button", name="Ajouter l’édition").click()
    page.wait_for_url(re.compile(r"onglet=editions"))
    expect(page.get_by_text("Autre Maison").first).to_be_visible()

    # Deux exemplaires de la même édition, numérotés par le serveur.
    page.goto(f"{base}/catalogue/notices/{notice}?onglet=exemplaires")
    page.fill("#quantite", "2")
    page.locator("#ajout-exemplaire button[type=submit]").click()
    page.wait_for_url(re.compile(r"onglet=exemplaires"))
    expect(page.locator("main")).to_contain_text(re.compile(r"EX-\d{6}"))
    assert len(set(re.findall(r"EX-\d{6}", page.locator("main").inner_text()))) == 2

    # Retrouvée sans accents ni ligature, et par l'ISBN de sa seconde édition, sous sa forme à 13 chiffres.
    for query in ("ete des oeuvres", "9780804429573", "léa fictive"):
        page.goto(f"{base}/catalogue?q={query}")
        expect(page.locator(".bcard")).to_have_count(1)
    page.goto(f"{base}/catalogue?cat=parcours&editeur=autre+maison")
    expect(page.locator(".bcard")).to_have_count(1)

    # Modification, puis archivage refusé tant que des exemplaires sont au fonds.
    page.goto(f"{base}/catalogue/notices/{notice}?onglet=notice&modifier=identification")
    page.fill("#titre", "L’Été des œuvres retrouvées")
    page.locator("#sec-identification button[type=submit]").first.click()
    page.wait_for_url(re.compile(r"onglet=notice"))
    expect(page.locator("h1")).to_have_text("L’Été des œuvres retrouvées")
    # Tant qu'il reste des exemplaires, la fenêtre explique pourquoi et ne propose ni archivage ni suppression.
    page.locator("[data-open-dialog=retirer-notice]").click()
    dialog = page.locator("#retirer-notice")
    expect(dialog).to_contain_text("ne peut pas être supprimée")
    expect(dialog.get_by_role("button", name="Archiver")).to_have_count(0)
    assert problems == []


def test_a_fake_image_is_refused(admin_page: Page, server: Server, tmp_path: Path) -> None:
    work = make_notice(server.settings, "Couverture piégée")
    fake = tmp_path / "piege.png"
    fake.write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
    admin_page.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
    upload = admin_page.locator("form[action$='/couverture/fichier']")
    upload.locator("input[type=file]").set_input_files(fake)
    upload.get_by_label("Photo prise pour le Club").check()
    with admin_page.expect_response(lambda response: "/couverture/fichier" in response.url) as answer:
        upload.get_by_role("button", name="Voir l’aperçu").click()
    assert answer.value.status == 400
    expect(admin_page.get_by_text("n’est pas une image JPEG, PNG ou WebP valide")).to_be_visible()
    expect(admin_page.locator("main img.cover-img")).to_have_count(0)


def test_archiving_a_work_without_copies_hides_it(admin_page: Page, server: Server) -> None:
    work = make_notice(server.settings, "Notice à ranger aux archives")
    base = server.base_url
    admin_page.goto(f"{base}/catalogue/notices/{work}")
    admin_page.locator("[data-open-dialog=retirer-notice]").click()
    admin_page.get_by_role("button", name="Archiver").click()
    admin_page.wait_for_load_state("networkidle")
    admin_page.goto(f"{base}/catalogue?q=ranger+aux+archives")
    expect(admin_page.locator(".bcard")).to_have_count(0)
    admin_page.goto(f"{base}/catalogue?q=ranger+aux+archives&statut=archivees")
    expect(admin_page.locator(".bcard")).to_have_count(1)


def test_series_three_states_and_confirmation(admin_page: Page, server: Server) -> None:
    settings = server.settings
    make_notice(settings, "Rivage 1", series="Saga du rivage", volume="1")
    make_notice(settings, "Rivage 4", series="Saga du rivage", volume="4")
    base = server.base_url
    admin_page.goto(f"{base}/catalogue/series?q=saga+du+rivage")
    expect(admin_page.get_by_text("100 % des tomes confirmés · 2 numéros à vérifier, non comptés")).to_be_visible()
    expect(admin_page.locator(".tome.gap")).to_have_count(2)
    admin_page.get_by_role("link", name="Ouvrir").click()
    admin_page.locator("summary", has_text="Confirmer des tomes parus").click()
    admin_page.fill("#premier", "2")
    admin_page.fill("#note", "Liste de l’éditeur")
    admin_page.get_by_role("button", name="Confirmer", exact=True).first.click()
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".tl .tome.miss")).to_have_count(1)
    expect(admin_page.locator(".tl .tome.gap")).to_have_count(1)
    expect(admin_page.get_by_text("67 % des tomes confirmés · 1 numéro à vérifier, non compté")).to_be_visible()


def test_a_volunteer_cannot_reach_editing_pages(browser: Browser, server: Server) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER])
    page = context.new_page()
    work = make_notice(server.settings, "Lisible par tous")
    base = server.base_url
    new_edition = f"/catalogue/notices/{work}/editions/nouvelle"
    paths = ("/catalogue/notices/nouvelle", "/admin/catalogue/categories", new_edition)
    for path in paths:
        response = page.goto(f"{base}{path}")
        assert response.status == 403, path
        expect(page.get_by_text("Accès refusé").first).to_be_visible()
    page.goto(f"{base}/catalogue")
    expect(page.locator(".ph a[href='/catalogue/notices/nouvelle']")).to_have_count(0)
    page.goto(f"{base}/catalogue/notices/{work}")
    expect(page.get_by_role("link", name="Modifier")).to_have_count(0)
    context.close()


def test_a_notice_can_be_created_from_the_keyboard(admin_page: Page, server: Server) -> None:
    base = server.base_url
    page = admin_page
    page.goto(f"{base}/catalogue")
    page.keyboard.press("n")
    page.wait_for_url(f"{base}/catalogue/notices/nouvelle")
    page.locator("#titre").focus()
    page.keyboard.type("Saisie au clavier")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "type"
    page.select_option("#type", label="Livre")
    page.locator("#titre").focus()
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/catalogue/notices/\d+$"))
    expect(page.locator("h1")).to_have_text("Saisie au clavier")
