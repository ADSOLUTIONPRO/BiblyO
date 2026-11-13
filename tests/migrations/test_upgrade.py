# SPDX-License-Identifier: EUPL-1.2
"""`biblyo db upgrade` : sauvegarde d'abord, jamais d'écrasement, récupération par une décision humaine."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core import migrate, schema_state
from biblyo.core.backup import list_backups, verify_backup
from biblyo.core.migrate import MigrationLocked, UpgradeFailed, UpgradeRefused, migration_lock, upgrade
from biblyo.core.schema_state import SchemaStatus, read_failure, read_state

from .conftest import fingerprint

HEAD = schema_state.script_directory().get_current_head()
NEW = "9001"
NEWER = "9002"

FAILING = """
    op.create_table("trace_partielle", sa.Column("id", sa.Integer, primary_key=True))
    raise RuntimeError("panne simulée")
"""
STRAY_TABLE = """
    op.create_table("table_inattendue", sa.Column("id", sa.Integer, primary_key=True))
"""
GOOD = """
    op.execute("CREATE INDEX ix_essai ON settings (updated_at)")
"""


def _sante(settings) -> int:
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        return test_client.get("/sante").status_code


def _tables(settings) -> set[str]:
    connection = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    try:
        return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def _commands(procedure: list[str]) -> list[str]:
    return [line for line in procedure if line.startswith("biblyo ")]


def test_dry_run_changes_nothing(bare_settings):
    before = sorted(path.name for path in bare_settings.data_dir.iterdir())
    report = upgrade(bare_settings, dry_run=True)
    assert report.applied[0] == "0001"
    assert report.applied[-1] == HEAD
    assert not bare_settings.database_path.exists()
    assert sorted(path.name for path in bare_settings.data_dir.iterdir()) == before  # ni base ni verrou


def test_dry_run_does_not_create_the_data_directory(bare_settings):
    settings = bare_settings.model_copy(update={"data_dir": bare_settings.data_dir / "absent"})
    assert upgrade(settings, dry_run=True).applied[-1] == HEAD
    assert not settings.data_dir.exists()


def test_dry_run_leaves_an_existing_database_untouched(seeded):
    seeded.migration_lock_path.unlink(missing_ok=True)
    before = fingerprint(seeded.database_path)
    assert upgrade(seeded, dry_run=True).applied == []
    assert fingerprint(seeded.database_path) == before
    assert not seeded.migration_lock_path.exists()


def test_dry_run_still_refuses_a_database_after_a_failure(seeded):
    seeded.migration_failure_path.write_text('{"error": "panne"}', encoding="utf-8")
    with pytest.raises(UpgradeRefused, match="précédente a échoué"):
        upgrade(seeded, dry_run=True)


def test_upgrade_at_head_is_a_no_op_without_backup(seeded):
    before = fingerprint(seeded.database_path)
    report = upgrade(seeded)
    assert report.applied == []
    assert report.backup is None
    assert list_backups(seeded) == []
    assert fingerprint(seeded.database_path) == before


def test_verified_backup_exists_before_first_revision(seeded, revisions, monkeypatch):
    revisions.add(
        NEW,
        HEAD,
        "op.execute('CREATE TABLE IF NOT EXISTS essai_ordre (id INTEGER)')\nop.execute('DROP TABLE essai_ordre')",
    )
    seen = {}
    real_upgrade = command.upgrade

    def spy(config, target):
        archives = list_backups(seeded)
        seen["archives"] = archives
        seen["manifest"] = verify_backup(archives[0])
        seen["revision_at_backup"] = read_state(seeded, ignore_failure=True).current
        real_upgrade(config, target)

    monkeypatch.setattr(migrate.command, "upgrade", spy)
    report = upgrade(seeded)
    assert report.previous == HEAD
    assert report.current == NEW
    assert report.applied == [NEW]
    assert report.backup is not None
    assert len(seen["archives"]) == 1
    assert "pre-migration" in seen["archives"][0].name
    assert seen["manifest"]["schema_revision"] == HEAD
    assert seen["revision_at_backup"] == HEAD
    assert read_state(seeded).status is SchemaStatus.OK


def test_revisions_apply_in_order(seeded, revisions):
    revisions.add(NEW, HEAD, "op.execute('CREATE TABLE essai_a (id INTEGER)')", "op.execute('DROP TABLE essai_a')")
    revisions.add(NEWER, NEW, "op.execute('DROP TABLE essai_a')", "op.execute('CREATE TABLE essai_a (id INTEGER)')")
    assert upgrade(seeded).applied == [NEW, NEWER]


def test_impossible_backup_stops_and_leaves_database_untouched(seeded, revisions, tmp_path):
    revisions.add(NEW, HEAD, GOOD)
    obstacle = tmp_path / "pas-un-dossier"
    obstacle.write_text("")
    seeded.backup_dir = obstacle
    before = fingerprint(seeded.database_path)
    with pytest.raises(UpgradeRefused, match="Sauvegarde impossible"):
        upgrade(seeded)
    assert fingerprint(seeded.database_path) == before
    assert read_failure(seeded) is None
    assert read_state(seeded).status is SchemaStatus.BEHIND


def test_failing_revision_stops_without_overwriting(seeded, revisions):
    revisions.add(NEW, HEAD, FAILING)
    before = fingerprint(seeded.database_path)
    with pytest.raises(UpgradeFailed) as caught:
        upgrade(seeded)
    failure = caught.value

    # La transaction de la révision est annulée (docs/adr/0002) : rien n'a changé, et surtout rien n'est remplacé.
    assert "trace_partielle" not in _tables(seeded)
    assert fingerprint(seeded.database_path) == before

    marker = read_failure(seeded)
    assert marker["revision"] == NEW
    assert "panne simulée" in marker["error"]
    assert read_state(seeded).status is SchemaStatus.FAILED
    assert _sante(seeded) == 503

    archives = list_backups(seeded)
    assert len(archives) == 1
    verify_backup(archives[0])
    assert any(archives[0].name in line for line in failure.procedure)
    assert any(f"Révision en cause : {NEW}" in line for line in failure.details)
    assert any("panne simulée" in line for line in failure.details)
    assert any(str(archives[0]) in line for line in failure.details)


def test_failure_marker_blocks_a_second_upgrade(seeded, revisions):
    revisions.add(NEW, HEAD, FAILING)
    with pytest.raises(UpgradeFailed):
        upgrade(seeded)
    archives = list_backups(seeded)
    with pytest.raises(UpgradeRefused, match="précédente a échoué") as refused:
        upgrade(seeded)
    assert archives[0].name in str(refused.value)
    assert list_backups(seeded) == archives  # aucune nouvelle tentative, aucune nouvelle sauvegarde


def test_failing_final_check_keeps_maintenance_at_head(seeded, revisions):
    revisions.add(NEW, HEAD, STRAY_TABLE)
    with pytest.raises(UpgradeFailed) as caught:
        upgrade(seeded)
    assert any("table_inattendue" in line for line in caught.value.details)
    state = read_state(seeded)
    assert state.current == state.head == NEW
    assert state.status is SchemaStatus.FAILED
    assert _sante(seeded) == 503


def test_missing_required_object_fails_final_check(seeded, monkeypatch):
    monkeypatch.setattr(migrate, "REQUIRED_OBJECTS", frozenset({"catalogue_fts"}))
    with pytest.raises(UpgradeFailed) as caught:
        upgrade(seeded)
    assert any("catalogue_fts" in line for line in caught.value.details)


def test_recovery_procedure_runs_as_displayed(seeded, revisions, run_cli):
    """Les commandes affichées, exécutées sans modification, redonnent des données identiques."""
    revisions.add(NEW, HEAD, STRAY_TABLE)
    before = fingerprint(seeded.database_path)

    result = run_cli(seeded, "biblyo db upgrade")
    assert result.exit_code == 1
    displayed = result.stderr.splitlines()
    assert any(line.startswith("Sauvegarde vérifiée : ") for line in displayed)
    modified = fingerprint(seeded.database_path)
    assert modified != before

    commands = _commands(displayed)
    assert commands[0] == "biblyo db status"
    assert any(c.startswith("biblyo backup restore ") for c in commands)
    for line in commands:
        outcome = run_cli(seeded, line)
        allowed = (0, 2) if line == "biblyo db status" else (0,)  # `db status` signale un schéma en attente
        assert outcome.exit_code in allowed, f"{line}\n{outcome.output}"

    assert fingerprint(seeded.database_path) == before
    assert read_failure(seeded) is None
    assert read_state(seeded).status is SchemaStatus.BEHIND

    kept = list(seeded.data_dir.glob("avant-restauration-*"))
    assert len(kept) == 1
    assert fingerprint(kept[0] / "biblyo.sqlite") == modified


def test_recovery_then_fixed_revision_succeeds(seeded, revisions, run_cli):
    revisions.add(NEW, HEAD, FAILING)
    failed = run_cli(seeded, "biblyo db upgrade")
    assert failed.exit_code == 1
    for line in _commands(failed.stderr.splitlines()):
        assert run_cli(seeded, line).exit_code in (0, 2)

    (revisions.directory / "versions" / f"{NEW}_test.py").unlink()
    revisions.add(NEW, HEAD, GOOD)
    # L'index n'est pas décrit par les modèles : la vérification le signale, comme pour tout écart.
    revisions.add(NEWER, NEW, "op.execute('DROP INDEX ix_essai')")
    fixed = run_cli(seeded, "biblyo db upgrade")
    assert fixed.exit_code == 0, fixed.output
    assert read_state(seeded).status is SchemaStatus.OK


def test_failed_fresh_install_recovery_as_displayed(bare_settings, revisions, run_cli):
    """Base neuve : pas de sauvegarde, et la procédure l'indique."""
    (revisions.directory / "versions" / "0001_socle.py").rename(revisions.directory / "versions" / "0001_socle.keep")
    revisions.add("0001", None, FAILING)
    failed = run_cli(bare_settings, "biblyo db upgrade")
    assert failed.exit_code == 1
    displayed = failed.stderr.splitlines()
    assert any("la base était neuve" in line for line in displayed)
    assert read_state(bare_settings).status is SchemaStatus.FAILED
    assert list_backups(bare_settings) == []

    assert any(str(bare_settings.database_path) in line for line in displayed)
    for path in bare_settings.data_dir.glob("biblyo.sqlite*"):
        path.unlink()
    (revisions.directory / "versions" / "0001_test.py").unlink()
    (revisions.directory / "versions" / "0001_socle.keep").rename(revisions.directory / "versions" / "0001_socle.py")
    schema_state._load_scripts.cache_clear()
    commands = _commands(displayed)
    assert commands == ["biblyo db upgrade"]
    assert run_cli(bare_settings, commands[0]).exit_code == 0
    assert read_state(bare_settings).status is SchemaStatus.OK


