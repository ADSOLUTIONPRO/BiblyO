# SPDX-License-Identifier: EUPL-1.2
"""Comptes : connexion, première installation, utilisateurs. Les règles sont ici, les routes ne font qu'appeler."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete

from biblyo.config import Settings
from biblyo.core import audit
from biblyo.core.clock import now, now_iso, to_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.models import AuthSession, User
from biblyo.core.security import install as install_tokens
from biblyo.core.security import passwords, sessions
from biblyo.core.settings_store import set_setting
from biblyo.modules.admin import repository as repo

LOCK_AFTER_FAILURES = 5
MAX_LOCK_MINUTES = 60
USERNAME_PATTERN = re.compile(r"^[\w.@-]{2,64}$")
NAME_MAX_LENGTH = 120


class InstallationClosed(Exception):
    """Un compte existe déjà : l'installation ne se rejoue pas."""


def lock_duration(failures: int) -> timedelta | None:
    """Verrouillage progressif : 1, 2, 4… minutes à partir du cinquième échec, une heure au plus."""
    if failures < LOCK_AFTER_FAILURES:
        return None
    return timedelta(minutes=min(2 ** (failures - LOCK_AFTER_FAILURES), MAX_LOCK_MINUTES))


def _username_error(username: str) -> str | None:
    if not USERNAME_PATTERN.match(username):
        return _("L’identifiant compte de 2 à 64 caractères : lettres, chiffres, point, tiret, arobase.")
    return None


def _name_error(name: str, label: str) -> str | None:
    if not name.strip():
        return _("{label} est obligatoire.").format(label=label)
    if len(name) > NAME_MAX_LENGTH:
        return _("{label} est trop long.").format(label=label)
    return None


