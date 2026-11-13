# SPDX-License-Identifier: EUPL-1.2
"""Administration des comptes, dernier administrateur, journal."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from biblyo.core import audit
from biblyo.core.db import Database

from ..factories import PASSWORD, make_user


def _query(settings, sql, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _audit(settings, action):
    return _query(settings, "SELECT user_id, entity_id, details FROM audit_log WHERE action = ?", action)


def test_create_user_shows_temporary_password_once(client_as, settings):
    admin = client_as("admin")
    response = admin.post(
        "/admin/utilisateurs/nouveau", data={"identifiant": "dominique", "nom": "Dominique", "roles": ["volunteer"]}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    temporary = re.search(r"<code[^>]*>([^<]+)</code>", response.text).group(1)
    stored = _query(settings, "SELECT password_hash, must_change_password FROM users WHERE username = 'dominique'")
    assert stored[0][0].startswith("$argon2id$")
    assert temporary not in stored[0][0]
    assert stored[0][1] == 1
    assert temporary not in str(_query(settings, "SELECT * FROM audit_log"))
    assert temporary not in admin.get("/admin/utilisateurs").text


def test_created_user_can_log_in_with_temporary_password(client_as, client, anonymous_form):
    admin = client_as("admin")
    page = admin.post(
        "/admin/utilisateurs/nouveau", data={"identifiant": "dominique", "nom": "Dominique", "roles": ["viewer"]}
    )
    temporary = re.search(r"<code[^>]*>([^<]+)</code>", page.text).group(1)
    token = anonymous_form(client, "/connexion")
    response = client.post("/connexion", data={"identifiant": "dominique", "mot_de_passe": temporary, "_csrf": token})
    assert response.status_code == 303
    assert client.get("/").headers["location"] == "/compte"


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"identifiant": "x", "nom": "N", "roles": ["viewer"]}, "identifiant compte"),
        ({"identifiant": "dominique", "nom": "", "roles": ["viewer"]}, "obligatoire"),
        ({"identifiant": "dominique", "nom": "N"}, "au moins un rôle"),
        ({"identifiant": "dominique", "nom": "N", "roles": ["pirate"]}, "Rôle inconnu"),
    ],
)
def test_create_user_refusals(client_as, data, fragment):
    response = client_as("admin").post("/admin/utilisateurs/nouveau", data=data)
    assert response.status_code == 400
    assert fragment in response.text


def test_usernames_are_unique_ignoring_case(client_as, settings):
    make_user(settings, "dominique")
    response = client_as("admin").post(
        "/admin/utilisateurs/nouveau", data={"identifiant": "DOMINIQUE", "nom": "N", "roles": ["viewer"]}
    )
    assert response.status_code == 400
    assert "déjà pris" in response.text


def test_list_and_edit_pages(client_as, settings):
    admin = client_as("admin", username="camille")
    other = make_user(settings, "dominique", "viewer", display_name="Dominique Essai")
    listing = admin.get("/admin/utilisateurs")
    assert "Dominique Essai" in listing.text
    assert "Consultation" in listing.text
    edit = admin.get(f"/admin/utilisateurs/{other}")
    assert edit.status_code == 200
    assert 'value="viewer" checked' in edit.text


def test_unknown_user_is_404(client_as):
    admin = client_as("admin")
    assert admin.get("/admin/utilisateurs/9999").status_code == 404
    assert admin.post("/admin/utilisateurs/9999", data={"nom": "N", "roles": ["viewer"]}).status_code == 404
    assert admin.post("/admin/utilisateurs/9999/mot-de-passe").status_code == 404


def test_role_change_applies_at_once_and_is_journaled(client_as, settings):
    admin = client_as("admin", username="camille")
    target = client_as("volunteer", username="dominique")
    assert target.get("/admin/utilisateurs").status_code == 403
    response = admin.post(
        f"/admin/utilisateurs/{target.user_id}", data={"nom": "Dominique", "roles": ["admin"], "actif": "1"}
    )
    assert response.status_code == 303
    assert target.get("/admin/utilisateurs").status_code == 200
    [(actor, entity, details)] = _audit(settings, "user.roles_changed")
    assert actor == admin.user_id
    assert entity == target.user_id
    assert json.loads(details) == {"avant": ["volunteer"], "apres": ["admin"]}


def test_disabling_closes_sessions_at_once(client_as, settings):
    admin = client_as("admin", username="camille")
    target = client_as("volunteer", username="dominique")
    response = admin.post(f"/admin/utilisateurs/{target.user_id}", data={"nom": "Dominique", "roles": ["volunteer"]})
    assert response.status_code == 303
    assert target.get("/").status_code == 303
    assert _query(settings, "SELECT COUNT(*) FROM sessions WHERE user_id = ?", target.user_id) == [(0,)]
    assert _audit(settings, "user.disabled")


def test_last_admin_cannot_lose_role(client_as, settings):
    admin = client_as("admin")
    response = admin.post(f"/admin/utilisateurs/{admin.user_id}", data={"nom": "N", "roles": ["viewer"], "actif": "1"})
    assert response.status_code == 400
    assert "au moins un administrateur" in response.text
    assert _query(
        settings, "SELECT code FROM roles JOIN user_roles ON roles.id = role_id WHERE user_id = ?", admin.user_id
    ) == [("admin",)]


def test_cannot_disable_self(client_as):
    admin = client_as("admin")
    response = admin.post(f"/admin/utilisateurs/{admin.user_id}", data={"nom": "N", "roles": ["admin"]})
    assert response.status_code == 400
    assert "votre propre compte" in response.text


def test_second_to_last_admin_can_leave_then_last_is_protected(client_as, settings):
    first = client_as("admin", username="camille")
    second = client_as("admin", username="dominique")
    assert first.post(f"/admin/utilisateurs/{second.user_id}", data={"nom": "D", "roles": ["admin"]}).status_code == 303
    response = first.post(f"/admin/utilisateurs/{first.user_id}", data={"nom": "C", "roles": ["viewer"], "actif": "1"})
    assert response.status_code == 400


def test_disabled_admin_does_not_count(client_as, settings):
    admin = client_as("admin", username="camille")
    make_user(settings, "dominique", "admin", active=False)
    response = admin.post(f"/admin/utilisateurs/{admin.user_id}", data={"nom": "C", "roles": ["viewer"], "actif": "1"})
    assert response.status_code == 400


def test_reset_password_closes_sessions_and_unlocks(client_as, settings):
    admin = client_as("admin", username="camille")
    target = client_as("volunteer", username="dominique")
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "UPDATE users SET failed_logins = 9, locked_until = '2999-01-01T00:00:00Z' WHERE id = ?", (target.user_id,)
    )
    connection.commit()
    connection.close()
    response = admin.post(f"/admin/utilisateurs/{target.user_id}/mot-de-passe")
    assert response.status_code == 200
    assert "<code" in response.text
    assert target.get("/").status_code == 303
    assert _query(
        settings, "SELECT failed_logins, locked_until, must_change_password FROM users WHERE id = ?", target.user_id
    ) == [(0, None, 1)]
    assert _audit(settings, "password.reset")


def test_login_events_are_journaled_without_sensitive_data(client, settings, anonymous_form):
    make_user(settings, "camille")
    token = anonymous_form(client, "/connexion")
    client.post("/connexion", data={"identifiant": "personne", "mot_de_passe": "Secret-1234", "_csrf": token})
    client.post("/connexion", data={"identifiant": "camille", "mot_de_passe": "Secret-1234", "_csrf": token})
    client.post("/connexion", data={"identifiant": "camille", "mot_de_passe": PASSWORD, "_csrf": token})
    assert len(_audit(settings, "login.failed")) == 2
    assert len(_audit(settings, "login.ok")) == 1
    journal = str(_query(settings, "SELECT * FROM audit_log"))
    assert "Secret-1234" not in journal
    assert "personne" not in journal  # un identifiant mal tapé peut être un mot de passe
    assert PASSWORD not in journal


def test_journal_refuses_sensitive_keys(settings):
    database = Database(settings.database_path)
    with pytest.raises(ValueError, match="sensible"), database.session(write=True) as session:
        audit.record(session, "essai", "user", details={"password": "x"})
    database.dispose()


def test_cli_user_create_and_reset(settings, run_cli, client, anonymous_form):
    created = run_cli(settings, "biblyo user create dominique --role viewer", input=f"{PASSWORD}\n{PASSWORD}\n")
    assert created.exit_code == 0, created.output
    assert PASSWORD not in created.output

    token = anonymous_form(client, "/connexion")
    assert (
        client.post(
            "/connexion", data={"identifiant": "dominique", "mot_de_passe": PASSWORD, "_csrf": token}
        ).status_code
        == 303
    )

    reset = run_cli(settings, "biblyo user reset-password dominique")
    assert reset.exit_code == 0
    assert "provisoire" in reset.output
    assert client.get("/").status_code == 303


def test_cli_user_refusals(settings, run_cli):
    weak = run_cli(settings, "biblyo user create dominique", input="court\ncourt\n")
    assert weak.exit_code == 1
    assert "au moins 10" in weak.stderr
    unknown_role = run_cli(settings, "biblyo user create dominique --role pirate", input=f"{PASSWORD}\n{PASSWORD}\n")
    assert unknown_role.exit_code == 1
    assert "Rôle inconnu" in unknown_role.stderr
    assert run_cli(settings, "biblyo user reset-password personne").exit_code == 1


def test_cli_user_refused_without_database(bare_settings, run_cli):
    result = run_cli(bare_settings, "biblyo user reset-password camille")
    assert result.exit_code == 1
    assert not bare_settings.database_path.exists()