def test_two_simultaneous_upgrades_second_is_refused(seeded):
    before = fingerprint(seeded.database_path)
    with migration_lock(seeded), pytest.raises(MigrationLocked):
        upgrade(seeded)
    assert fingerprint(seeded.database_path) == before
    upgrade(seeded)  # le verrou est rendu


def test_database_newer_than_code_is_refused_without_descent(seeded):
    connection = sqlite3.connect(seeded.database_path)
    connection.execute("UPDATE alembic_version SET version_num = '9999'")
    connection.commit()
    connection.close()
    before = fingerprint(seeded.database_path)
    assert read_state(seeded).status is SchemaStatus.AHEAD
    with pytest.raises(UpgradeRefused, match="trop ancienne"):
        upgrade(seeded)
    assert fingerprint(seeded.database_path) == before
    assert list_backups(seeded) == []


def test_foreign_file_is_refused(bare_settings):
    bare_settings.database_path.write_bytes(b"ceci n'est pas une base SQLite, loin de la" * 100)
    assert read_state(bare_settings).status is SchemaStatus.INCOMPATIBLE
    with pytest.raises(UpgradeRefused):
        upgrade(bare_settings)


def test_other_sqlite_database_is_refused(bare_settings):
    connection = sqlite3.connect(bare_settings.database_path)
    connection.execute("CREATE TABLE autre (id INTEGER)")
    connection.commit()
    connection.close()
    before = fingerprint(bare_settings.database_path)
    assert read_state(bare_settings).status is SchemaStatus.INCOMPATIBLE
    with pytest.raises(UpgradeRefused):
        upgrade(bare_settings)
    assert fingerprint(bare_settings.database_path) == before


