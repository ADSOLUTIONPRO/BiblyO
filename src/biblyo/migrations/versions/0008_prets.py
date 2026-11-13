# SPDX-License-Identifier: EUPL-1.2
"""Prêts, opérations, événements, rappels, règles, et droits explicites d'emprunt (lot L4).

Révision 0008. Elle **crée** cinq tables et une vue, et ajoute deux colonnes fausses par défaut : personne n'emprunte
seul ni pour une structure sans décision explicite. Aucune donnée existante n'est réécrite.

Invariant central : au plus un prêt en cours par exemplaire, garanti par l'index partiel `ux_loans_active_copy`.

Rôles fournis (plan de L4, C6 et D16) : le Bibliothécaire reçoit `circulation.settings` et `circulation.override_limit`,
**seulement s'il est resté tel qu'après la révision 0007**. Un rôle modifié ou créé localement n'est jamais touché. Le
rôle Administrateur a déjà tout. Chaque ajustement est inscrit dans `role_permission_changes`.

Codes : une carte égale à un code d'exemplaire ou lisible comme un numéro de lecteur est **signalée, jamais corrigée**
(C4). Le comptoir demandera alors un choix explicite. Le signalement ne donne que des nombres, aucun numéro de carte.

Descente : **refusée dès qu'un prêt, une opération ou un rappel existe, ou qu'un droit nouveau a été accordé**. La voie
de retour est alors la sauvegarde prise avant la migration. Sinon, elle défait exactement ce qui a été tracé.
"""

from __future__ import annotations

import json
import logging
import re

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

log = logging.getLogger("biblyo.migrate")

