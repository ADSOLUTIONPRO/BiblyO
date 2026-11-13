# SPDX-License-Identifier: EUPL-1.2
"""Migrations de test : une copie du dossier des révisions, à laquelle un test ajoute les siennes."""

from __future__ import annotations

import shutil
import sqlite3
import textwrap
from pathlib import Path

import pytest

from biblyo.config import Settings
from biblyo.core import schema_state


class Revisions:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def add(self, revision: str, down: str, upgrade: str = "pass", downgrade: str = "pass") -> None:
        body = textwrap.indent(textwrap.dedent(upgrade).strip(), " " * 4)
        (self.directory / "versions" / f"{revision}_test.py").write_text(
            "from alembic import op\n"
            "import sqlalchemy as sa\n\n"
            f"revision = {revision!r}\n"
            f"down_revision = {down!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            f"def upgrade():\n{body}\n\n\n"
            f"def downgrade():\n    {downgrade}\n",
            encoding="utf-8",
        )
        schema_state._load_scripts.cache_clear()


@pytest.fixture
def revisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Revisions:
    target = tmp_path / "migrations"
    shutil.copytree(schema_state.MIGRATIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(schema_state, "MIGRATIONS_DIR", target)
    schema_state._load_scripts.cache_clear()
    yield Revisions(target)
    schema_state._load_scripts.cache_clear()


def fingerprint(path: Path) -> str:
    """Contenu logique complet d'une base (schéma et données)."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


@pytest.fixture
def seeded(settings: Settings) -> Settings:
    """Base à jour contenant une donnée reconnaissable."""
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("library.name", '"Bibliothèque d’essai"', "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    return settings
