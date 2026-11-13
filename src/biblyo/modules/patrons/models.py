# SPDX-License-Identifier: EUPL-1.2
"""Lecteurs, cartes, foyers, liens et coordonnées (révision 0007, plan de L3).

**Trois identifiants distincts** : `public_id` (UUID interne immuable, pour les relations et les exports), `number`
(numéro de lecteur permanent, lisible, jamais réattribué) et les cartes successives de `patron_cards`. **Les liens ne
portent un droit que s'il y est écrit** : un rôle dans un foyer est descriptif ; seules les colonnes d'autorisation de
`guardianships` comptent.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base, IntBool


class Patron(Base):
    """L'identité d'une personne inscrite. Rien ici ne décrit ses relations."""

    __tablename__ = "patrons"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_patrons_public_id"),
        UniqueConstraint("number", name="uq_patrons_number"),
        CheckConstraint("category IN ('adult','minor')", name="category_values"),
        CheckConstraint("status IN ('active','suspended','archived','anonymized')", name="status_values"),
        CheckConstraint("status = 'anonymized' OR length(trim(last_name)) > 0", name="name_present"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_patrons_name_norm", "name_norm"),
        Index("ix_patrons_sort_key", "sort_key"),
        Index("ix_patrons_status", "status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)  # UUID v7, immuable
    number: Mapped[str] = mapped_column(Text, nullable=False)  # « P-000001 », permanent
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    sort_key: Mapped[str] = mapped_column(Text, nullable=False)
    birth: Mapped[str | None] = mapped_column(Text)  # « 2014 » ou « 2014-05-03 »
    category: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    registered_on: Mapped[str] = mapped_column(Text, nullable=False)
    membership_expires_on: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)  # observations internes, jamais de donnée sensible
    archived_at: Mapped[str | None] = mapped_column(Text)
    anonymized_at: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class PatronCard(Base):
    """Une carte et son histoire. Une ligne n'est jamais supprimée : son numéro reste pris pour toujours (D1)."""

    __tablename__ = "patron_cards"
    __table_args__ = (
        UniqueConstraint("number", name="uq_patron_cards_number"),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('lost','damaged','replaced','revoked','anonymized')",
            name="end_reason_values",
        ),
        CheckConstraint("(ended_on IS NULL) = (end_reason IS NULL)", name="end_consistent"),
        CheckConstraint("patron_id IS NOT NULL OR ended_on IS NOT NULL", name="detached_only_when_ended"),
        Index("ux_patron_cards_active", "patron_id", unique=True, sqlite_where=text("ended_on IS NULL")),
        Index("ix_patron_cards_patron", "patron_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(Text, nullable=False)
    patron_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"))
    issued_on: Mapped[str] = mapped_column(Text, nullable=False)
    ended_on: Mapped[str | None] = mapped_column(Text)
    end_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Household(Base):
    """Une famille ou une structure (école, association) : jamais une personne."""

    __tablename__ = "households"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_households_public_id"),
        CheckConstraint("kind IN ('family','structure')", name="kind_values"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_households_name_norm", "name_norm"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class HouseholdMember(Base):
    """Appartenance d'un lecteur à un foyer. Le rôle est **descriptif** : il ne donne aucun droit (D4)."""

    __tablename__ = "household_members"
    __table_args__ = (
        CheckConstraint("role IN ('responsible','member','child','referent')", name="role_values"),
        CheckConstraint("can_borrow_for_structure IN (0, 1)", name="can_borrow_for_structure"),
        Index("ix_household_members_patron", "patron_id"),
        TABLE_OPTIONS,
    )

    household_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("households.id", ondelete="RESTRICT"), primary_key=True
    )
    patron_id: Mapped[int] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    joined_on: Mapped[str] = mapped_column(Text, nullable=False)
    #: Autorisation explicite sur ce lien, jamais déduite du rôle de référent (D2 du plan de L4)
    can_borrow_for_structure: Mapped[bool] = mapped_column(
        IntBool, nullable=False, default=False, server_default=text("0")
    )


class Guardianship(Base):
    """Un adulte responsable d'un mineur, et ce qu'il peut faire : **chaque droit est explicite** (D5)."""

    __tablename__ = "guardianships"
    __table_args__ = (
        CheckConstraint("minor_id <> guardian_id", name="distinct_people"),
        CheckConstraint("relation IN ('parent','legal_guardian','teacher','other')", name="relation_values"),
        CheckConstraint("can_borrow_for IN (0, 1)", name="can_borrow_for_bool"),
        CheckConstraint("is_contact IN (0, 1)", name="is_contact_bool"),
        CheckConstraint("can_borrow_alone IN (0, 1)", name="can_borrow_alone"),
        CheckConstraint("contact_rank >= 1", name="contact_rank_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_guardianships_guardian", "guardian_id"),
        TABLE_OPTIONS,
    )

    minor_id: Mapped[int] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"), primary_key=True)
    guardian_id: Mapped[int] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"), primary_key=True)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    can_borrow_for: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    is_contact: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    contact_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: « Peut emprunter seul » : droit distinct de `can_borrow_for`, jamais déduit de lui (D1 et P1 du plan de L4)
    can_borrow_alone: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Contact(Base):
    """Une coordonnée, d'une personne **ou** d'un foyer, jamais des deux."""

    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint("(patron_id IS NULL) <> (household_id IS NULL)", name="one_owner"),
        CheckConstraint("kind IN ('email','phone','address')", name="kind_values"),
        CheckConstraint("label IS NULL OR label IN ('home','mobile','work','other')", name="label_values"),
        CheckConstraint("is_primary IN (0, 1)", name="is_primary_bool"),
        CheckConstraint(
            "(kind = 'address') = (line1 IS NOT NULL AND city IS NOT NULL AND value IS NULL)", name="address_shape"
        ),
        Index("ix_contacts_patron", "patron_id"),
        Index("ix_contacts_household", "household_id"),
        Index("ix_contacts_search", "kind", "search_value"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patron_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"))
    household_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("households.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text)  # courriel, ou téléphone au format E.164
    search_value: Mapped[str | None] = mapped_column(Text)  # forme comparée par la recherche réservée
    line1: Mapped[str | None] = mapped_column(Text)
    line2: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class PatronHold(Base):
    """Mise en conservation (P2) : tant qu'elle court, l'anonymisation est refusée."""

    __tablename__ = "patron_holds"
    __table_args__ = (
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_empty"),
        Index("ix_patron_holds_patron", "patron_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patron_id: Mapped[int] = mapped_column(Integer, ForeignKey("patrons.id", ondelete="RESTRICT"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    until: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[str] = mapped_column(Text, nullable=False)
    placed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    lifted_at: Mapped[str | None] = mapped_column(Text)
    lifted_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
