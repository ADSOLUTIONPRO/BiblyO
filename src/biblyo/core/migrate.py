# SPDX-License-Identifier: EUPL-1.2
"""Mise à niveau explicite de la base : jamais au démarrage, jamais d'écrasement après un échec.

Déroulement (05-MIGRATIONS-SAUVEGARDES.md, § 1.3) : version actuelle, sauvegarde vérifiée, migration,
contrôle de la version et de l'intégrité. Sur un échec, la commande s'arrête, pose un marqueur qui maintient
l'application en maintenance et donne la procédure de récupération : la décision de restaurer reste humaine.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from biblyo.config import Settings
from biblyo.core import schema_state
from biblyo.core.backup import BackupError, BackupInfo, check_integrity, create_backup
from biblyo.core.clock import now_iso
from biblyo.core.db import make_engine, readonly_uri
from biblyo.core.models_base import Base
from biblyo.core.schema_state import SchemaStatus, read_failure, read_revision, read_state, script_directory

log = logging.getLogger("biblyo.migrate")

#: Tables gérées hors des modèles déclaratifs, ignorées par la comparaison de schéma.
UNMODELED_TABLES: frozenset[str] = frozenset(
    {
        "works_search",
        "works_search_config",
        "works_search_content",
        "works_search_data",
        "works_search_docsize",
        "works_search_idx",
    }
)
#: Objets invisibles pour la comparaison (index partiels, déclencheurs, vues, FTS5), dont la présence est exigée.
REQUIRED_OBJECTS: frozenset[str] = frozenset(
    {"works_search", "ux_loan_rules_scope", "ux_loans_active_copy", "trg_loans_copy_lendable", "copy_availability"}
)


class MigrationLocked(Exception):
    """Une autre opération sur la base est en cours."""


class UpgradeRefused(Exception):
    """L'état de la base interdit la mise à niveau ; rien n'a été modifié."""


class UpgradeFailed(Exception):
    """La mise à niveau a échoué ; `details` constate les faits et `procedure` indique comment récupérer."""

    def __init__(self, message: str, details: list[str], procedure: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.procedure = procedure


@dataclass
class UpgradeReport:
    previous: str | None
    current: str | None
    applied: list[str] = field(default_factory=list)
    backup: BackupInfo | None = None
    dry_run: bool = False
    seconds: float = 0.0


def load_models() -> None:
    """Importe les modèles de tous les modules pour remplir `Base.metadata`."""
    import biblyo.core.models  # noqa: F401
    from biblyo.modules.registry import all_modules

    all_modules()


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(schema_state.MIGRATIONS_DIR))
    config.attributes["settings"] = settings
    return config


@contextmanager
def migration_lock(settings: Settings) -> Iterator[None]:
    """Verrou exclusif entre commandes, libéré par le système si le processus s'arrête brutalement."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with settings.migration_lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MigrationLocked("Une autre opération sur la base est en cours.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pending_revisions(current: str | None) -> list[str]:
    """Révisions à appliquer, dans l'ordre d'application."""
    scripts = script_directory()
    revisions = [r.revision for r in scripts.iterate_revisions(scripts.get_current_head(), current or "base")]
    revisions.reverse()
    return revisions


def _describe_difference(difference: object) -> str:
    """Écart lisible pour l'administrateur ; le détail brut d'Alembic pour les cas moins fréquents."""
    if isinstance(difference, tuple) and difference and difference[0] in ("add_table", "remove_table"):
        label = "table manquante" if difference[0] == "add_table" else "table en trop"
        return f"{label} {difference[1].name}"
    return str(difference)


def _fk_signature(constraint: Any) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    columns = tuple(element.parent.name for element in constraint.elements)
    targets = tuple(element.target_fullname for element in constraint.elements)
    return constraint.parent.name, columns, targets


