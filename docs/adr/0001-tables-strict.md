# ADR 0001 — Tables SQLite `STRICT`

**Statut** : adoptée pendant T2 (lot L0), le 6 novembre 2026.

## Contexte

`11-PLAN-L0.md` demandait de trancher : les tables `STRICT` sont-elles utilisables avec la version de SQLAlchemy
retenue ? SQLite accepte normalement n'importe quelle valeur dans n'importe quelle colonne ; une table `STRICT` refuse
une valeur du mauvais type.

## Constat

- SQLAlchemy 2.1.8 et Alembic 1.20.1 émettent `STRICT` avec l'option `sqlite_strict=True`.
- SQLite 3.37 est le minimum requis. La version est contrôlée au démarrage (`MINIMUM_SQLITE`) : en dessous, l'application
  reste en maintenance.
- Une table `STRICT` n'accepte que `INTEGER`, `REAL`, `TEXT`, `BLOB` et `ANY`. Les types `VARCHAR` et `BOOLEAN` de
  SQLAlchemy y sont refusés à la création.

## Décision

Toutes les tables métier sont `STRICT`, déclarées avec `TABLE_OPTIONS` (`core/models_base.py`).

- Colonnes en `Text` et `Integer`. Les limites de longueur et les énumérations passent par des contraintes `CHECK`.
- Les booléens sont stockés en 0/1 par le type `IntBool`, avec un `CHECK (… IN (0, 1))`.
- Les dates sont des `TEXT` au format ISO 8601 UTC, écrites par `core/clock.py`.
- `alembic_version`, créée par Alembic, reste une table ordinaire.

## Conséquences

- Un test vérifie que chaque table métier est `STRICT` et qu'une valeur du mauvais type est refusée.
- L'autogénération d'Alembic ne compare pas les `CHECK` : une révision qui en ajoute est relue à la main (`05` § 1.1).