# Contenu du rôle après la révision 0007, recopié ici pour ne dépendre d'aucun code courant.
LIBRARIAN_0007 = frozenset(
    {
        "catalogue.view", "catalogue.edit", "catalogue.archive", "holdings.edit", "holdings.withdraw",
        "locations.manage", "patrons.view", "patrons.view_personal", "patrons.edit", "patrons.archive",
        "patrons.register", "patrons.export", "circulation.lend", "circulation.renew", "circulation.override",
        "circulation.history", "enrichment.search", "enrichment.review", "enrichment.batch", "imports.simulate",
        "imports.apply", "inventory.run", "labels.print", "admin.settings", "admin.identity", "admin.sources",
        "admin.audit",
    }
)  # fmt: skip
ADDED = ("circulation.settings", "circulation.override_limit")
SETTINGS: dict[str, object] = {
    "circulation.limit_mode": "avertir",
    "circulation.overdue_block": None,  # seuil de retards bloquant : désactivé
    "circulation.closed_weekday": None,  # jour de fermeture hebdomadaire : aucun
    "circulation.retention_months": 12,  # hypothèse de travail, sans aucun effet (D13)
}
#: portée, durée, durée de renouvellement, renouvellements, plafond (D3 bis)
DEFAULT_RULES = (
    ("adult", 28, 28, 1, 5),
    ("minor", 21, 21, 1, 3),
    ("structure", 42, 42, 1, 15),
)
#: Même forme que le numéro de lecteur : recopiée pour que la révision reste figée.
_PATRON_NUMBER = re.compile(r"P-?0*\d{1,9}")
_ACTIVE = "returned_at IS NULL AND cancelled_at IS NULL"
STATEMENTS = (
    "ALTER TABLE guardianships ADD COLUMN can_borrow_alone INTEGER NOT NULL DEFAULT 0"
    " CONSTRAINT ck_guardianships_can_borrow_alone CHECK (can_borrow_alone IN (0, 1))",
    "ALTER TABLE household_members ADD COLUMN can_borrow_for_structure INTEGER NOT NULL DEFAULT 0"
    " CONSTRAINT ck_household_members_can_borrow_for_structure CHECK (can_borrow_for_structure IN (0, 1))",
    """CREATE TABLE loan_rules (
        id INTEGER NOT NULL,
        document_type_id INTEGER,
        patron_category TEXT,
        loan_days INTEGER NOT NULL,
        renewal_days INTEGER NOT NULL,
        max_renewals INTEGER NOT NULL DEFAULT 1,
        max_active_loans INTEGER,
        CONSTRAINT pk_loan_rules PRIMARY KEY (id),
        CONSTRAINT fk_loan_rules_document_type_id_document_types
            FOREIGN KEY (document_type_id) REFERENCES document_types (id) ON DELETE CASCADE,
        CONSTRAINT ck_loan_rules_patron_category_values CHECK (patron_category IN ('adult','minor','structure')),
        CONSTRAINT ck_loan_rules_loan_days_range CHECK (loan_days BETWEEN 1 AND 365),
        CONSTRAINT ck_loan_rules_renewal_days_range CHECK (renewal_days BETWEEN 1 AND 365),
        CONSTRAINT ck_loan_rules_max_renewals_positive CHECK (max_renewals >= 0),
        CONSTRAINT ck_loan_rules_max_active_loans_positive CHECK (max_active_loans >= 1)
    ) STRICT""",
    "CREATE UNIQUE INDEX ux_loan_rules_scope ON loan_rules (COALESCE(document_type_id, 0), COALESCE(patron_category, ''))",
    """CREATE TABLE loan_operations (
        id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        cancelled_at TEXT,
        cancelled_by INTEGER,
        CONSTRAINT pk_loan_operations PRIMARY KEY (id),
        CONSTRAINT fk_loan_operations_user_id_users FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_loan_operations_cancelled_by_users
            FOREIGN KEY (cancelled_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_loan_operations_kind_values CHECK (kind IN ('lend','return','renew'))
    ) STRICT""",
    """CREATE TABLE loans (
        id INTEGER NOT NULL,
        operation_id INTEGER NOT NULL,
        copy_id INTEGER NOT NULL,
        patron_id INTEGER NOT NULL,
        responsible_id INTEGER,
        household_id INTEGER,
        borrow_mode TEXT NOT NULL,
        loaned_at TEXT NOT NULL,
        due_on TEXT NOT NULL,
        returned_at TEXT,
        renewals INTEGER NOT NULL DEFAULT 0,
        loaned_by INTEGER,
        returned_by INTEGER,
        cancelled_at TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT pk_loans PRIMARY KEY (id),
        CONSTRAINT fk_loans_operation_id_loan_operations
            FOREIGN KEY (operation_id) REFERENCES loan_operations (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loans_copy_id_copies FOREIGN KEY (copy_id) REFERENCES copies (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loans_patron_id_patrons FOREIGN KEY (patron_id) REFERENCES patrons (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loans_responsible_id_patrons
            FOREIGN KEY (responsible_id) REFERENCES patrons (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loans_household_id_households
            FOREIGN KEY (household_id) REFERENCES households (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loans_loaned_by_users FOREIGN KEY (loaned_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_loans_returned_by_users FOREIGN KEY (returned_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_loans_borrow_mode_values CHECK (borrow_mode IN ('self','with_adult','alone','structure')),
        CONSTRAINT ck_loans_renewals_positive CHECK (renewals >= 0),
        CONSTRAINT ck_loans_version_positive CHECK (version >= 1),
        CONSTRAINT ck_loans_return_after_loan CHECK (returned_at IS NULL OR returned_at >= loaned_at),
        CONSTRAINT ck_loans_responsible_distinct CHECK (responsible_id IS NULL OR responsible_id <> patron_id),
        CONSTRAINT ck_loans_mode_active CHECK (returned_at IS NOT NULL OR cancelled_at IS NOT NULL
            OR (borrow_mode = 'self' AND responsible_id IS NULL AND household_id IS NULL)
            OR (borrow_mode IN ('with_adult','alone') AND responsible_id IS NOT NULL)
            OR (borrow_mode = 'structure' AND household_id IS NOT NULL))
    ) STRICT""",
    f"CREATE UNIQUE INDEX ux_loans_active_copy ON loans (copy_id) WHERE {_ACTIVE}",
    f"CREATE INDEX ix_loans_patron_active ON loans (patron_id) WHERE {_ACTIVE}",
    f"CREATE INDEX ix_loans_due_active ON loans (due_on) WHERE {_ACTIVE}",
    "CREATE INDEX ix_loans_responsible ON loans (responsible_id)",
    "CREATE INDEX ix_loans_copy_history ON loans (copy_id, loaned_at)",
    "CREATE INDEX ix_loans_operation ON loans (operation_id)",
    """CREATE TRIGGER trg_loans_copy_lendable BEFORE INSERT ON loans
    WHEN NOT EXISTS (SELECT 1 FROM copies WHERE id = NEW.copy_id AND status = 'available' AND is_loanable = 1)
    BEGIN SELECT RAISE(ABORT, 'Exemplaire non empruntable'); END""",
    """CREATE TABLE loan_events (
        id INTEGER NOT NULL,
        loan_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        at TEXT NOT NULL,
        user_id INTEGER,
        reason_code TEXT,
        details TEXT,
        CONSTRAINT pk_loan_events PRIMARY KEY (id),
        CONSTRAINT fk_loan_events_loan_id_loans FOREIGN KEY (loan_id) REFERENCES loans (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loan_events_user_id_users FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_loan_events_kind_values
            CHECK (kind IN ('created','renewed','due_changed','returned','cancelled','corrected','override'))
    ) STRICT""",
    "CREATE INDEX ix_loan_events_loan ON loan_events (loan_id)",
    """CREATE TABLE loan_reminders (
        id INTEGER NOT NULL,
        loan_id INTEGER NOT NULL,
        reminded_on TEXT NOT NULL,
        channel TEXT NOT NULL,
        result TEXT NOT NULL,
        user_id INTEGER,
        created_at TEXT NOT NULL,
        CONSTRAINT pk_loan_reminders PRIMARY KEY (id),
        CONSTRAINT fk_loan_reminders_loan_id_loans FOREIGN KEY (loan_id) REFERENCES loans (id) ON DELETE RESTRICT,
        CONSTRAINT fk_loan_reminders_user_id_users FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_loan_reminders_channel_values CHECK (channel IN ('phone','in_person','email_outside','letter')),
        CONSTRAINT ck_loan_reminders_result_values
            CHECK (result IN ('reached','message_left','no_answer','wrong_contact','return_promised'))
    ) STRICT""",
    "CREATE INDEX ix_loan_reminders_loan ON loan_reminders (loan_id)",
    """CREATE VIEW copy_availability AS
    SELECT c.id AS copy_id, c.edition_id,
      CASE WHEN l.id IS NOT NULL THEN 'on_loan' WHEN c.status <> 'available' THEN c.status
           WHEN c.is_loanable = 0 THEN 'reference_only' ELSE 'available' END AS availability,
      l.id AS loan_id, l.due_on
    FROM copies c LEFT JOIN loans l ON l.copy_id = c.id AND l.returned_at IS NULL AND l.cancelled_at IS NULL""",
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal(bind: sa.engine.Connection, action: str, entity: str, details: dict[str, object]) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO audit_log (at, user_id, action, entity_type, entity_id, details)"
            " VALUES (:at, NULL, :action, :entity, NULL, :details)"
        ),
        {"at": _now(), "action": action, "entity": entity, "details": json.dumps(details, ensure_ascii=False)},
    )