def test_database_with_anomalies_is_refused(seeded, revisions):
    revisions.add(NEW, HEAD, GOOD)
    connection = sqlite3.connect(seeded.database_path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("INSERT INTO user_roles (user_id, role_id) VALUES (999, 999)")
    connection.commit()
    connection.close()
    with pytest.raises(UpgradeRefused, match="anomalies"):
        upgrade(seeded)
    assert list_backups(seeded) == []
    assert read_failure(seeded) is None


def test_sqlite_too_old_is_incompatible(settings, monkeypatch):
    monkeypatch.setattr(schema_state, "MINIMUM_SQLITE", (99, 0, 0))
    assert read_state(settings).status is SchemaStatus.INCOMPATIBLE


def test_cli_status_and_check(settings, run_cli):
    status = run_cli(settings, "biblyo db status")
    assert status.exit_code == 0
    assert "Intégrité : correcte" in status.output
    check = run_cli(settings, "biblyo db check")
    assert check.exit_code == 0
    assert "schéma conforme" in check.output


def test_cli_status_exit_code_when_absent(bare_settings, run_cli):
    status = run_cli(bare_settings, "biblyo db status")
    assert status.exit_code == 2
    assert "non initialisée" in status.output
    assert not bare_settings.database_path.exists()


def test_cli_check_reports_behind_without_failing(seeded, revisions, run_cli):
    revisions.add(NEW, HEAD, GOOD)
    check = run_cli(seeded, "biblyo db check")
    assert check.exit_code == 0
    assert f"Révision {HEAD}, attendue {NEW}" in check.output


def test_cli_downgrade_refused_in_production(settings, run_cli):
    settings.env = "production"
    before = fingerprint(settings.database_path)
    result = run_cli(settings, "biblyo db downgrade base")
    assert result.exit_code == 1
    assert "Refusé en production" in result.stderr
    assert fingerprint(settings.database_path) == before


def test_cli_upgrade_refused_when_locked(settings, run_cli):
    with migration_lock(settings):
        result = run_cli(settings, "biblyo db upgrade")
    assert result.exit_code == 1
    assert "en cours" in result.stderr
