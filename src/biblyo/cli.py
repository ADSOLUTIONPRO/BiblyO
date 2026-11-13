# SPDX-License-Identifier: EUPL-1.2
"""Commande `biblyo` : serveur, base de données, sauvegardes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from biblyo.config import Settings

if TYPE_CHECKING:
    from biblyo.core.db import Database

cli = typer.Typer(help="biblyO : gestion de bibliothèque.", no_args_is_help=True, add_completion=False)
db_cli = typer.Typer(help="Base de données : état, mise à niveau, contrôle.", no_args_is_help=True)
backup_cli = typer.Typer(help="Sauvegardes : création, vérification, restauration.", no_args_is_help=True)
user_cli = typer.Typer(help="Comptes : création, mot de passe perdu.", no_args_is_help=True)
install_cli = typer.Typer(help="Première installation.", no_args_is_help=True)
search_cli = typer.Typer(help="Recherche plein texte du catalogue.", no_args_is_help=True)
media_cli = typer.Typer(help="Images : nettoyage des fichiers devenus inutiles.", no_args_is_help=True)
import_cli = typer.Typer(help="Import : simulation, recette aller-retour, application.", no_args_is_help=True)
cli.add_typer(db_cli, name="db")
cli.add_typer(backup_cli, name="backup")
cli.add_typer(user_cli, name="user")
cli.add_typer(install_cli, name="install")
cli.add_typer(search_cli, name="search")
cli.add_typer(media_cli, name="medias")
cli.add_typer(import_cli, name="import")


def _settings() -> Settings:
    return Settings()


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


@cli.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Lance le serveur web. Ne modifie jamais le schéma de la base."""
    import uvicorn

    uvicorn.run("biblyo.app:create_app", factory=True, host=host, port=port, proxy_headers=False, server_header=False)


@db_cli.command("status")
def db_status() -> None:
    """Affiche l'état de la base, sans rien modifier. Code de sortie 0 seulement si elle est à jour."""
    from biblyo.core.backup import check_integrity
    from biblyo.core.migrate import failure_summary, pending_revisions
    from biblyo.core.schema_state import SchemaStatus, read_state

    settings = _settings()
    state = read_state(settings)
    typer.echo(f"État : {state.message}")
    typer.echo(f"Base : {settings.database_path}")
    typer.echo(f"Révision courante : {state.current or 'aucune'}")
    typer.echo(f"Révision attendue : {state.head}")
    if state.detail:
        typer.echo(f"Détail : {state.detail}")
    summary = failure_summary(settings)
    if summary:
        typer.echo(f"Dernier échec : {summary}")
    if state.status in (SchemaStatus.ABSENT, SchemaStatus.BEHIND):
        typer.echo("Révisions à appliquer : " + ", ".join(pending_revisions(state.current)))
    if settings.database_path.exists() and state.status is not SchemaStatus.INCOMPATIBLE:
        problems = check_integrity(settings.database_path)
        typer.echo("Intégrité : " + ("correcte" if not problems else " ; ".join(problems[:5])))
    if not state.ok:
        raise typer.Exit(2)


@db_cli.command("upgrade")
def db_upgrade(
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche ce qui serait fait, sans rien modifier."),
) -> None:
    """Sauvegarde, migre, puis vérifie la version et l'intégrité. N'écrase jamais la base sur un échec."""
    from biblyo.core.migrate import MigrationLocked, UpgradeFailed, UpgradeRefused, upgrade

    try:
        report = upgrade(_settings(), dry_run=dry_run)
    except MigrationLocked as exc:
        raise _fail(str(exc)) from exc
    except UpgradeRefused as exc:
        raise _fail(f"Mise à niveau refusée : {exc}") from exc
    except UpgradeFailed as exc:
        typer.secho(exc.message, fg=typer.colors.RED, err=True)
        for line in [*exc.details, "", *exc.procedure]:
            typer.echo(line, err=True)
        raise typer.Exit(1) from exc
    if dry_run:
        typer.echo(f"Révision courante : {report.previous or 'aucune'}")
        typer.echo("À appliquer : " + (", ".join(report.applied) or "rien"))
        return
    if report.backup:
        typer.echo(f"Sauvegarde vérifiée : {report.backup.path}")
    typer.echo(f"Révisions appliquées : {', '.join(report.applied) or 'aucune'}")
    typer.echo(f"Base à jour, révision {report.current}. Intégrité et schéma contrôlés ({report.seconds:.1f} s).")


