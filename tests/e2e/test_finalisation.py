# SPDX-License-Identifier: EUPL-1.2
"""Écrans de la finalisation de L6 dans un vrai navigateur : paramètres, « À ranger », mise en rayon par lot,
présence vérifiée, codes-barres en lot. Accessibilité (axe), aucun défilement horizontal, grand écran et téléphone.

Instance à part, classeur fabriqué importé avant le démarrage : aucune donnée du Club."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, expect

from biblyo.core.db import Database
from biblyo.core.migrate import upgrade
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput
from biblyo.modules.imports import apply as importer
from biblyo.modules.imports import profile as profiles
from biblyo.modules.imports import reader, simulate

from .. import import_samples as samples
from ..factories import make_user
from .conftest import ADMIN, Server, _settings, login, start_server, stop_server, watch
from .test_import import SIZES, _accessible, _capture, _no_horizontal_scroll

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def ready_server(tmp_path_factory: pytest.TempPathFactory, browser: Browser) -> Iterator[Server]:
    root = tmp_path_factory.mktemp("finalisation")
    settings = _settings(root)
    upgrade(settings)
    make_user(settings, ADMIN, "admin", display_name="Camille Essai")
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, 'true', 'x')", (importer.ENABLED_SETTING,)
    )
    connection.commit()
    connection.close()
    database = Database(settings.database_path)
    main = reader.read(samples.workbook(), "catalogue.xlsx")
    profile = profiles.detect(main.sheets[0])
    with database.session() as session:
        sim = simulate.simulate(main, "Feuil1", profile, existing=simulate.snapshot(session))
        session.rollback()
    importer.apply(database, settings, importer.ApplyRequest(main, "Feuil1", profile, sim.plan_sha256, None))
    room = holdings.create_location(database, "room", None, LocationInput("Salle BD"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble 1", code="M1"), None)
    holdings.create_location(database, "shelf", furniture, LocationInput("Étagère A", capacity="2"), None)
    database.dispose()
    instance = start_server(settings, root)
    context = browser.new_context()
    login(context.new_page(), instance.base_url, ADMIN)
    instance.storage[ADMIN] = root / "admin.json"
    context.storage_state(path=instance.storage[ADMIN])
    context.close()
    yield instance
    stop_server(instance)


@pytest.fixture(params=tuple(SIZES))
def page(request: pytest.FixtureRequest, browser: Browser, ready_server: Server) -> Iterator[Page]:
    context = browser.new_context(storage_state=ready_server.storage[ADMIN], viewport=SIZES[request.param])
    instance = context.new_page()
    instance.size_name = request.param  # type: ignore[attr-defined]
    yield instance
    context.close()


def _check(page: Page, name: str) -> None:
    _accessible(page)
    _no_horizontal_scroll(page)
    _capture(page, name)


def test_settings_and_bulk_shelving_screens(page: Page, ready_server: Server) -> None:
    problems = watch(page)
    base = ready_server.base_url
    page.goto(f"{base}/parametres/avance")
    expect(page.get_by_role("heading", name="Contrôle des réglages")).to_be_visible()
    _check(page, "10-parametres")
    page.goto(f"{base}/emplacements/non-ranges?origine=importes")
    expect(page.get_by_role("heading", name="23 exemplaire(s)")).to_be_visible()
    _check(page, "11-a-ranger")
    page.get_by_role("checkbox", name=re.compile(r"La mer d'huile.*inventaire 1\b")).check()
    page.get_by_role("checkbox", name=re.compile(r"Le vent tourne.*inventaire 2\b")).check()
    page.get_by_role("button", name="Ranger la sélection…").click()
    page.locator("#lieu").select_option(index=1)
    page.get_by_role("button", name="Voir ce qui va se passer").click()
    expect(page.get_by_text("statut après : En préparation").first).to_be_visible()
    _check(page, "12-ranger-par-lot")
    page.goto(f"{base}/emplacements/verifier?exemplaire=1&exemplaire=2")
    expect(page.get_by_role("heading", name="Confirmer la présence")).to_be_visible()
    _check(page, "13-presence")
    page.goto(f"{base}/emplacements/codes-barres?exemplaire=1&exemplaire=2")
    expect(page.get_by_text("EX-000001").first).to_be_visible()
    _check(page, "14-codes-barres")
    assert problems == []
