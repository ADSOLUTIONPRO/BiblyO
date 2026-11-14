# SPDX-License-Identifier: EUPL-1.2
"""La première révision et le schéma obtenu."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from alembic import command

from biblyo.core import schema_state
from biblyo.core.db import APPLICATION_ID
from biblyo.core.migrate import alembic_config, schema_differences, upgrade
from biblyo.core.schema_state import SchemaStatus, read_state

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "alembic_version",
    "enrichment_jobs",
    "enrichment_job_items",
    "audit_log",
    "role_permissions",
    "roles",
    "sessions",
    "settings",
    "user_roles",
    "users",
    # Catalogue (révision 0002)
    "agents",
    "collections",
    "copies",
    "document_types",
    "edition_contributors",
    "edition_identifiers",
    "editions",
    "media_files",
    "publishers",
    "sections",
    "sequences",
    "series",
    "series_expected_volumes",
    "tags",
    "work_contributors",
    "work_series",
    "work_tags",
    "works",
    # Emplacements et codes retirés (révision 0006)
    "locations",
    "retired_codes",
    # Recherches enregistrées (révision 0004)
    "saved_searches",
    # Recherche (révision 0003) : la table FTS5 et ses tables internes
    "works_search",
    "works_search_config",
    "works_search_content",
    "works_search_data",
    "works_search_docsize",
    "works_search_idx",
    # Lecteurs et familles (révision 0007)
    "patrons",
    "patron_cards",
    "households",
    "household_members",
    "guardianships",
    "contacts",
    "patron_holds",
    "role_permission_changes",
    # Prêts et retours (révision 0008)
    "loan_rules",
    "loan_operations",
    "loans",
    "loan_events",
    "loan_reminders",
    # Enrichissement (révision 0009)
    "metadata_sources",
    "source_cache",
    "enrichment_searches",
    "enrichment_proposals",
    "enrichment_decisions",
    "edition_isbn_candidates",
    "field_provenance",
    "cover_changes",
    "import_profiles",
    "import_batches",
    "import_rows",
    "import_links",
    "import_proposals",
}


def _connect(settings) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)


def test_fresh_upgrade_reaches_head_with_provided_roles(bare_settings):
    report = upgrade(bare_settings)
    assert report.previous is None
    assert report.backup is None  # base neuve : rien à sauvegarder
    assert read_state(bare_settings).status is SchemaStatus.OK
    connection = _connect(bare_settings)
    roles = {r[0] for r in connection.execute("SELECT code FROM roles")}
    star = connection.execute(
        "SELECT permission FROM role_permissions JOIN roles ON roles.id = role_id WHERE code = 'admin'"
    ).fetchall()
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    uuid = connection.execute("SELECT value FROM settings WHERE key = 'installation.uuid'").fetchone()
    connection.close()
    assert roles == {"admin", "librarian", "volunteer", "viewer"}
    assert star == [("*",)]
    assert application_id == APPLICATION_ID
    assert uuid is not None


def test_real_schema_matches_models(settings):
    assert schema_differences(settings) == []


def test_tables_are_exactly_the_expected_ones(settings):
    connection = _connect(settings)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert tables == EXPECTED_TABLES


def test_business_tables_are_strict(settings):
    connection = _connect(settings)
    rows = connection.execute("SELECT name, strict FROM pragma_table_list WHERE schema = 'main'").fetchall()
    connection.close()
    ignored = {"alembic_version"} | {name for name in EXPECTED_TABLES if name.startswith("works_search")}
    strict = {name: flag for name, flag in rows if name in EXPECTED_TABLES - ignored}
    assert strict and all(strict.values()), strict


def test_downgrade_then_upgrade_is_reversible(settings):
    config = alembic_config(settings)
    command.downgrade(config, "base")
    connection = _connect(settings)
    left = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert left == {"alembic_version"}
    command.upgrade(config, "head")
    assert schema_differences(settings) == []


def _load_freezer():
    spec = importlib.util.spec_from_file_location("freeze_migrations", ROOT / "scripts" / "freeze_migrations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_revisions_are_frozen():
    assert _load_freezer().check() == []


def test_modified_or_unfrozen_revision_is_detected(tmp_path):
    freezer = _load_freezer()
    versions = tmp_path / "versions"
    versions.mkdir()
    revision = versions / "0001_essai.py"
    revision.write_text("revision = '0001'\n")
    ledger = tmp_path / "checksums.json"
    assert any("non figée" in p for p in freezer.check(versions, ledger))

    import json

    ledger.write_text(json.dumps(freezer.digests(versions)))
    assert freezer.check(versions, ledger) == []
    revision.write_text("revision = '0001'  # modifiée\n")
    assert any("modifiée" in p for p in freezer.check(versions, ledger))


def test_migrations_dir_is_inside_the_package():
    assert schema_state.MIGRATIONS_DIR.is_dir()
    assert (schema_state.MIGRATIONS_DIR / "env.py").is_file()