@db_cli.command("check")
def db_check() -> None:
    """Contrôle l'intégrité et les clés étrangères ; compare aussi le schéma si la base est à la tête."""
    from biblyo.core.backup import check_integrity
    from biblyo.core.migrate import verify_schema
    from biblyo.core.schema_state import SchemaStatus, read_state

    settings = _settings()
    state = read_state(settings, ignore_failure=True)
    if state.status in (SchemaStatus.ABSENT, SchemaStatus.INCOMPATIBLE):
        raise _fail(state.message)
    at_head = state.status is SchemaStatus.OK
    problems = verify_schema(settings) if at_head else check_integrity(settings.database_path)
    if at_head and not problems:
        problems = _search_problems(settings)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        raise typer.Exit(1)
    if at_head:
        typer.echo("Base intègre, schéma conforme.")
    else:
        typer.echo(f"Base intègre. {state.message} Révision {state.current}, attendue {state.head}.")


def _search_problems(settings: Settings) -> list[str]:
    from biblyo.core.db import Database
    from biblyo.modules.catalogue import search

    database = Database(settings.database_path)
    try:
        with database.session() as session:
            return search.inconsistencies(session)
    finally:
        database.dispose()


@search_cli.command("reindex")
def search_reindex() -> None:
    """Reconstruit l'index de recherche à partir du catalogue, en une seule transaction."""
    from biblyo.core.db import Database
    from biblyo.core.migrate import MigrationLocked, migration_lock
    from biblyo.core.schema_state import read_state
    from biblyo.modules.catalogue import search

    settings = _settings()
    state = read_state(settings)
    if not state.ok:
        raise _fail(state.message)
    database = Database(settings.database_path)
    try:
        with migration_lock(settings), database.session(write=True) as session:
            count = search.reindex_all(session)
    except MigrationLocked as exc:
        raise _fail(str(exc)) from exc
    finally:
        database.dispose()
    typer.echo(f"Index de recherche reconstruit : {count} œuvre(s).")


