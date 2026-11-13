# SPDX-License-Identifier: EUPL-1.2
"""Bout en bout : le vrai serveur sur une base jetable, dans un vrai navigateur."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from biblyo.config import Settings
from biblyo.core.migrate import upgrade

from ..factories import PASSWORD, make_user

ROOT = Path(__file__).resolve().parents[2]
#: Formulation de l'annulation d'une requête par Chromium, WebKit et Firefox.
CANCELLED = {"net::ERR_ABORTED", "Load request cancelled", "cancelled", "NS_BINDING_ABORTED"}
#: Moteur des tests : `make e2e-navigateurs` le fait varier, `make e2e` reste sur Chromium.
ENGINE = os.environ.get("BIBLYO_E2E_NAVIGATEUR", "chromium")
#: Sous macOS, WebKit saute les liens à la touche Tab : Option+Tab parcourt tous les éléments, comme Safari.
NEXT_STOP = "Alt+Tab" if ENGINE == "webkit" else "Tab"
ADMIN = "camille"
VOLUNTEER = "dominique"
PROGRAM = "from biblyo.cli import main; main()"


@dataclass
class Server:
    settings: Settings
    base_url: str
    process: subprocess.Popen
    storage: dict[str, Path] = field(default_factory=dict)

    @property
    def environment(self) -> dict[str, str]:
        return _environment(self.settings, self.base_url)

    def biblyo(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", PROGRAM, *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )


def _environment(settings: Settings, base_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "BIBLYO_ENV": settings.env,
        "BIBLYO_DATA_DIR": str(settings.data_dir),
        "BIBLYO_BACKUP_DIR": str(settings.backup_dir),
        "BIBLYO_BASE_URL": base_url,
        # Les sources bibliographiques sont remplacées par des réponses enregistrées : aucun appel réseau réel
        "BIBLYO_ENRICHMENT_FIXTURES": str(ROOT / "tests" / "fixtures" / "sources" / "enregistrees"),
    }


def _settings(root: Path) -> Settings:
    (root / "data").mkdir()
    (root / "backups").mkdir()
    return Settings(env="development", data_dir=root / "data", backup_dir=root / "backups")


def start_server(settings: Settings, root: Path) -> Server:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = root / "serveur.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", PROGRAM, "serve", "--port", str(port)],
            env=_environment(settings, base_url),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/sante", timeout=1)  # noqa: S310
            return Server(settings, base_url, process)
        except urllib.error.HTTPError:
            return Server(settings, base_url, process)  # 503 en maintenance : le serveur répond
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise AssertionError(f"Le serveur ne répond pas :\n{log_path.read_text()}")


def stop_server(server: Server) -> None:
    server.process.terminate()
    server.process.wait(timeout=10)


@pytest.fixture(scope="session", autouse=True)
def _stylesheet_is_built() -> None:
    assert (ROOT / "src/biblyo/static/dist/app.css").is_file(), "Feuille de style absente : lancez `make css`."


@pytest.fixture
def empty_server(tmp_path: Path) -> Iterator[Server]:
    """Installation neuve, sans base de données."""
    server = start_server(_settings(tmp_path), tmp_path)
    yield server
    stop_server(server)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        instance = getattr(playwright, ENGINE).launch()
        yield instance
        instance.close()


def login(page: Page, base_url: str, username: str, password: str = PASSWORD) -> None:
    page.goto(f"{base_url}/connexion")
    page.fill("#identifiant", username)
    page.fill("#mot_de_passe", password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/")


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory, browser: Browser) -> Iterator[Server]:
    """Installation prête, avec un administrateur et un bénévole dont les sessions sont déjà ouvertes."""
    root = tmp_path_factory.mktemp("e2e")
    settings = _settings(root)
    upgrade(settings)
    make_user(settings, ADMIN, "admin", display_name="Camille Essai")
    make_user(settings, VOLUNTEER, "volunteer", display_name="Dominique Essai")
    instance = start_server(settings, root)
    for username in (ADMIN, VOLUNTEER):
        context = browser.new_context()
        login(context.new_page(), instance.base_url, username)
        state = root / f"{username}.json"
        context.storage_state(path=state)
        instance.storage[username] = state
        context.close()
    yield instance
    stop_server(instance)


def watch(page: Page) -> list[str]:
    """Erreurs vues par le navigateur : violations de CSP, erreurs JavaScript, ressources en échec."""
    problems: list[str] = []

    def on_console(message) -> None:
        if message.type == "error" and "the server responded with a status of" not in message.text:
            problems.append(f"console : {message.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda error: problems.append(f"erreur JavaScript : {error}"))

    def on_failed(request) -> None:
        if request.failure not in CANCELLED:  # image différée interrompue par la navigation suivante : rien d'anormal
            problems.append(f"requête en échec : {request.url} ({request.failure})")

    page.on("requestfailed", on_failed)
    return problems


@pytest.fixture
def admin_context(browser: Browser, server: Server) -> Iterator[BrowserContext]:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": 1440, "height": 900})
    yield context
    context.close()


@pytest.fixture
def admin_page(admin_context: BrowserContext) -> Page:
    return admin_context.new_page()