def _clean(fields: dict[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if value}


def authenticate(database: Database, username: str, password: str, ip: str | None) -> int | None:
    """Rend l'identifiant du compte, ou None : la raison du refus ne sort jamais d'ici."""
    with database.session() as session:
        user = repo.get_by_username(session, username)
        found = (user.id, user.password_hash, user.is_active, user.locked_until) if user else None

    # Le hachage se fait hors transaction : il ne doit pas retenir le verrou d'écriture.
    if found is None:
        passwords.dummy_verify(password)
        with database.session(write=True) as session:
            audit.record(session, "login.failed", "user", details={"motif": "inconnu", "ip": ip})
        return None

    user_id, password_hash, active, locked_until = found
    valid = passwords.verify_password(password_hash, password)
    locked = locked_until is not None and locked_until > now_iso()
    with database.session(write=True) as session:
        account = session.get(User, user_id)
        if account is None:
            return None
        if valid and active and not locked:
            account.failed_logins = 0
            account.locked_until = None
            account.last_login_at = now_iso()
            if passwords.needs_rehash(password_hash):
                account.password_hash = passwords.hash_password(password)
            audit.record(session, "login.ok", "user", user_id, user_id=user_id, details={"ip": ip})
            return user_id
        reason = "desactive" if not active else "verrouille" if locked else "mot_de_passe"
        if active and not locked:
            account.failed_logins += 1
            duration = lock_duration(account.failed_logins)
            if duration:
                account.locked_until = to_iso(now() + duration)
        audit.record(session, "login.failed", "user", user_id, details={"motif": reason, "ip": ip})
    return None


def open_session(database: Database, user_id: int, settings: Settings, ip: str | None, agent: str | None) -> str:
    with database.session(write=True) as session:
        return sessions.open_session(session, user_id, settings, ip, agent)


def close_session(database: Database, session_id: int, user_id: int) -> None:
    with database.session(write=True) as session:
        session.execute(delete(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user_id))
        audit.record(session, "session.closed", "user", user_id, user_id=user_id)


@dataclass(frozen=True)
class InstallForm:
    token: str
    library_name: str
    display_name: str
    username: str
    password: str
    confirmation: str


def install(database: Database, form: InstallForm, settings: Settings, ip: str | None, agent: str | None) -> str:
    """Crée le premier administrateur et rend le jeton de sa session. Se ferme définitivement ensuite."""
    username = form.username.strip()
    errors = _clean(
        {
            "library_name": _name_error(form.library_name, _("Le nom de la bibliothèque")),
            "display_name": _name_error(form.display_name, _("Votre nom")),
            "username": _username_error(username),
            "password": passwords.policy_error(form.password, username),
            "confirmation": None if form.password == form.confirmation else _("Les deux saisies diffèrent."),
        }
    )
    with database.session() as session:
        if not install_tokens.is_open(session):
            raise InstallationClosed
        if not install_tokens.check_token(session, form.token.strip()):
            raise FormErrors({"token": _("Jeton incorrect."), **errors})
    if errors:
        raise FormErrors(errors)

    password_hash = passwords.hash_password(form.password)
    moment = now_iso()
    with database.session(write=True) as session:
        # Deux installations simultanées : seul le premier écrivain retrouve l'installation ouverte et le jeton valide.
        if not install_tokens.is_open(session) or not install_tokens.check_token(session, form.token.strip()):
            raise InstallationClosed
        account = User(
            username=username,
            display_name=form.display_name.strip(),
            password_hash=password_hash,
            last_login_at=moment,
            created_at=moment,
            updated_at=moment,
        )
        account.roles = list(repo.roles_by_codes(session, [repo.ADMIN_ROLE]))
        session.add(account)
        session.flush()
        set_setting(session, "library.name", form.library_name.strip(), account.id)
        install_tokens.consume_token(session)
        audit.record(session, "install.done", "user", account.id, user_id=account.id, details={"ip": ip})
        return sessions.open_session(session, account.id, settings, ip, agent)


def update_profile(database: Database, user_id: int, display_name: str) -> None:
    error = _name_error(display_name, _("Votre nom"))
    if error:
        raise FormErrors({"display_name": error})
    with database.session(write=True) as session:
        account = session.get(User, user_id)
        if account is None:
            raise BusinessError(_("Compte introuvable."))
        account.display_name = display_name.strip()
        account.updated_at = now_iso()


def change_password(
    database: Database,
    user_id: int,
    current: str,
    new: str,
    confirmation: str,
    settings: Settings,
    ip: str | None,
    agent: str | None,
) -> str:
    """Change le mot de passe, ferme toutes les sessions du compte et en rouvre une pour ce navigateur."""
    with database.session() as session:
        account = session.get(User, user_id)
        if account is None:
            raise BusinessError(_("Compte introuvable."))
        username, password_hash = account.username, account.password_hash
    errors: dict[str, str] = {}
    if not passwords.verify_password(password_hash, current):
        errors["current"] = _("Le mot de passe actuel est incorrect.")
    policy = passwords.policy_error(new, username)
    if policy:
        errors["new"] = policy
    elif new == current:
        errors["new"] = _("Le nouveau mot de passe doit différer de l’actuel.")
    if new != confirmation:
        errors["confirmation"] = _("Les deux saisies diffèrent.")
    if errors:
        raise FormErrors(errors)
    new_hash = passwords.hash_password(new)
    with database.session(write=True) as session:
        account = session.get(User, user_id)
        if account is None:
            raise BusinessError(_("Compte introuvable."))
        account.password_hash = new_hash
        account.must_change_password = False
        account.updated_at = now_iso()
        sessions.revoke_all(session, user_id)
        audit.record(session, "password.changed", "user", user_id, user_id=user_id)
        return sessions.open_session(session, user_id, settings, ip, agent)


def create_user(database: Database, actor_id: int | None, username: str, display_name: str, roles: list[str]) -> str:
    """Crée un compte et rend son mot de passe provisoire, à changer à la première connexion."""
    username = username.strip()
    errors = _clean(
        {
            "username": _username_error(username),
            "display_name": _name_error(display_name, _("Le nom")),
            "roles": None if roles else _("Choisissez au moins un rôle."),
        }
    )
    if errors:
        raise FormErrors(errors)
    temporary = passwords.generate_temporary()
    password_hash = passwords.hash_password(temporary)
    moment = now_iso()
    with database.session(write=True) as session:
        if repo.get_by_username(session, username) is not None:
            raise FormErrors({"username": _("Cet identifiant est déjà pris.")})
        chosen = list(repo.roles_by_codes(session, roles))
        if len(chosen) != len(set(roles)):
            raise FormErrors({"roles": _("Rôle inconnu.")})
        account = User(
            username=username,
            display_name=display_name.strip(),
            password_hash=password_hash,
            must_change_password=True,
            created_at=moment,
            updated_at=moment,
        )
        account.roles = chosen
        session.add(account)
        session.flush()
        audit.record(
            session,
            "user.created",
            "user",
            account.id,
            user_id=actor_id,
            details={"roles": sorted(r.code for r in chosen)},
        )
    return temporary


def update_user(
    database: Database, actor_id: int | None, user_id: int, display_name: str, roles: list[str], is_active: bool
) -> None:
    """Modifie un compte. Refuse tout ce qui laisserait l'installation sans administrateur actif."""
    errors = _clean(
        {
            "display_name": _name_error(display_name, _("Le nom")),
            "roles": None if roles else _("Choisissez au moins un rôle."),
        }
    )
    if errors:
        raise FormErrors(errors)
    if actor_id == user_id and not is_active:
        raise BusinessError(_("Vous ne pouvez pas désactiver votre propre compte."))
    with database.session(write=True) as session:
        account = session.get(User, user_id)
        if account is None:
            raise BusinessError(_("Compte introuvable."))
        chosen = list(repo.roles_by_codes(session, roles))
        if len(chosen) != len(set(roles)):
            raise FormErrors({"roles": _("Rôle inconnu.")})
        before = sorted(r.code for r in account.roles)
        was_active = account.is_active
        account.display_name = display_name.strip()
        account.roles = chosen
        account.is_active = is_active
        account.updated_at = now_iso()
        session.flush()
        if repo.count_active_admins(session) == 0:
            raise BusinessError(_("Il doit rester au moins un administrateur actif."))  # annule toute la transaction
        after = sorted(r.code for r in chosen)
        if before != after:
            audit.record(
                session,
                "user.roles_changed",
                "user",
                user_id,
                user_id=actor_id,
                details={"avant": before, "apres": after},
            )
        if was_active != is_active:
            action = "user.enabled" if is_active else "user.disabled"
            audit.record(session, action, "user", user_id, user_id=actor_id)
        if not is_active:
            sessions.revoke_all(session, user_id)


def reset_password(database: Database, actor_id: int | None, username_or_id: int | str) -> str:
    """Pose un mot de passe provisoire, ferme les sessions, lève le verrouillage. Rend le mot de passe."""
    temporary = passwords.generate_temporary()
    password_hash = passwords.hash_password(temporary)
    with database.session(write=True) as session:
        if isinstance(username_or_id, int):
            account = session.get(User, username_or_id)
        else:
            account = repo.get_by_username(session, username_or_id)
        if account is None:
            raise BusinessError(_("Compte introuvable."))
        account.password_hash = password_hash
        account.must_change_password = True
        account.failed_logins = 0
        account.locked_until = None
        account.updated_at = now_iso()
        sessions.revoke_all(session, account.id)
        audit.record(session, "password.reset", "user", account.id, user_id=actor_id)
    return temporary


def create_account_with_password(
    database: Database, username: str, display_name: str, role: str, password: str
) -> None:
    """Création en ligne de commande : le mot de passe est choisi sur le serveur par l'administrateur."""
    username = username.strip()
    errors = _clean(
        {
            "username": _username_error(username),
            "display_name": _name_error(display_name, _("Le nom")),
            "password": passwords.policy_error(password, username),
        }
    )
    if errors:
        raise FormErrors(errors)
    password_hash = passwords.hash_password(password)
    moment = now_iso()
    with database.session(write=True) as session:
        if repo.get_by_username(session, username) is not None:
            raise FormErrors({"username": _("Cet identifiant est déjà pris.")})
        chosen = list(repo.roles_by_codes(session, [role]))
        if not chosen:
            raise FormErrors({"roles": _("Rôle inconnu.")})
        account = User(
            username=username,
            display_name=display_name.strip(),
            password_hash=password_hash,
            created_at=moment,
            updated_at=moment,
        )
        account.roles = chosen
        session.add(account)
        session.flush()
        install_tokens.consume_token(session)
        audit.record(session, "user.created", "user", account.id, details={"roles": [role], "origine": "commande"})
