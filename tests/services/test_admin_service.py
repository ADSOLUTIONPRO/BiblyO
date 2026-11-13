# SPDX-License-Identifier: EUPL-1.2
"""Règles du module d'administration, sur une base réelle obtenue par les migrations."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.security import install as install_tokens
from biblyo.modules.admin import service

from ..factories import PASSWORD, make_user


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _count(settings, query: str) -> int:
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query).fetchone()[0]
    finally:
        connection.close()


def _form(token: str, username: str = "camille") -> service.InstallForm:
    return service.InstallForm(token, "Bibliothèque d’essai", "Camille", username, PASSWORD, PASSWORD)


def test_simultaneous_installations_create_a_single_account(database, settings):
    """Deux navigateurs, un seul jeton : `BEGIN IMMEDIATE` ne laisse passer qu'une installation."""
    token = install_tokens.regenerate_token(database)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            service.install(database, _form(token, f"compte{index}"), settings, None, None)
            results.append("créé")
        except service.InstallationClosed:
            results.append("fermé")
        except FormErrors:
            results.append("jeton consommé")

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("créé") == 1
    assert _count(settings, "SELECT COUNT(*) FROM users") == 1


def test_simultaneous_demotions_keep_one_administrator(database, settings):
    first = make_user(settings, "camille", "admin")
    second = make_user(settings, "dominique", "admin")
    barrier = threading.Barrier(2)
    refused: list[str] = []

    def demote(actor: int, target: int) -> None:
        barrier.wait()
        try:
            service.update_user(database, actor, target, "Nom", ["viewer"], True)
        except BusinessError as exc:
            refused.append(exc.message)

    threads = [threading.Thread(target=demote, args=pair) for pair in ((first, second), (second, first))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refused) == 1
    admins = _count(
        settings,
        "SELECT COUNT(*) FROM user_roles JOIN roles ON roles.id = role_id WHERE code = 'admin'",
    )
    assert admins == 1


def test_refused_update_changes_nothing(database, settings):
    only_admin = make_user(settings, "camille", "admin", display_name="Avant")
    with pytest.raises(BusinessError):
        service.update_user(database, None, only_admin, "Après", ["viewer"], True)
    connection = sqlite3.connect(settings.database_path)
    name = connection.execute("SELECT display_name FROM users WHERE id = ?", (only_admin,)).fetchone()[0]
    connection.close()
    assert name == "Avant"


def test_unknown_accounts_are_reported(database, settings):
    with pytest.raises(BusinessError):
        service.update_profile(database, 9999, "Nom")
    with pytest.raises(BusinessError):
        service.update_user(database, None, 9999, "Nom", ["viewer"], True)
    with pytest.raises(BusinessError):
        service.reset_password(database, None, 9999)
    with pytest.raises(BusinessError):
        service.change_password(database, 9999, "a", "b", "b", settings, None, None)


def test_name_length_is_bounded(database, settings):
    account = make_user(settings, "camille")
    with pytest.raises(FormErrors, match="trop long"):
        service.update_profile(database, account, "x" * 121)


def test_duplicate_command_line_account_is_refused(database, settings):
    make_user(settings, "camille")
    with pytest.raises(FormErrors, match="déjà pris"):
        service.create_account_with_password(database, "CAMILLE", "Camille", "viewer", PASSWORD)


def test_command_line_account_closes_the_installation(database, settings):
    install_tokens.regenerate_token(database)
    service.create_account_with_password(database, "camille", "Camille", "admin", PASSWORD)
    assert _count(settings, "SELECT COUNT(*) FROM settings WHERE key = 'install.token_hash'") == 0
    assert install_tokens.regenerate_token(database) is None


def test_old_hash_parameters_are_upgraded_at_login(database, settings, monkeypatch):
    account = make_user(settings, "camille")
    monkeypatch.setattr(service.passwords, "needs_rehash", lambda _hash: True)
    before = sqlite3.connect(settings.database_path).execute("SELECT password_hash FROM users").fetchone()[0]
    assert service.authenticate(database, "camille", PASSWORD, None) == account
    after = sqlite3.connect(settings.database_path).execute("SELECT password_hash FROM users").fetchone()[0]
    assert before != after
    assert service.authenticate(database, "camille", PASSWORD, None) == account
