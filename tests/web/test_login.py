# SPDX-License-Identifier: EUPL-1.2
"""Connexion, verrouillage, sessions."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from biblyo.app import LOGIN_FAILURES_PER_ADDRESS, LOGIN_FAILURES_PER_IDENTIFIER
from biblyo.core.security.ratelimit import FailureLimiter
from biblyo.core.security.sessions import COOKIE_NAME
from biblyo.modules.admin.routes import LOGIN_KEY_MAX_LENGTH, _login_key
from biblyo.modules.admin.service import lock_duration

from ..factories import PASSWORD, make_user, open_session_for


def _login(client, token, username="camille", password=PASSWORD, **extra):
    return client.post("/connexion", data={"identifiant": username, "mot_de_passe": password, "_csrf": token, **extra})


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


@pytest.fixture
def account(settings):
    return make_user(settings, "camille", "volunteer")


def test_good_credentials_open_a_session(client, account, anonymous_form):
    response = _login(client, anonymous_form(client, "/connexion"))
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME))
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie  # HTTP en développement
    assert client.get("/").status_code == 200


def test_cookie_is_secure_behind_https(settings, account, anonymous_form):
    from biblyo.app import create_app

    settings.base_url = "https://bibliotheque.example"
    with TestClient(create_app(settings), base_url="https://testserver", follow_redirects=False) as secure:
        response = _login(secure, anonymous_form(secure, "/connexion"))
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME))
    assert "Secure" in cookie


def test_same_message_for_unknown_user_and_wrong_password(client, account, anonymous_form):
    token = anonymous_form(client, "/connexion")
    unknown = _login(client, token, username="personne")
    wrong = _login(client, token, password="mauvais mot de passe")
    assert unknown.status_code == wrong.status_code == 400

    assert "Identifiant ou mot de passe incorrect." in unknown.text
    assert "Identifiant ou mot de passe incorrect." in wrong.text
    assert "inconnu" not in unknown.text


def test_disabled_account_is_refused_with_the_same_message(client, settings, anonymous_form):
    make_user(settings, "camille", "volunteer", active=False)
    response = _login(client, anonymous_form(client, "/connexion"))
    assert response.status_code == 400
    assert "Identifiant ou mot de passe incorrect." in response.text
    assert COOKIE_NAME not in response.cookies


def test_lock_durations_grow_and_are_bounded():
    assert lock_duration(4) is None
    assert lock_duration(5).total_seconds() == 60
    assert lock_duration(6).total_seconds() == 120
    assert lock_duration(30).total_seconds() == 3600


def test_account_locks_after_failures_and_releases_with_time(client, account, settings, frozen_clock, anonymous_form):
    token = anonymous_form(client, "/connexion")
    for _ in range(5):
        assert _login(client, token, password="mauvais mot de passe").status_code == 400
    assert _rows(settings, "SELECT failed_logins FROM users WHERE id = ?", account) == [(5,)]

    refused = _login(client, token)  # le bon mot de passe ne passe plus
    assert refused.status_code == 400
    assert "Identifiant ou mot de passe incorrect." in refused.text

    frozen_clock.advance(minutes=2)
    assert _login(client, token).status_code == 303
    assert _rows(settings, "SELECT failed_logins, locked_until FROM users WHERE id = ?", account) == [(0, None)]


def test_failures_on_one_identifier_do_not_block_the_others(client, account, anonymous_form):
    token = anonymous_form(client, "/connexion")
    for _ in range(LOGIN_FAILURES_PER_IDENTIFIER):
        assert _login(client, token, username="inconnu").status_code == 400
    blocked = _login(client, token, username=" INCONNU ")  # même identifiant, autre écriture
    assert blocked.status_code == 429
    assert "Trop de tentatives" in blocked.text
    assert _login(client, token).status_code == 303  # les autres comptes de cette adresse se connectent


def test_address_is_capped_across_identifiers(app, client, account, anonymous_form):
    app.state.login_address_limiter = FailureLimiter(3, 60)
    token = anonymous_form(client, "/connexion")
    for index in range(3):
        assert _login(client, token, username=f"inconnu{index}").status_code == 400
    assert _login(client, token).status_code == 429  # essais en masse : le bon mot de passe ne passe plus


def test_successful_login_does_not_clear_the_address_cap(app, client, account, anonymous_form):
    app.state.login_address_limiter = FailureLimiter(2, 60)
    token = anonymous_form(client, "/connexion")
    assert _login(client, token, username="inconnu").status_code == 400
    assert _login(client, token).status_code == 303
    client.cookies.delete(COOKIE_NAME)
    assert _login(client, token, username="autre").status_code == 400
    assert _login(client, token).status_code == 429


def test_limiter_key_is_bounded_and_normalized():
    assert _login_key("192.0.2.1", " Camille ") == "192.0.2.1|camille"
    assert len(_login_key("192.0.2.1", "x" * 1_000_000)) <= len("192.0.2.1|") + LOGIN_KEY_MAX_LENGTH


def test_default_login_limits():
    assert (LOGIN_FAILURES_PER_IDENTIFIER, LOGIN_FAILURES_PER_ADDRESS) == (20, 100)


def test_account_page_names_the_browser_instead_of_its_signature(client, account, anonymous_form):
    signature = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36"
    client.headers["user-agent"] = signature
    _login(client, anonymous_form(client, "/connexion"))
    page = client.get("/compte").text
    assert "Chrome sur macOS" in page
    assert "Mozilla/5.0" not in page


def test_token_is_never_stored_in_clear(client, account, settings, anonymous_form):
    _login(client, anonymous_form(client, "/connexion"))
    raw = client.cookies[COOKIE_NAME]
    stored = _rows(settings, "SELECT token_hash FROM sessions")
    assert stored == [(hashlib.sha256(raw.encode()).hexdigest(),)]
    with sqlite3.connect(settings.database_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert raw not in dump


def test_session_expires(app, client_as, frozen_clock):
    browser = client_as("volunteer")
    assert browser.get("/").status_code == 200
    frozen_clock.advance(days=15)
    response = browser.get("/")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/connexion")


def test_session_expiry_slides_with_use(app, client_as, frozen_clock, settings):
    browser = client_as("volunteer")
    before = _rows(settings, "SELECT expires_at FROM sessions")
    frozen_clock.advance(days=10)
    response = browser.get("/compte")
    assert response.status_code == 200
    after = _rows(settings, "SELECT expires_at FROM sessions")
    assert after > before
    assert COOKIE_NAME in "".join(response.headers.get_list("set-cookie"))
    frozen_clock.advance(days=10)  # vingt jours depuis la connexion, dix depuis le dernier passage
    assert browser.get("/").status_code == 200


def test_activity_within_interval_does_not_write(app, client_as, frozen_clock, settings):
    browser = client_as("volunteer")
    before = _rows(settings, "SELECT last_seen_at FROM sessions")
    frozen_clock.advance(minutes=1)
    assert browser.get("/").status_code == 200
    assert _rows(settings, "SELECT last_seen_at FROM sessions") == before


def test_logout_revokes_on_the_server(client_as, settings):
    browser = client_as("volunteer")
    stolen = browser.cookies[COOKIE_NAME]
    response = browser.post("/deconnexion")
    assert response.status_code == 303
    assert response.headers["location"] == "/connexion"
    assert _rows(settings, "SELECT COUNT(*) FROM sessions") == [(0,)]
    browser.cookies.set(COOKIE_NAME, stolen)
    assert browser.get("/").status_code == 303


def test_password_change_revokes_all_sessions_and_keeps_this_browser(app, client_as, settings):
    browser = client_as("volunteer", username="camille")
    other_raw, _ = open_session_for(settings, browser.user_id)
    previous = browser.cookies[COOKIE_NAME]
    response = browser.post(
        "/compte/mot-de-passe",
        data={"actuel": PASSWORD, "nouveau": "Nouvelle-Phrase-77", "confirmation": "Nouvelle-Phrase-77"},
    )
    assert response.status_code == 303, response.text
    assert _rows(settings, "SELECT COUNT(*) FROM sessions") == [(1,)]
    fresh = re.search(rf"{COOKIE_NAME}=([^;]+)", "".join(response.headers.get_list("set-cookie"))).group(1)
    assert fresh != previous
    browser.cookies.clear()
    browser.cookies.set(COOKIE_NAME, previous)
    assert browser.get("/").status_code == 303  # l'ancien jeton est mort
    browser.cookies.clear()
    browser.cookies.set(COOKIE_NAME, fresh)

    with TestClient(app, follow_redirects=False) as other:
        other.cookies.set(COOKIE_NAME, other_raw)
        assert other.get("/").status_code == 303
    assert browser.get("/").status_code == 200


@pytest.mark.parametrize(
    ("form", "fragment"),
    [
        (
            {"actuel": "faux", "nouveau": "Nouvelle-Phrase-77", "confirmation": "Nouvelle-Phrase-77"},
            "actuel est incorrect",
        ),
        ({"actuel": PASSWORD, "nouveau": "court", "confirmation": "court"}, "au moins 10"),
        ({"actuel": PASSWORD, "nouveau": "Nouvelle-Phrase-77", "confirmation": "autre"}, "diffèrent"),
        ({"actuel": PASSWORD, "nouveau": PASSWORD, "confirmation": PASSWORD}, "différer"),
    ],
)
def test_password_change_refusals(client_as, form, fragment, settings):
    browser = client_as("volunteer")
    response = browser.post("/compte/mot-de-passe", data=form)
    assert response.status_code == 400
    assert fragment in response.text


def test_forced_password_change_blocks_other_pages(client_as):
    browser = client_as("volunteer", must_change=True)
    redirected = browser.get("/")
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/compte"
    assert browser.get("/compte").status_code == 200
    browser.post(
        "/compte/mot-de-passe",
        data={"actuel": PASSWORD, "nouveau": "Nouvelle-Phrase-77", "confirmation": "Nouvelle-Phrase-77"},
    )
    assert browser.get("/").status_code == 200


def test_anonymous_is_sent_to_login(client):
    response = client.get("/compte")
    assert response.status_code == 303
    assert response.headers["location"] == "/connexion?suivant=/compte"


def test_anonymous_htmx_gets_a_client_redirect(client):
    response = client.get("/compte", headers={"HX-Request": "true"})
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/connexion"


def test_login_page_redirects_when_already_connected(client_as):
    response = client_as("volunteer").get("/connexion")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_login_page_redirects_to_installation_when_empty(client):
    response = client.get("/connexion")
    assert response.status_code == 303
    assert response.headers["location"] == "/installation"


@pytest.mark.parametrize(
    "target",
    [
        "https://exemple.invalid/piege",
        "//exemple.invalid/piege",
        "/\\exemple.invalid",
        "javascript:alert(1)",
        "/\t/exemple.invalid",
        "/\n/exemple.invalid",
    ],
)
def test_external_redirect_after_login_is_refused(client, account, anonymous_form, target):
    response = _login(client, anonymous_form(client, "/connexion"), suivant=target)
    assert response.headers["location"] == "/"


def test_internal_redirect_after_login_is_kept(client, account, anonymous_form):
    response = _login(client, anonymous_form(client, "/connexion"), suivant="/compte")
    assert response.headers["location"] == "/compte"


def test_no_password_in_logs_or_journal(client, account, settings, anonymous_form, caplog):
    caplog.set_level(logging.DEBUG)
    token = anonymous_form(client, "/connexion")
    _login(client, token, password="Secret-Echoue-1234")
    _login(client, token)
    assert "Secret-Echoue-1234" not in caplog.text
    assert PASSWORD not in caplog.text
    journal = str(_rows(settings, "SELECT * FROM audit_log"))
    assert "Secret-Echoue-1234" not in journal
    assert PASSWORD not in journal


def test_closing_own_other_session_only(client_as, settings):
    first = client_as("volunteer", username="camille")
    second = client_as("volunteer", username="dominique")
    foreign = _rows(settings, "SELECT id FROM sessions WHERE user_id = ?", second.user_id)[0][0]
    response = first.post(f"/compte/sessions/{foreign}/fermer")
    assert response.status_code == 303
    assert second.get("/").status_code == 200  # la session d'un autre compte n'est pas touchée


def test_closing_another_session_of_mine(app, client_as, settings):
    browser = client_as("volunteer")
    other_raw, _ = open_session_for(settings, browser.user_id)
    page = browser.get("/compte").text
    assert "cette session" in page
    ids = [r[0] for r in _rows(settings, "SELECT id FROM sessions WHERE user_id = ?", browser.user_id)]
    other_id = next(i for i in ids if str(i) != str(_current_id(settings, browser)))
    browser.post(f"/compte/sessions/{other_id}/fermer")
    with TestClient(app, follow_redirects=False) as other:
        other.cookies.set(COOKIE_NAME, other_raw)
        assert other.get("/").status_code == 303


def _current_id(settings, browser):
    digest = hashlib.sha256(browser.cookies[COOKIE_NAME].encode()).hexdigest()
    return _rows(settings, "SELECT id FROM sessions WHERE token_hash = ?", digest)[0][0]


def test_profile_update_and_escaping(client_as):
    browser = client_as("volunteer")
    response = browser.post("/compte/profil", data={"nom": "<script>alert(1)</script>"})
    assert response.status_code == 303
    page = browser.get("/compte").text
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


def test_profile_requires_a_name(client_as):
    response = client_as("volunteer").post("/compte/profil", data={"nom": "   "})
    assert response.status_code == 400
    assert "obligatoire" in response.text
