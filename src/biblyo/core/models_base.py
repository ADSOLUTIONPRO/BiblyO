# SPDX-License-Identifier: EUPL-1.2
"""Base déclarative : contraintes nommées (indispensables au mode batch d'Alembic) et tables STRICT."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, MetaData
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Options de chaque table : voir docs/adr/0001-tables-strict.md
TABLE_OPTIONS: dict[str, Any] = {"sqlite_strict": True}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IntBool(TypeDecorator[bool]):
    """Booléen stocké en 0/1 : le type BOOLEAN n'existe pas dans une table STRICT."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: bool | None, dialect: Dialect) -> int | None:
        return None if value is None else int(bool(value))

    def process_result_value(self, value: int | None, dialect: Dialect) -> bool | None:
        return None if value is None else bool(value)
