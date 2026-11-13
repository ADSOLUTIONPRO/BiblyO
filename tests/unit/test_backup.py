# SPDX-License-Identifier: EUPL-1.2
"""Sauvegarde et restauration : mêmes données, archives altérées refusées, rien de détruit."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from biblyo.cli import cli
from biblyo.core.backup import BackupError, create_backup, list_backups, restore_backup, verify_backup
from biblyo.core.schema_state import SchemaStatus, read_state, script_directory


def _dump(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def _change(settings) -> None:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("INSERT INTO settings (key, value, updated_at) VALUES ('apres', '1', '2026-01-01T00:00:00Z')")
    connection.commit()
    connection.close()


def _rewrite(archive: Path, target: Path, edit) -> Path:
    """Copie de l'archive dont le manifeste (ou la base) est modifié par `edit(dossier)`."""
    work = target / "extrait"
    work.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(work, filter="data")
    edit(work)
    forged = target / "falsifiee.tar.gz"
    with tarfile.open(forged, "w:gz") as output:
        for path in sorted(work.rglob("*")):
            if path.is_file():
                output.add(path, arcname=path.relative_to(work).as_posix())
    return forged


def test_backup_contains_manifest_with_counts_and_digests(settings):
    info = create_backup(settings)
    assert info.path.name.startswith("biblyo-")
    assert info.path.name.endswith("-manual.tar.gz")
    manifest = verify_backup(info.path)
    assert manifest["schema_revision"] == script_directory().get_current_head()
    assert manifest["counts"]["roles"] == 4
    assert manifest["motif"] == "manual"
    assert len(manifest["files"]["biblyo.sqlite"]["sha256"]) == 64


def test_backup_without_database_is_refused(bare_settings):
    with pytest.raises(BackupError, match="Aucune base"):
        create_backup(bare_settings)


def test_consecutive_backups_never_overwrite(settings):
    first = create_backup(settings)
    second = create_backup(settings)
    assert first.path != second.path
    assert len(list_backups(settings)) == 2


def test_no_incomplete_archive_is_left_behind(settings):
    create_backup(settings)
    assert sorted(p.name for p in settings.backup_dir.iterdir()) == [list_backups(settings)[0].name]


def test_restore_gives_back_the_same_data_and_keeps_current_state(settings):
    before = _dump(settings.database_path)
    info = create_backup(settings)
    _change(settings)
    changed = _dump(settings.database_path)

    kept = restore_backup(settings, info.path)

    assert _dump(settings.database_path) == before
    assert read_state(settings).status is SchemaStatus.OK
    assert kept is not None
    assert kept.name.startswith("avant-restauration-")
    assert _dump(kept / "biblyo.sqlite") == changed


def test_restore_includes_media(settings):
    media = settings.data_dir / "media" / "couvertures"
    media.mkdir(parents=True)
    (media / "a.jpg").write_bytes(b"image")
    info = create_backup(settings)
    (media / "a.jpg").write_bytes(b"autre")
    (media / "b.jpg").write_bytes(b"nouvelle")

    kept = restore_backup(settings, info.path)

    assert (media / "a.jpg").read_bytes() == b"image"
    assert not (media / "b.jpg").exists()
    assert (kept / "media" / "couvertures" / "b.jpg").read_bytes() == b"nouvelle"


def test_restore_on_a_new_installation(settings, tmp_path):
    from biblyo.config import Settings

    info = create_backup(settings)
    (tmp_path / "neuve").mkdir()
    fresh = Settings(env="test", data_dir=tmp_path / "neuve", backup_dir=settings.backup_dir)
    assert restore_backup(fresh, info.path) is None
    assert _dump(fresh.database_path) == _dump(settings.database_path)
    assert read_state(fresh).status is SchemaStatus.OK