def _adjust_librarian(bind: sa.engine.Connection) -> None:
    role_id = bind.execute(sa.text("SELECT id FROM roles WHERE code = 'librarian'")).scalar()
    if role_id is None:
        log.warning("Rôle « librarian » absent : aucun ajustement.")
        return
    current = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT permission FROM role_permissions WHERE role_id = :role"), {"role": role_id}
        )
    }
    if current != LIBRARIAN_0007:
        log.warning(
            "Rôle « librarian » personnalisé : laissé tel quel. À revoir à la main : ajouter %s.", ", ".join(ADDED)
        )
        _journal(bind, "migration.role_kept", "role", {"revision": revision, "role": "librarian"})
        return
    moment = _now()
    for permission in ADDED:
        bind.execute(
            sa.text("INSERT INTO role_permissions (role_id, permission) VALUES (:role, :permission)"),
            {"role": role_id, "permission": permission},
        )
        bind.execute(
            sa.text(
                "INSERT INTO role_permission_changes (revision, role_id, permission, change, applied_at)"
                " VALUES (:revision, :role, :permission, 'added', :at)"
            ),
            {"revision": revision, "role": role_id, "permission": permission, "at": moment},
        )
    _journal(
        bind, "migration.role_adjusted", "role", {"revision": revision, "role": "librarian", "ajouts": list(ADDED)}
    )