@import_cli.command("simuler")
def import_simulate(
    fichier: Path = typer.Argument(..., help="Classeur .xlsx ou fichier .csv à simuler."),
    complement: Path | None = typer.Option(None, "--complement", help="Version complémentaire, sans faire foi."),
    feuille: str = typer.Option("", "--feuille", help="Feuille à lire ; par défaut, la première qui a des lignes."),
    profil: str = typer.Option("", "--profil", help="Nom d'un profil de correspondance enregistré dans l'instance."),
    rapport: Path | None = typer.Option(None, "--rapport", help="Écrit le rapport complet (.xlsx) à cet endroit."),
    lignes: Path | None = typer.Option(None, "--lignes", help="Écrit les lignes (.csv) à cet endroit."),
) -> None:
    """Simule l'import d'un fichier : lit le catalogue sans jamais l'écrire, et n'écrit que les rapports demandés."""
    from biblyo.core.db import Database
    from biblyo.modules.imports import profile as profiles
    from biblyo.modules.imports import reader, report, simulate, store
    from biblyo.modules.imports.models import ImportProfile

    settings = _settings()
    try:
        main = reader.read(fichier.read_bytes(), fichier.name)
        other = reader.read(complement.read_bytes(), complement.name) if complement else None
    except (OSError, reader.ReadError) as exc:
        raise _fail(getattr(exc, "message", str(exc))) from exc
    sheet = main.sheet(feuille or store.data_sheet(main))
    if sheet is None:
        raise _fail(f"Feuille introuvable : {feuille}")
    existing = simulate.Existing()
    base = None
    if settings.database_path.exists():
        database = Database(settings.database_path)
        try:
            with database.session() as session:
                existing = simulate.snapshot(session)
                if profil:
                    saved = session.query(ImportProfile).filter_by(name=profil).one_or_none()
                    if saved is None:
                        raise _fail(f"Profil introuvable : {profil}")
                    base = profiles.Profile.from_json(saved.mapping, name=saved.name)
                session.rollback()
        finally:
            database.dispose()
    elif profil:
        raise _fail("Aucune base : un profil enregistré ne peut pas être lu.")
    profile = profiles.detect(sheet, base)
    other_sheet = other.sheet(store.data_sheet(other)) if other else None
    if other_sheet is not None:
        profile = profiles.detect(other_sheet, profile)
    result = simulate.simulate(
        main, sheet.name, profile, complement=other, complement_sheet=other_sheet.name if other_sheet else "",
        existing=existing,
    )  # fmt: skip
    t = result.totals
    typer.echo(f"Fichier : {main.filename} ({main.sha256[:16]}), feuille « {sheet.name} »")
    typer.echo(f"Lignes lues : {t['rows_read']} ; refusées : {t['rows_rejected']} ; exemplaires : {t['copies']}")
    typer.echo(f"Livres : {t['books']} ; œuvres : {t['works']} ; séries : {t['series']}")
    typer.echo("Répartition : " + ", ".join(f"{n} livre(s) à {k} exemplaire(s)" for k, n in t["distribution"].items()))
    typer.echo(f"Cotes partagées : {t['shared_call_numbers']} ; différences de version : {t['differences']}")
    for label, ok, detail in result.checks:
        typer.echo(f"{'conforme' if ok else 'ÉCART'} : {label}{' — ' + detail if detail else ''}")
    for blocker in result.blockers:
        typer.echo(f"Avant toute application : {blocker}")
    typer.echo(f"Empreinte du plan : {result.plan_sha256}")
    if rapport:
        rapport.write_bytes(report.to_xlsx(result))
        typer.echo(f"Rapport : {rapport}")
    if lignes:
        lignes.write_bytes(report.to_csv(result))
        typer.echo(f"Lignes : {lignes}")
    if any(not ok for _label, ok, _detail in result.checks):
        raise typer.Exit(1)


@import_cli.command("application")
def import_application(
    activer: bool = typer.Option(False, "--activer", help="Permet d'appliquer un import sur cette instance."),
    desactiver: bool = typer.Option(False, "--desactiver", help="Interdit d'appliquer un import sur cette instance."),
) -> None:
    """Affiche, active ou désactive l'application des imports sur cette instance (désactivée par défaut)."""
    from biblyo.core import audit
    from biblyo.core.settings_store import set_setting
    from biblyo.modules.imports import apply as importer

    if activer and desactiver:
        raise _fail("Choisissez --activer ou --desactiver, pas les deux.")
    settings = _settings()
    database = _ready_database(settings)
    try:
        with database.session(write=True) as session:
            if activer or desactiver:
                set_setting(session, importer.ENABLED_SETTING, bool(activer))
                audit.record(
                    session, "import.application_setting", "settings", None, details={"active": bool(activer)}
                )
            state = importer.enabled(session)
    finally:
        database.dispose()
    typer.echo(f"Application des imports sur cette instance : {'activée' if state else 'désactivée'}.")


