# SPDX-License-Identifier: EUPL-1.2
"""Révision 0007 : lecteurs et foyers, rôles fournis ajustés sans écraser une personnalisation, retour arrière."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences

MOMENT = "2026-11-08T00:00:00Z"
PATRON = (
    "INSERT INTO patrons (public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
    " registered_on, version, created_at, updated_at) VALUES "
)
CARD = "INSERT INTO patron_cards (number, patron_id, issued_on, ended_on, end_reason, created_at) VALUES "


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _permissions(settings, code: str) -> set[str]:
    connection = _connect(settings)
    try:
        rows = connection.execute(
            "SELECT permission FROM role_permissions JOIN roles ON roles.id = role_id WHERE roles.code = ?", (code,)
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def _execute(settings, query: str, *params) -> None:
    connection = _connect(settings)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def at_0006(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0006")
    return bare_settings


def _shipped(settings):
    return {code: _permissions(settings, code) for code in ("admin", "librarian", "volunteer", "viewer")}


# --- Rôles tels que livrés ----------------------------------------------------------------------------------------
def test_shipped_roles_are_adjusted(at_0006):
    before = _shipped(at_0006)
    command.upgrade(alembic_config(at_0006), "0007")
    after = _shipped(at_0006)
    assert after["librarian"] == before["librarian"] - {"patrons.anonymize"} | {"patrons.register", "patrons.export"}
    assert after["volunteer"] == before["volunteer"] | {"patrons.register"}
    assert after["admin"] == before["admin"] == {"*"}
    assert after["viewer"] == before["viewer"]
    command.upgrade(alembic_config(at_0006), "head")  # les modèles décrivent le schéma de la dernière révision
    assert schema_differences(at_0006) == []


def test_downgrade_restores_the_shipped_roles_exactly(at_0006):
    before = _shipped(at_0006)
    command.upgrade(alembic_config(at_0006), "0007")
    command.downgrade(alembic_config(at_0006), "0006")
    assert _shipped(at_0006) == before
    connection = _connect(at_0006)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    sequences = {row[0] for row in connection.execute("SELECT name FROM sequences")}
    connection.close()
    assert not tables & {"patrons", "patron_cards", "households", "contacts", "role_permission_changes"}
    assert sequences == {"copy_barcode"}
    command.upgrade(alembic_config(at_0006), "head")  # et l'on remonte sans difficulté
    assert _shipped(at_0006)["volunteer"] == before["volunteer"] | {"patrons.register"}


# --- Rôles personnalisés ------------------------------------------------------------------------------------------
def test_a_customized_role_is_never_overwritten(at_0006):
    _execute(at_0006, "DELETE FROM role_permissions WHERE permission = 'admin.sources'")  # bibliothécaire personnalisé
    custom = _permissions(at_0006, "librarian")
    volunteer = _permissions(at_0006, "volunteer")
    command.upgrade(alembic_config(at_0006), "0007")
    assert _permissions(at_0006, "librarian") == custom  # pas d'ajout, pas de retrait : anonymize reste
    assert "patrons.anonymize" in custom
    assert _permissions(at_0006, "volunteer") == volunteer | {"patrons.register"}  # l'autre rôle, intact, est ajusté
    connection = _connect(at_0006)
    kept = connection.execute("SELECT details FROM audit_log WHERE action = 'migration.role_kept'").fetchall()
    connection.close()
    assert len(kept) == 1 and "librarian" in kept[0][0]


def test_a_local_role_and_a_later_change_survive_the_downgrade(at_0006):
    _execute(
        at_0006,
        "INSERT INTO roles (id, code, label, is_system, created_at) VALUES (9, 'accueil_plus', 'Accueil+', 0, ?)",
        MOMENT,
    )
    _execute(at_0006, "INSERT INTO role_permissions (role_id, permission) VALUES (9, 'patrons.view')")
    command.upgrade(alembic_config(at_0006), "0007")
    assert _permissions(at_0006, "accueil_plus") == {"patrons.view"}  # rôle créé localement : jamais touché
    _execute(
        at_0006, "INSERT INTO role_permissions (role_id, permission) VALUES (2, 'labels.special')"
    )  # choix local postérieur
    command.downgrade(alembic_config(at_0006), "0006")
    librarian = _permissions(at_0006, "librarian")
    assert "labels.special" in librarian  # la descente ne défait que ce que la montée a fait
    assert "patrons.anonymize" in librarian and "patrons.export" not in librarian
    assert _permissions(at_0006, "accueil_plus") == {"patrons.view"}


def test_missing_shipped_role_is_tolerated(at_0006):
    _execute(at_0006, "DELETE FROM user_roles WHERE role_id = 3")
    _execute(at_0006, "DELETE FROM roles WHERE code = 'volunteer'")
    command.upgrade(alembic_config(at_0006), "0007")
    assert "patrons.register" in _permissions(at_0006, "librarian")


# --- Données : jamais supprimées en silence -------------------------------------------------------------------------
def _patron(settings, patron_id: int = 1) -> None:
    _execute(
        settings,
        "INSERT INTO patrons (id, public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
        " registered_on, version, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Léa', 'Martin', 'lea martin', 'martin lea', 'minor', 'active', '2026-11-08', 1, ?, ?)",
        patron_id,
        f"u{patron_id}",
        f"P-{patron_id:06d}",
        MOMENT,
        MOMENT,
    )


@pytest.mark.parametrize("table", ["patrons", "households", "patron_cards"])
def test_downgrade_is_refused_when_reader_data_exists(at_0006, table):
    command.upgrade(alembic_config(at_0006), "0007")
    if table == "households":
        _execute(
            at_0006,
            "INSERT INTO households (public_id, kind, name, name_norm, version, created_at, updated_at)"
            " VALUES ('h1', 'family', 'Famille', 'famille', 1, ?, ?)",
            MOMENT,
            MOMENT,
        )
    else:
        _patron(at_0006)
        if table == "patron_cards":
            _execute(at_0006, "DELETE FROM patrons")
            _execute(at_0006, CARD + "('L-000001', NULL, '2026-11-08', '2026-11-08', 'anonymized', ?)", MOMENT)
    with pytest.raises(RuntimeError, match="jamais supprimées"):
        command.downgrade(alembic_config(at_0006), "0006")
    connection = _connect(at_0006)
    assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1  # noqa: S608
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0007"
    connection.close()


# --- Contraintes ------------------------------------------------------------------------------------------------------
@pytest.fixture
def at_head(bare_settings):
    command.upgrade(alembic_config(bare_settings), "head")
    return bare_settings


@pytest.mark.parametrize(
    "query",
    [
        # deux lecteurs ne partagent ni UUID ni numéro permanent
        PATRON + "('u1', 'P-000099', 'A', 'B', 'a b', 'b a', 'adult', 'active', 'x', 1, 'x', 'x')",
        PATRON + "('u9', 'P-000001', 'A', 'B', 'a b', 'b a', 'adult', 'active', 'x', 1, 'x', 'x')",
        # catégorie et statut fermés, nom obligatoire hors anonymisation
        PATRON + "('u2', 'P-000002', 'A', 'B', 'a b', 'b a', 'enfant', 'active', 'x', 1, 'x', 'x')",
        PATRON + "('u3', 'P-000003', 'A', ' ', 'a', 'a', 'adult', 'active', 'x', 1, 'x', 'x')",
        # une seule carte en service ; un numéro de carte pour toujours
        "INSERT INTO patron_cards (number, patron_id, issued_on, created_at) VALUES ('L-000002', 1, 'x', 'x')",
        CARD + "('L-000001', 1, 'x', 'y', 'lost', 'x')",
        "INSERT INTO patron_cards (number, patron_id, issued_on, ended_on, created_at) VALUES ('L-000003', 1, 'x', 'y', 'x')",  # noqa: E501
        "INSERT INTO patron_cards (number, patron_id, issued_on, created_at) VALUES ('L-000004', NULL, 'x', 'x')",
        # une coordonnée a un seul propriétaire, et une adresse a sa forme
        "INSERT INTO contacts (patron_id, household_id, kind, value, is_primary, created_at, updated_at) VALUES (1, 1, 'email', 'a@example.org', 0, 'x', 'x')",  # noqa: E501
        "INSERT INTO contacts (patron_id, kind, value, is_primary, created_at, updated_at) VALUES (1, 'address', 'x', 0, 'x', 'x')",  # noqa: E501
        # personne n'est son propre responsable ; un rôle de foyer est fermé
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank, version, created_at, updated_at) VALUES (1, 1, 'parent', 1, 1, 1, 1, 'x', 'x')",  # noqa: E501
        "INSERT INTO household_members (household_id, patron_id, role, joined_on) VALUES (1, 1, 'chef', 'x')",
        # liens sans lecteur ni foyer existants
        "INSERT INTO household_members (household_id, patron_id, role, joined_on) VALUES (1, 77, 'member', 'x')",
    ],
)
def test_constraints_hold(at_head, query):
    connection = _connect(at_head)
    connection.execute(
        "INSERT INTO patrons (id, public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
        " registered_on, version, created_at, updated_at) VALUES (1, 'u1', 'P-000001', 'Léa', 'Martin', 'lea martin',"
        " 'martin lea', 'minor', 'active', 'x', 1, 'x', 'x')"
    )
    connection.execute(
        "INSERT INTO households (id, public_id, kind, name, name_norm, version, created_at, updated_at)"
        " VALUES (1, 'h1', 'family', 'Famille Martin', 'famille martin', 1, 'x', 'x')"
    )
    connection.execute(
        "INSERT INTO patron_cards (number, patron_id, issued_on, created_at) VALUES ('L-000001', 1, 'x', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(query)
    connection.close()


def test_an_anonymized_reader_may_have_no_name_and_a_detached_card(at_head):
    connection = _connect(at_head)
    connection.execute(
        "INSERT INTO patrons (id, public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
        " registered_on, version, created_at, updated_at)"
        " VALUES (1, 'u1', 'P-000001', '', '', '', '', 'adult', 'anonymized', 'x', 1, 'x', 'x')"
    )
    connection.execute(
        "INSERT INTO patron_cards (number, patron_id, issued_on, ended_on, end_reason, created_at)"
        " VALUES ('L-000001', NULL, 'x', 'y', 'anonymized', 'x')"
    )
    connection.close()
