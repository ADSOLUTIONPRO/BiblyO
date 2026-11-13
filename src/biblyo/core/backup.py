# SPDX-License-Identifier: EUPL-1.2
"""Sauvegardes : une archive unique, cohérente et vérifiable (05-MIGRATIONS-SAUVEGARDES.md, § 2)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.util.exc import CommandError

from biblyo import __version__
from biblyo.config import Settings
from biblyo.core.clock import now, to_iso
from biblyo.core.db import readonly_uri
from biblyo.core.schema_state import read_revision, script_directory

DATABASE_MEMBER = "biblyo.sqlite"
MANIFEST_MEMBER = "manifest.json"
MEDIA_DIR = "media"
FORMAT_VERSION = 1
SIDE_FILES = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    """Sauvegarde impossible ou invalide : le message s'adresse à l'administrateur."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    manifest: dict[str, Any]

    @property
    def name(self) -> str:
        return self.path.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_integrity(path: Path) -> list[str]:
    """Anomalies d'une base : vide si `integrity_check` et `foreign_key_check` sont irréprochables."""
    problems: list[str] = []
    connection = sqlite3.connect(readonly_uri(path), uri=True)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in rows] != ["ok"]:
            problems.extend(f"integrity_check : {r[0]}" for r in rows)
        problems.extend(
            f"foreign_key_check : table {r[0]}, ligne {r[1]}, référence {r[2]}"
            for r in connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    except sqlite3.Error as exc:
        problems.append(f"lecture impossible : {exc}")
    finally:
        connection.close()
    return problems


def _table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(readonly_uri(path), uri=True)
    try:
        names = [
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}  # noqa: S608
    finally:
        connection.close()


def _installation_id(path: Path) -> str:
    connection = sqlite3.connect(readonly_uri(path), uri=True)
    try:
        row = connection.execute("SELECT value FROM settings WHERE key = 'installation.uuid'").fetchone()
        return str(json.loads(row[0])) if row else "inconnue"
    except (sqlite3.Error, ValueError):
        return "inconnue"
    finally:
        connection.close()


def _free_path(directory: Path, stem: str, suffix: str = "") -> Path:
    """Chemin encore libre : une sauvegarde ou un état conservé n'est jamais écrasé."""
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def create_backup(settings: Settings, motif: str = "manual") -> BackupInfo:
    """Copie cohérente de la base (`VACUUM INTO`), médias et manifeste. Seule une archive vérifiée est publiée."""
    source = settings.database_path
    if not source.exists():
        raise BackupError("Aucune base de données à sauvegarder.")
    try:
        return _write_backup(settings, source, motif)
    except OSError as exc:
        raise BackupError(f"Écriture de la sauvegarde impossible : {exc}") from exc


def _write_backup(settings: Settings, source: Path, motif: str) -> BackupInfo:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    moment = now()
    installation = _installation_id(source)
    stem = f"biblyo-{installation[:8]}-{moment:%Y%m%d-%H%M%S}-{motif}"
    with tempfile.TemporaryDirectory(dir=settings.backup_dir, prefix=".sauvegarde-") as workdir:
        work = Path(workdir)
        copy = work / DATABASE_MEMBER
        connection = sqlite3.connect(readonly_uri(source), uri=True)
        try:
            connection.execute("VACUUM INTO ?", (str(copy),))
        except sqlite3.Error as exc:
            raise BackupError(f"Copie de la base impossible : {exc}") from exc
        finally:
            connection.close()
        problems = check_integrity(copy)
        if problems:
            raise BackupError("La base copiée présente des anomalies : " + " ; ".join(problems[:5]))

        revision, _ = read_revision(copy)
        files = {DATABASE_MEMBER: {"sha256": _sha256(copy), "bytes": copy.stat().st_size}}
        media_root = settings.data_dir / MEDIA_DIR
        media_files = sorted(p for p in media_root.rglob("*") if p.is_file()) if media_root.is_dir() else []
        for media in media_files:
            relative = f"{MEDIA_DIR}/{media.relative_to(media_root).as_posix()}"
            files[relative] = {"sha256": _sha256(media), "bytes": media.stat().st_size}
        manifest = {
            "format": FORMAT_VERSION,
            "app_version": __version__,
            "schema_revision": revision,
            "installation": installation,
            "created_at": to_iso(moment),
            "motif": motif,
            "files": files,
            "counts": _table_counts(copy),
        }
        temporary = work / "archive.tar.gz"
        with tarfile.open(temporary, "w:gz") as archive:
            payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_MEMBER)
            info.size = len(payload)
            info.mtime = int(moment.timestamp())
            archive.addfile(info, io.BytesIO(payload))
            archive.add(copy, arcname=DATABASE_MEMBER, recursive=False)
            for media in media_files:
                archive.add(media, arcname=f"{MEDIA_DIR}/{media.relative_to(media_root).as_posix()}", recursive=False)
        verify_backup(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        final = _free_path(settings.backup_dir, stem, ".tar.gz")
        os.replace(temporary, final)
    return BackupInfo(final, manifest)


def _read_manifest(archive: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = archive.getmember(MANIFEST_MEMBER)
        handle = archive.extractfile(member)
        if handle is None:
            raise KeyError(MANIFEST_MEMBER)
        manifest = json.loads(handle.read().decode("utf-8"))
    except (KeyError, ValueError) as exc:
        raise BackupError("Manifeste absent ou illisible.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        raise BackupError("Format de sauvegarde inconnu.")
    return manifest


def _extract(archive_path: Path, target: Path) -> dict[str, Any]:
    """Extrait et contrôle l'archive : seuls les fichiers annoncés par le manifeste sont écrits."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            manifest = _read_manifest(archive)
            files: dict[str, dict[str, Any]] = manifest.get("files", {})
            if DATABASE_MEMBER not in files:
                raise BackupError("L’archive ne contient pas de base de données.")
            names = {m.name for m in archive.getmembers()}
            unexpected = names - set(files) - {MANIFEST_MEMBER}
            if unexpected:
                raise BackupError(f"Fichiers inattendus dans l’archive : {sorted(unexpected)[:3]}")
            for name, expected in files.items():
                if name.startswith("/") or ".." in Path(name).parts:
                    raise BackupError(f"Chemin refusé dans l’archive : {name}")
                if name not in names:
                    raise BackupError(f"Fichier absent de l’archive : {name}")
                member = archive.getmember(name)
                source = archive.extractfile(member) if member.isfile() else None
                if source is None:
                    raise BackupError(f"Entrée d’archive invalide : {name}")
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                if _sha256(destination) != expected.get("sha256"):
                    raise BackupError(f"Empreinte incorrecte : {name}")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(f"Archive illisible : {exc}") from exc
    problems = check_integrity(target / DATABASE_MEMBER)
    if problems:
        raise BackupError("La base de l’archive présente des anomalies : " + " ; ".join(problems[:5]))
    return manifest


def verify_backup(archive_path: Path) -> dict[str, Any]:
    """Contrôle complet d'une archive : manifeste, empreintes, intégrité de la base. Rend le manifeste."""
    if not archive_path.is_file():
        raise BackupError(f"Archive introuvable : {archive_path}")
    with tempfile.TemporaryDirectory(prefix="biblyo-verification-") as workdir:
        return _extract(archive_path, Path(workdir))


def list_backups(settings: Settings) -> list[Path]:
    if not settings.backup_dir.is_dir():
        return []
    return sorted(settings.backup_dir.glob("biblyo-*.tar.gz"), reverse=True)


def resolve_archive(settings: Settings, name: str) -> Path:
    candidate = Path(name)
    return candidate if candidate.is_absolute() or candidate.exists() else settings.backup_dir / name


def restore_backup(settings: Settings, archive_path: Path) -> Path | None:
    """Met l'archive en place. **Rien n'est détruit** : l'état courant est déplacé dans un dossier daté.

    Rend ce dossier (aucun s'il n'y avait rien à conserver). L'appelant tient le verrou de migration et a
    recueilli la confirmation.
    """
    manifest = verify_backup(archive_path)
    revision = manifest.get("schema_revision")
    if not isinstance(revision, str) or not revision:
        raise BackupError("La sauvegarde n’indique aucune révision de schéma.")
    try:
        script_directory().get_revision(revision)
    except CommandError as exc:
        raise BackupError(f"Cette sauvegarde (révision {revision}) est plus récente que l’application.") from exc

    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{now():%Y%m%d-%H%M%S}"
    with tempfile.TemporaryDirectory(dir=data_dir, prefix=".restauration-") as workdir:
        work = Path(workdir)
        _extract(archive_path, work)

        kept = _free_path(data_dir, f"avant-restauration-{stamp}")
        moved: list[tuple[Path, Path]] = []
        current = settings.database_path
        candidates = [current, *(current.with_name(current.name + side) for side in SIDE_FILES)]
        candidates.append(data_dir / MEDIA_DIR)
        try:
            for path in candidates:
                if path.exists():
                    kept.mkdir(exist_ok=True)
                    target = kept / path.name
                    os.replace(path, target)
                    moved.append((target, path))
            os.replace(work / DATABASE_MEMBER, current)
            if (work / MEDIA_DIR).exists():
                os.replace(work / MEDIA_DIR, data_dir / MEDIA_DIR)
        except OSError as exc:
            for target, origin in reversed(moved):
                os.replace(target, origin)
            raise BackupError(f"Mise en place impossible, état d’origine rétabli : {exc}") from exc
    problems = check_integrity(current)
    if problems:
        raise BackupError("La base restaurée présente des anomalies : " + " ; ".join(problems[:5]))
    settings.migration_failure_path.unlink(missing_ok=True)
    return kept if moved else None