@import_cli.command("recette")
def import_recette(
    fichier: Path = typer.Argument(..., help="Classeur .xlsx ou fichier .csv à essayer."),
    complement: Path | None = typer.Option(None, "--complement", help="Version complémentaire, sans faire foi."),
    sortie: Path = typer.Option(..., "--sortie", help="Dossier du rapport, hors du dépôt."),
    garder: bool = typer.Option(False, "--garder", help="Garde l'instance temporaire, pour l'examiner."),
) -> None:
    """Recette aller-retour dans une instance temporaire et indépendante : simulation, coupure, import, second
    import refusé, export au format d'origine comparé cellule par cellule, export complet, restauration."""
    import json as json_module
    from dataclasses import asdict

    from biblyo.modules.imports import recette

    try:
        main = fichier.read_bytes()
        other = complement.read_bytes() if complement else None
    except OSError as exc:
        raise _fail(str(exc)) from exc
    result = recette.run(main, fichier.name, other, complement.name if complement else "", keep=garder)
    sortie.mkdir(parents=True, exist_ok=True, mode=0o700)
    (sortie / "recette.md").write_text(recette.markdown(result), encoding="utf-8")
    (sortie / "recette.json").write_text(
        json_module.dumps(asdict(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    for step in result.steps:
        typer.echo(f"{'conforme' if step.ok else 'ÉCART'} : {step.label} — {step.detail}")
    typer.echo(f"Rapport : {sortie / 'recette.md'}" + (f" ; instance gardée : {result.workdir}" if garder else ""))
    if not result.ok:
        raise typer.Exit(1)


@media_cli.command("nettoyer")
def media_purge(
    appliquer: bool = typer.Option(False, "--appliquer", help="Supprime vraiment ; sans cette option, rien ne change."),
) -> None:
    """Fichiers d'aperçus jamais confirmés (plus de deux heures) et images que plus aucune édition ni aucun
    changement des 90 derniers jours ne désignent."""
    from biblyo.modules.catalogue import covers, service

    settings = _settings()
    database = _ready_database(settings)
    try:
        report = service.purge_media(database, settings.media_dir, apply=appliquer)
        if appliquer:  # en simulation, rien ne change, pas même une note d'aperçu expiré
            covers.expire(settings.media_dir, in_use=covers.in_use_checker(database))
    finally:
        database.dispose()
    verb = "supprimé(s)" if appliquer else "à supprimer"
    typer.echo(f"Aperçus non confirmés {verb} : {len(report.staged_files)} fichier(s).")
    typer.echo(f"Images inutilisées {verb} : {len(report.unused_media)}.")
    if not appliquer and (report.staged_files or report.unused_media):
        typer.echo("Simulation : relancez avec --appliquer pour supprimer.")


@media_cli.command("retirer")
def media_withdraw(
    empreinte: str = typer.Argument(..., help="Empreinte SHA-256 de l'image, telle que l'historique la montre."),
    motif: str = typer.Option(..., "--motif", help="Pourquoi l'image est retirée avant 90 jours (droits contestés…)."),
    appliquer: bool = typer.Option(False, "--appliquer", help="Supprime vraiment ; sans cette option, rien ne change."),
) -> None:
    """Retrait anticipé d'une image que plus aucune édition n'affiche : fichiers supprimés tout de suite, sans attendre
    les 90 jours de l'historique. Une couverture actuelle se supprime d'abord depuis sa page Couverture."""
    from biblyo.core.errors import BusinessError
    from biblyo.modules.catalogue import service

    settings = _settings()
    database = _ready_database(settings)
    try:
        report = service.withdraw_media(database, settings.media_dir, empreinte, reason=motif, apply=appliquer)
    except BusinessError as exc:
        raise _fail(exc.message) from exc
    finally:
        database.dispose()
    if appliquer:
        typer.echo(f"Image retirée : {report.sha256}.")
        typer.echo(f"{report.history} changement(s) d'historique ne la rétablissent plus.")
    else:
        typer.echo(f"Simulation : l'image {report.sha256} serait retirée.")
        typer.echo(f"{report.history} changement(s) d'historique la désignent.")
        typer.echo("Relancez avec --appliquer pour la retirer.")


@db_cli.command("downgrade")
def db_downgrade(revision: str) -> None:
    """Revient à une révision antérieure. Réservé au développement."""
    from alembic import command

    from biblyo.core.migrate import alembic_config, migration_lock

    settings = _settings()
    if settings.is_production:
        raise _fail("Refusé en production : restaurez une sauvegarde.")
    with migration_lock(settings):
        command.downgrade(alembic_config(settings), revision)
    typer.echo(f"Base ramenée à la révision {revision}.")


@backup_cli.command("create")
def backup_create() -> None:
    """Crée une sauvegarde vérifiée."""
    from biblyo.core.backup import BackupError, create_backup

    try:
        info = create_backup(_settings(), "manual")
    except BackupError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Sauvegarde vérifiée : {info.path}")


@backup_cli.command("list")
def backup_list() -> None:
    """Liste les sauvegardes, de la plus récente à la plus ancienne."""
    from biblyo.core.backup import list_backups

    archives = list_backups(_settings())
    if not archives:
        typer.echo("Aucune sauvegarde.")
    for archive in archives:
        typer.echo(archive.name)


@backup_cli.command("verify")
def backup_verify(archive: str) -> None:
    """Vérifie une archive : manifeste, empreintes, intégrité de la base."""
    from biblyo.core.backup import BackupError, resolve_archive, verify_backup

    try:
        manifest = verify_backup(resolve_archive(_settings(), archive))
    except BackupError as exc:
        raise _fail(f"Sauvegarde invalide : {exc}") from exc
    typer.echo(f"Sauvegarde valide : révision {manifest['schema_revision']}, créée le {manifest['created_at']}.")


@backup_cli.command("restore")
def backup_restore(
    archive: str,
    confirmer: str = typer.Option("", "--confirmer", help="Répétez le nom de l'archive pour confirmer."),
) -> None:
    """Restaure une archive. L'état courant n'est pas détruit : il est conservé dans un dossier daté."""
    from biblyo.core.backup import BackupError, resolve_archive, restore_backup
    from biblyo.core.migrate import MigrationLocked, migration_lock

    settings = _settings()
    path: Path = resolve_archive(settings, archive)
    if confirmer != path.name:
        raise _fail(f"Confirmation requise : ajoutez --confirmer {path.name}")
    try:
        with migration_lock(settings):
            kept = restore_backup(settings, path)
    except (BackupError, MigrationLocked) as exc:
        raise _fail(f"Restauration refusée : {exc}") from exc
    typer.echo(f"Sauvegarde restaurée : {path.name}")
    if kept:
        typer.echo(f"État précédent conservé dans : {kept}")


def _ready_database(settings: Settings) -> Database:
    """Base prête à l'emploi : aucune commande de compte n'écrit sur une base absente ou en retard."""
    from biblyo.core.db import Database
    from biblyo.core.schema_state import read_state

    state = read_state(settings)
    if not state.ok:
        raise _fail(f"{state.message} Consultez : biblyo db status")
    return Database(settings.database_path)


@install_cli.command("token")
def install_token() -> None:
    """Affiche un nouveau jeton d'installation. L'ancien cesse de valoir."""
    from biblyo.core.security import install

    token = install.regenerate_token(_ready_database(_settings()))
    if token is None:
        raise _fail("L’installation est déjà terminée : un compte existe.")
    typer.echo(f"Jeton d’installation : {token}")
    typer.echo("Page : /installation")


@user_cli.command("create")
def user_create(
    username: str,
    display_name: str = typer.Option("", "--nom", help="Nom affiché (par défaut, l'identifiant)."),
    role: str = typer.Option("admin", "--role", help="admin, librarian, volunteer ou viewer."),
) -> None:
    """Crée un compte. Le mot de passe est demandé sur le terminal, jamais passé en argument."""
    from biblyo.core.errors import FormErrors
    from biblyo.modules.admin import service

    database = _ready_database(_settings())
    password = typer.prompt("Mot de passe", hide_input=True, confirmation_prompt="Confirmez le mot de passe")
    try:
        service.create_account_with_password(database, username, display_name or username, role, password)
    except FormErrors as exc:
        raise _fail(" ".join(exc.fields.values())) from exc
    typer.echo(f"Compte créé : {username}")


@user_cli.command("reset-password")
def user_reset_password(username: str) -> None:
    """Pose un mot de passe provisoire, ferme les sessions et lève le verrouillage."""
    from biblyo.core.errors import BusinessError
    from biblyo.modules.admin import service

    database = _ready_database(_settings())
    try:
        temporary = service.reset_password(database, None, username)
    except BusinessError as exc:
        raise _fail(exc.message) from exc
    typer.echo(f"Mot de passe provisoire de {username} : {temporary}")
    typer.echo("Il devra être changé à la prochaine connexion.")


def main() -> None:
    from biblyo.core.logs import configure_logging

    os.umask(
        0o077
    )  # base, journaux et sauvegardes contiennent des données personnelles : réservés au compte de service
    configure_logging(_settings().log_level)
    cli()
