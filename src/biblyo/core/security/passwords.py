# SPDX-License-Identifier: EUPL-1.2
"""Mots de passe : argon2id et politique (04-SECURITE-ROLES.md, § 1)."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from biblyo.core.i18n import _

MIN_LENGTH = 10
MAX_LENGTH = 128  # argon2 reçoit une longueur bornée : pas de déni de service par un mot de passe géant

#: Liste locale, sans appel réseau. Seuls comptent les mots d'au moins MIN_LENGTH caractères.
COMMON_PASSWORDS = frozenset(
    {
        "0123456789",
        "000000000000",
        "1234567890",
        "12345678910",
        "123456789012",
        "0987654321",
        "1111111111",
        "administrateur",
        "administrator",
        "adminadmin",
        "azertyuiop",
        "azertyuiop1",
        "azerty12345",
        "azerty123456",
        "bibliotheque",
        "bibliothèque",
        "bibliothecaire",
        "bibliothécaire",
        "biblyobiblyo",
        "bonjour123",
        "changeme123",
        "football123",
        "iloveyou123",
        "jetaime1234",
        "letmein1234",
        "motdepasse",
        "motdepasse1",
        "motdepasse123",
        "password12",
        "password123",
        "password1234",
        "qwerty12345",
        "qwerty123456",
        "qwertyuiop",
        "soleil12345",
        "superadmin",
        "utilisateur",
        "welcome123",
    }
)

_hasher = PasswordHasher()
_dummy_hash: str | None = None


def configure(*, time_cost: int, memory_cost: int, parallelism: int) -> None:
    """Paramètres argon2id. Réservé aux tests, qui veulent des hachages rapides."""
    global _hasher, _dummy_hash
    _hasher = PasswordHasher(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)
    _dummy_hash = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def dummy_verify(password: str) -> None:
    """Même coût pour un identifiant inconnu : le temps de réponse ne révèle pas si un compte existe."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash(secrets.token_urlsafe(16))
    verify_password(_dummy_hash, password)


def generate_temporary() -> str:
    return secrets.token_urlsafe(12)


def policy_error(password: str, username: str = "") -> str | None:
    """Motif de refus d'un mot de passe, ou None."""
    if len(password) < MIN_LENGTH:
        return _("Le mot de passe doit compter au moins 10 caractères.")
    if len(password) > MAX_LENGTH:
        return _("Le mot de passe est trop long (128 caractères au maximum).")
    lowered = password.casefold()
    if lowered in COMMON_PASSWORDS or len(set(password)) == 1:
        return _("Ce mot de passe est trop courant. Choisissez-en un autre.")
    if username and lowered == username.casefold():
        return _("Le mot de passe ne peut pas être votre identifiant.")
    return None
