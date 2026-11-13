# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

import pytest

from biblyo.core.security import passwords


def test_hash_is_argon2id_and_verifies():
    hashed = passwords.hash_password("Lecture-Partagee-42")
    assert hashed.startswith("$argon2id$")
    assert "Lecture-Partagee-42" not in hashed
    assert passwords.verify_password(hashed, "Lecture-Partagee-42")
    assert not passwords.verify_password(hashed, "autre mot de passe")


def test_malformed_hash_is_refused_without_error():
    assert not passwords.verify_password("pas un hachage", "x")


def test_oversized_password_is_refused_before_hashing():
    hashed = passwords.hash_password("Lecture-Partagee-42")
    assert not passwords.verify_password(hashed, "x" * 10_000)


@pytest.mark.parametrize(
    ("candidate", "username", "fragment"),
    [
        ("court", "", "au moins 10"),
        ("x" * 129, "", "trop long"),
        ("motdepasse", "", "trop courant"),
        ("AZERTYUIOP", "", "trop courant"),
        ("aaaaaaaaaaaa", "", "trop courant"),
        ("camille-la-lectrice", "Camille-la-Lectrice", "identifiant"),
    ],
)
def test_policy_refuses(candidate, username, fragment):
    assert fragment in passwords.policy_error(candidate, username)


def test_policy_accepts_a_reasonable_password():
    assert passwords.policy_error("Lecture-Partagee-42", "camille") is None


def test_temporary_password_passes_the_policy():
    assert passwords.policy_error(passwords.generate_temporary()) is None


def test_dummy_verify_runs_on_any_input():
    passwords.dummy_verify("n'importe quoi")