def _without_inline_foreign_keys(connection: Any, differences: list[Any]) -> list[Any]:
    """Clés étrangères ajoutées par `ALTER TABLE ADD COLUMN … REFERENCES` (révision 0006).

    SQLAlchemy ne relit ni le nom ni `ON DELETE` d'une clé déclarée dans la colonne : la comparaison voit une clé
    à retirer et une à ajouter. Elles ne s'annulent que si `PRAGMA foreign_key_list` confirme la même cible et la
    même action à la suppression : la vérification est faite, pas ignorée.
    """
    removed = {_fk_signature(d[1]): d for d in differences if d[0] == "remove_fk" and d[1].name is None}
    kept = list(differences)
    for difference in differences:
        if difference[0] != "add_fk":
            continue
        signature = _fk_signature(difference[1])
        twin = removed.get(signature)
        if twin is None or len(signature[1]) != 1:
            continue
        table, (column,), (target,) = signature
        rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list({table})").fetchall()
        expected = (difference[1].ondelete or "NO ACTION").upper()
        if any(row[3] == column and f"{row[2]}.{row[4]}" == target and row[6] == expected for row in rows):
            kept.remove(difference)
            kept.remove(twin)
    return kept


def schema_differences(settings: Settings) -> list[str]:
    """Différences entre le schéma réel et les modèles : vide si tout concorde."""
    load_models()
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "include_name": lambda name, type_, _parents: not (type_ == "table" and name in UNMODELED_TABLES),
                },
            )
            with warnings.catch_warnings():
                # SQLAlchemy ne relit pas les index sur expressions : leur présence passe par REQUIRED_OBJECTS
                warnings.filterwarnings("ignore", message="Skipped unsupported reflection of expression-based index")
                found = list(compare_metadata(context, Base.metadata))
            differences = _without_inline_foreign_keys(connection, found)
            return [_describe_difference(d) for d in differences]
    finally:
        engine.dispose()


def missing_objects(settings: Settings) -> list[str]:
    connection = sqlite3.connect(readonly_uri(settings.database_path), uri=True)
    try:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()
    return sorted(REQUIRED_OBJECTS - present)


def verify_schema(settings: Settings) -> list[str]:
    """Contrôle complet d'une base censée être à la tête : version, intégrité, clés étrangères, schéma."""
    problems = check_integrity(settings.database_path)
    current, _ = read_revision(settings.database_path)
    head = script_directory().get_current_head()
    if current != head:
        problems.append(f"révision obtenue {current}, attendue {head}")
    problems.extend(f"schéma : {difference}" for difference in schema_differences(settings))
    problems.extend(f"objet absent : {name}" for name in missing_objects(settings))
    return problems


def recovery_procedure(backup_name: str | None, settings: Settings) -> list[str]:
    """Procédure affichée à l'administrateur : `05` § 1.4. Un test exécute ses commandes telles quelles.

    Les lignes qui commencent par `biblyo ` sont des commandes, à copier sans les modifier.
    """
    docker = "Dans Docker, précédez chaque commande de « docker compose run --rm app ». Ne lancez jamais « down -v »."
    if backup_name is None:
        return [
            "Aucune donnée à protéger : la base était neuve.",
            docker,
            "1. Corrigez la cause, puis écartez le fichier incomplet du dossier de données :",
            f"   {settings.database_path}",
            "2. Recréez la base :",
            "biblyo db upgrade",
        ]
    return [
        "La base n’a pas été remplacée. Pour revenir à l’état d’avant la migration, sur décision de votre part :",
        docker,
        "1. Lisez le diagnostic :",
        "biblyo db status",
        "2. Arrêtez l’application (docker compose stop app).",
        "3. Vérifiez la sauvegarde :",
        f"biblyo backup verify {backup_name}",
        "4. Restaurez-la. L’état actuel n’est pas détruit, il est déplacé dans un dossier « avant-restauration » :",
        f"biblyo backup restore {backup_name} --confirmer {backup_name}",
        "5. Contrôlez le résultat :",
        "biblyo db status",
        "biblyo db check",
        "6. Redémarrez l’application (docker compose start app).",
        "   Elle reste en maintenance tant que la mise à jour n’a pas abouti.",
    ]