def test_altered_manifest_is_refused(settings, tmp_path):
    info = create_backup(settings)

    def edit(work: Path) -> None:
        manifest = json.loads((work / "manifest.json").read_text())
        manifest["files"]["biblyo.sqlite"]["sha256"] = "0" * 64
        (work / "manifest.json").write_text(json.dumps(manifest))

    forged = _rewrite(info.path, tmp_path, edit)
    with pytest.raises(BackupError, match="Empreinte"):
        verify_backup(forged)
    before = _dump(settings.database_path)
    with pytest.raises(BackupError):
        restore_backup(settings, forged)
    assert _dump(settings.database_path) == before
    assert not list(settings.data_dir.glob("avant-restauration-*"))


def test_altered_database_is_refused(settings, tmp_path):
    info = create_backup(settings)

    def edit(work: Path) -> None:
        with (work / "biblyo.sqlite").open("r+b") as handle:
            handle.seek(5000)
            handle.write(b"\xff" * 64)

    with pytest.raises(BackupError, match="Empreinte"):
        verify_backup(_rewrite(info.path, tmp_path, edit))


def test_unexpected_member_is_refused(settings, tmp_path):
    info = create_backup(settings)

    def edit(work: Path) -> None:
        (work / "intrus.txt").write_text("x")

    with pytest.raises(BackupError, match="inattendus"):
        verify_backup(_rewrite(info.path, tmp_path, edit))


def test_truncated_archive_is_refused(settings, tmp_path):
    info = create_backup(settings)
    broken = tmp_path / "tronquee.tar.gz"
    broken.write_bytes(info.path.read_bytes()[:200])
    with pytest.raises(BackupError):
        verify_backup(broken)


def test_not_an_archive_is_refused(tmp_path):
    fake = tmp_path / "faux.tar.gz"
    fake.write_text("rien")
    with pytest.raises(BackupError):
        verify_backup(fake)


def test_archive_newer_than_code_is_refused(settings, tmp_path):
    info = create_backup(settings)

    def edit(work: Path) -> None:
        manifest = json.loads((work / "manifest.json").read_text())
        manifest["schema_revision"] = "9999"
        (work / "manifest.json").write_text(json.dumps(manifest))

    forged = _rewrite(info.path, tmp_path, edit)
    verify_backup(forged)  # l'archive est saine, mais trop récente
    with pytest.raises(BackupError, match="plus récente"):
        restore_backup(settings, forged)


def test_cli_restore_requires_confirmation(settings, monkeypatch):
    info = create_backup(settings)
    monkeypatch.setenv("BIBLYO_ENV", "test")
    monkeypatch.setenv("BIBLYO_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("BIBLYO_BACKUP_DIR", str(settings.backup_dir))
    runner = CliRunner()
    _change(settings)
    changed = _dump(settings.database_path)

    refused = runner.invoke(cli, ["backup", "restore", info.name])
    assert refused.exit_code == 1
    assert "--confirmer" in refused.stderr
    wrong = runner.invoke(cli, ["backup", "restore", info.name, "--confirmer", "autre"])
    assert wrong.exit_code == 1
    assert _dump(settings.database_path) == changed

    done = runner.invoke(cli, ["backup", "restore", info.name, "--confirmer", info.name])
    assert done.exit_code == 0, done.output
    assert _dump(settings.database_path) != changed


def test_cli_create_list_verify(settings, monkeypatch):
    monkeypatch.setenv("BIBLYO_ENV", "test")
    monkeypatch.setenv("BIBLYO_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("BIBLYO_BACKUP_DIR", str(settings.backup_dir))
    runner = CliRunner()
    assert "Aucune sauvegarde" in runner.invoke(cli, ["backup", "list"]).output
    assert runner.invoke(cli, ["backup", "create"]).exit_code == 0
    listed = runner.invoke(cli, ["backup", "list"]).output.strip()
    assert listed.startswith("biblyo-")
    assert "valide" in runner.invoke(cli, ["backup", "verify", listed]).output
    (settings.backup_dir / listed).write_bytes(b"abime")
    assert runner.invoke(cli, ["backup", "verify", listed]).exit_code == 1
    shutil.rmtree(settings.backup_dir)
    assert "Aucune sauvegarde" in runner.invoke(cli, ["backup", "list"]).output
