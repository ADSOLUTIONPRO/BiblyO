# SPDX-License-Identifier: EUPL-1.2
"""Environnement Alembic. Lancé par `biblyo db upgrade`, jamais au démarrage de l'application."""

from __future__ import annotations

from alembic import context

from biblyo.config import Settings
from biblyo.core.db import make_engine
from biblyo.core.migrate import UNMODELED_TABLES, load_models
from biblyo.core.models_base import Base

load_models()
settings: Settings = context.config.attributes.get("settings") or Settings()


def _include_name(name: str | None, type_: str, _parents: dict[str, str | None]) -> bool:
    return not (type_ == "table" and name in UNMODELED_TABLES)


# Clés étrangères désactivées pendant la migration (reconstruction de tables en mode batch) :
# `foreign_key_check` est exigé juste après par `verify_schema`.
engine = make_engine(settings.database_path, foreign_keys=False, create=True)
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
            transactional_ddl=True,
            transaction_per_migration=True,
            compare_type=True,
            include_name=_include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