def _failed(
    settings: Settings, *, stage: str, error: str, previous: str | None, backup: BackupInfo | None
) -> UpgradeFailed:
    """Pose le marqueur et décrit ce qui est constaté sur la base. Ne la touche pas."""
    try:
        observed, _ = read_revision(settings.database_path)
        remaining = pending_revisions(observed)
    except (sqlite3.Error, OSError):
        observed, remaining = None, []
    faulty = remaining[0] if stage == "migration" and remaining else None
    settings.migration_failure_path.write_text(
        json.dumps(
            {
                "at": now_iso(),
                "stage": stage,
                "error": error[:500],
                "revision": faulty,
                "previous_revision": previous,
                "observed_revision": observed,
                "backup": backup.name if backup else None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    details = [
        f"Étape en échec : {stage}",
        f"Révision en cause : {faulty or 'sans objet'}",
        f"Erreur : {error}",
        f"Révision constatée sur la base : {observed or 'aucune'} (avant la commande : {previous or 'aucune'})",
        f"Sauvegarde vérifiée : {backup.path if backup else 'aucune, la base était neuve'}",
    ]
    return UpgradeFailed(
        f"La mise à jour a échoué ({stage}).",
        details,
        recovery_procedure(backup.name if backup else None, settings),
    )


def _plan(settings: Settings, *, dry_run: bool) -> tuple[schema_state.SchemaState, UpgradeReport]:
    """État de la base et révisions à appliquer, ou refus. Ne fait que lire."""
    state = read_state(settings, ignore_failure=True)
    if state.status in (SchemaStatus.AHEAD, SchemaStatus.INCOMPATIBLE):
        raise UpgradeRefused(f"{state.message} {state.detail}".strip())

    failure = read_failure(settings)
    if failure and state.status is not SchemaStatus.ABSENT:
        # Base potentiellement modifiée : on ne repart pas dessus sans décision humaine.
        lines = recovery_procedure(failure.get("backup"), settings)
        raise UpgradeRefused("\n".join(["Une mise à jour précédente a échoué.", *lines]))

    pending = [] if state.status is SchemaStatus.OK else pending_revisions(state.current)
    return state, UpgradeReport(previous=state.current, current=state.current, applied=pending, dry_run=dry_run)


def upgrade(settings: Settings, *, dry_run: bool = False) -> UpgradeReport:
    """Met la base à niveau jusqu'à la dernière révision. Voir la documentation du module.

    L'essai à blanc ne prend pas le verrou : il n'écrit rien, pas même le fichier du verrou.
    """
    if dry_run:
        return _plan(settings, dry_run=True)[1]

    started = time.monotonic()
    with migration_lock(settings):
        state, report = _plan(settings, dry_run=False)
        previous, pending = report.previous, report.applied

        backup: BackupInfo | None = None
        if state.status is not SchemaStatus.ABSENT:
            problems = check_integrity(settings.database_path)
            if problems:
                raise UpgradeRefused("La base présente déjà des anomalies : " + " ; ".join(problems[:5]))
            if pending:
                try:
                    backup = create_backup(settings, "pre-migration")
                except BackupError as exc:
                    raise UpgradeRefused(f"Sauvegarde impossible, base inchangée. {exc}") from exc
                report.backup = backup

        settings.migration_failure_path.unlink(missing_ok=True)
        if pending:
            try:
                command.upgrade(alembic_config(settings), "head")
            except Exception as exc:
                log.exception("Échec de la migration")
                raise _failed(settings, stage="migration", error=str(exc), previous=previous, backup=backup) from exc

        problems = verify_schema(settings)
        if problems:
            message = " ; ".join(problems[:5])
            log.error("Vérification après migration en échec : %s", message)
            raise _failed(settings, stage="vérification", error=message, previous=previous, backup=backup)

        report.current, _ = read_revision(settings.database_path)
        report.seconds = time.monotonic() - started
        log.info(
            "Base à jour : %s vers %s, %s",
            previous or "aucune",
            report.current,
            backup.name if backup else "sans sauvegarde",
        )
        return report


def failure_summary(settings: Settings) -> str | None:
    failure = read_failure(settings)
    if failure is None:
        return None
    return f"{failure.get('at', '?')}, {failure.get('stage', '?')} : {failure.get('error', '')}"
