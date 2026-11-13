# SPDX-License-Identifier: EUPL-1.2
"""Prêts, opérations, événements, rappels et règles (révision 0008, plan de L4 § 6).

**Un prêt porte sur un exemplaire physique** et dit qui l'utilise, qui en répond et dans quel cadre. L'historique ne se
déduit d'aucun champ écrasé : chaque opération est un événement. Aucune colonne de texte libre, ni sur le prêt ni sur
le rappel. L'index `ux_loan_rules_scope`, sur des expressions, n'est connu que de la révision.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base

ACTIVE = text("returned_at IS NULL AND cancelled_at IS NULL")


class LoanRule(Base):
    """Durée, renouvellements et plafond. Une portée vide vaut pour tous."""

    __tablename__ = "loan_rules"
    __table_args__ = (
        CheckConstraint("patron_category IN ('adult','minor','structure')", name="patron_category_values"),
        CheckConstraint("loan_days BETWEEN 1 AND 365", name="loan_days_range"),
        CheckConstraint("renewal_days BETWEEN 1 AND 365", name="renewal_days_range"),
        CheckConstraint("max_renewals >= 0", name="max_renewals_positive"),
        CheckConstraint("max_active_loans >= 1", name="max_active_loans_positive"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_type_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("document_types.id", ondelete="CASCADE"))
    patron_category: Mapped[str | None] = mapped_column(Text)
    loan_days: Mapped[int] = mapped_column(Integer, nullable=False)
    renewal_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_renewals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_active_loans: Mapped[int | None] = mapped_column(Integer)


class LoanOperation(Base):
    """Une validation au comptoir, pour annuler l'ensemble d'un coup (D6, D9)."""

    __tablename__ = "loan_operations"
    __table_args__ = (CheckConstraint("kind IN ('lend','return','renew')", name="kind_values"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (
        CheckConstraint("borrow_mode IN ('self','with_adult','alone','structure')", name="borrow_mode_values"),
        CheckConstraint("renewals >= 0", name="renewals_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("returned_at IS NULL OR returned_at >= loaned_at", name="return_after_loan"),
        CheckConstraint("responsible_id IS NULL OR responsible_id <> patron_id", name="responsible_distinct"),
        CheckConstraint(
            "returned_at IS NOT NULL OR cancelled_at IS NOT NULL"
            " OR (borrow_mode = 'self' AND responsible_id IS NULL AND household_id IS NULL)"
            " OR (borrow_mode IN ('with_adult','alone') AND responsible_id IS NOT NULL)"
            " OR (borrow_mode = 'structure' AND household_id IS NOT NULL)",
            name="mode_active",
        ),
        Index("ux_loans_active_copy", "copy_id", unique=True, sqlite_where=ACTIVE),
        Index("ix_loans_patron_active", "patron_id", sqlite_where=ACTIVE),
        Index("ix_loans_due_active", "due_on", sqlite_where=ACTIVE),
        Index("ix_loans_responsible", "responsible_id"),
        Index("ix_loans_copy_history", "copy_id", "loaned_at"),
        Index("ix_loans_operation", "operation_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("loan_operations.id", ondelete="RESTRICT"), nullable=False
    )
    copy_id: Mapped[int] = mapped_column(Integer, ForeignKey("copies.id", ondelete="RESTRICT"), nullable=False)
    patron_id: Mapped[int] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"), nullable=False)
    responsible_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"))
    household_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("households.id", ondelete="RESTRICT"))
    borrow_mode: Mapped[str] = mapped_column(Text, nullable=False)
    loaned_at: Mapped[str] = mapped_column(Text, nullable=False)
    due_on: Mapped[str] = mapped_column(Text, nullable=False)  # date calendaire, sans heure
    returned_at: Mapped[str | None] = mapped_column(Text)
    renewals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loaned_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    returned_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class LoanEvent(Base):
    """Un fait daté de la vie d'un prêt. `details` : identifiants et codes, jamais de valeur personnelle."""

    __tablename__ = "loan_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('created','renewed','due_changed','returned','cancelled','corrected','override')",
            name="kind_values",
        ),
        Index("ix_loan_events_loan", "loan_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int] = mapped_column(Integer, ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    reason_code: Mapped[str | None] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)


class LoanReminder(Base):
    """Un rappel noté à la main (D15) : des valeurs structurées, aucun texte libre, aucun envoi."""

    __tablename__ = "loan_reminders"
    __table_args__ = (
        CheckConstraint("channel IN ('phone','in_person','email_outside','letter')", name="channel_values"),
        CheckConstraint(
            "result IN ('reached','message_left','no_answer','wrong_contact','return_promised')", name="result_values"
        ),
        Index("ix_loan_reminders_loan", "loan_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int] = mapped_column(Integer, ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False)
    reminded_on: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