def _report_code_collisions(bind: sa.engine.Connection) -> None:
    """Signale, sans rien modifier et sans citer aucun numéro : le comptoir demandera un choix explicite (C4)."""
    copies = bind.execute(
        sa.text(
            "SELECT COUNT(DISTINCT c.number) FROM patron_cards c JOIN copies e"
            " ON e.barcode = c.number OR e.inventory_number = c.number"
        )
    ).scalar()
    shaped = sum(
        1 for row in bind.execute(sa.text("SELECT number FROM patron_cards")) if _PATRON_NUMBER.fullmatch(row[0])
    )
    if copies or shaped:
        log.warning(
            "Codes à vérifier, rien n'a été modifié : %d carte(s) égale(s) à un code d'exemplaire, "
            "%d carte(s) lisible(s) comme un numéro de lecteur. Le comptoir demandera un choix explicite.",
            copies,
            shaped,
        )
        _journal(bind, "migration.code_collisions", "patron_card", {"exemplaires": copies, "numeros_lecteur": shaped})


def upgrade() -> None:
    bind = op.get_bind()
    for statement in STATEMENTS:
        bind.exec_driver_sql(statement)
    for scope, loan_days, renewal_days, renewals, maximum in DEFAULT_RULES:
        bind.execute(
            sa.text(
                "INSERT INTO loan_rules (patron_category, loan_days, renewal_days, max_renewals, max_active_loans)"
                " VALUES (:scope, :loan_days, :renewal_days, :renewals, :maximum)"
            ),
            {
                "scope": scope,
                "loan_days": loan_days,
                "renewal_days": renewal_days,
                "renewals": renewals,
                "maximum": maximum,
            },
        )
    moment = _now()
    for key, value in SETTINGS.items():
        bind.execute(
            sa.text("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (:key, :value, :at)"),
            {"key": key, "value": json.dumps(value, ensure_ascii=False), "at": moment},
        )
    _adjust_librarian(bind)
    _report_code_collisions(bind)


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        "prêts": "SELECT COUNT(*) FROM loans",
        "opérations": "SELECT COUNT(*) FROM loan_operations",
        "rappels": "SELECT COUNT(*) FROM loan_reminders",
        "droits d'emprunter seul": "SELECT COUNT(*) FROM guardianships WHERE can_borrow_alone = 1",
        "droits pour une structure": "SELECT COUNT(*) FROM household_members WHERE can_borrow_for_structure = 1",
    }
    filled = {name: count for name, query in counts.items() if (count := bind.exec_driver_sql(query).scalar())}
    if filled:
        detail = ", ".join(f"{name} : {count}" for name, count in filled.items())
        raise RuntimeError(
            f"Des données de prêt existent ({detail}). Elles ne sont jamais supprimées par un retour arrière : "
            "restaurez la sauvegarde prise avant la migration."
        )
    for role_id, permission in bind.execute(
        sa.text("SELECT role_id, permission FROM role_permission_changes WHERE revision = :revision ORDER BY id DESC"),
        {"revision": revision},
    ).all():
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE role_id = :role AND permission = :permission"),
            {"role": role_id, "permission": permission},
        )
    bind.execute(sa.text("DELETE FROM role_permission_changes WHERE revision = :revision"), {"revision": revision})
    for key in SETTINGS:
        bind.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": key})
    bind.exec_driver_sql("DROP VIEW copy_availability")
    for table in ("loan_reminders", "loan_events", "loans", "loan_operations", "loan_rules"):
        bind.exec_driver_sql(f"DROP TABLE {table}")
    bind.exec_driver_sql("ALTER TABLE household_members DROP COLUMN can_borrow_for_structure")
    bind.exec_driver_sql("ALTER TABLE guardianships DROP COLUMN can_borrow_alone")
