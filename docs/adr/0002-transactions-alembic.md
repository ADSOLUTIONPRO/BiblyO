# ADR 0002 — Comportement transactionnel d'Alembic sur SQLite

**Statut** : adoptée pendant T2 (lot L0), le 6 novembre 2026.

## Contexte

Le pilote `sqlite3` de Python gère les transactions à sa façon, et Alembic considère par défaut que SQLite n'a pas de DDL
transactionnel. Une migration pourrait donc échouer à mi-chemin en laissant une base partiellement modifiée.

## Décision

- Le pilote est mis en mode manuel (`isolation_level = None`) et `BEGIN` est émis explicitement (`core/db.py`).
- Alembic reçoit `transactional_ddl=True` et `transaction_per_migration=True` (`migrations/env.py`).
- Les clés étrangères sont désactivées sur la connexion de migration, car la reconstruction d'une table en mode *batch* les
  violerait temporairement. `PRAGMA foreign_key_check` est exigé juste après, par `verify_schema`.

## Constat, vérifié par des tests

- Une révision qui crée une table puis lève une exception ne laisse aucune trace : le schéma et les données sont
  identiques à l'état d'avant (`tests/migrations/test_upgrade.py::test_failing_revision_stops_without_overwriting`).
- Les révisions déjà validées d'une même commande le restent. Une commande qui échoue en troisième révision laisse donc la
  base à la deuxième.

## Limites : ce que la transaction ne couvre pas

- `PRAGMA journal_mode`, et `PRAGMA foreign_keys` dans une transaction, sont sans effet ou ne s'annulent pas.
- `VACUUM` ne peut pas s'exécuter dans une transaction.
- Une écriture hors de la base (fichier, média) n'est pas annulée.

Une révision qui y recourt doit le dire dans son en-tête. Pour ces cas, et pour une panne franche du processus ou du
disque, la voie de retour reste **la sauvegarde vérifiée prise avant la migration** : le code ne s'appuie jamais sur
l'annulation pour conclure qu'une base est saine. Le marqueur d'échec est posé dans tous les cas, et l'application reste
en maintenance jusqu'à une décision humaine.
