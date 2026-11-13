# SPDX-License-Identifier: EUPL-1.2
"""Écritures des lecteurs, foyers, responsables et cartes (lot L3).

Chaque écriture est une transaction `BEGIN IMMEDIATE` : deux inscriptions simultanées reçoivent des numéros distincts,
sans trou ni réemploi. Une modification fondée sur un affichage périmé est refusée (`version`). **Rien n'est écrasé en
silence** : un numéro de carte déjà pris, un lien qui existe déjà, un homonyme non confirmé sont refusés avec un
message. Le journal dit quels champs ont changé, **jamais leurs valeurs** (D10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text, update
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now, now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.models import AuditEntry, User
from biblyo.core.phones import Ambiguous, Phone, PhoneError, parse_phone
from biblyo.core.security import passwords
from biblyo.modules.patrons import repository as repo
from biblyo.modules.patrons import rules
from biblyo.modules.patrons.models import (
    Contact,
    Guardianship,
    Household,
    HouseholdMember,
    Patron,
    PatronCard,
    PatronHold,
)
from biblyo.modules.patrons.rules import PatronError

CONFLICT_MESSAGE = _(
    "Cette fiche vient d’être modifiée par une autre personne. Rien n’a été enregistré : rechargez la page, puis "
    "recommencez."
)


class ConflictError(BusinessError):
    """Deux modifications concurrentes : la seconde est refusée."""


def today() -> date:
    return now().date()


# --- Saisies ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AddressInput:
    line1: str = ""
    line2: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = "FR"


@dataclass(frozen=True)
class PersonInput:
    first_name: str
    last_name: str
    category: str
    birth: str = ""
    email: str = ""
    phone: str = ""
    phone_territory: str = ""
    address: AddressInput = field(default_factory=AddressInput)
    notes: str = ""
    membership_expires_on: str = ""
    card_number: str = ""


@dataclass(frozen=True)
class GuardianInput:
    """Un responsable existant (par son numéro permanent) ou nouveau. Chaque droit est coché explicitement."""

    number: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    phone_territory: str = ""
    relation: str = "parent"
    can_borrow_for: bool = False
    is_contact: bool = False
    can_borrow_alone: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.number.strip() or self.first_name.strip() or self.last_name.strip())


@dataclass(frozen=True)
class HouseholdChoice:
    household_id: int | None = None
    new_name: str = ""
    kind: str = "family"
    role: str = ""


@dataclass(frozen=True)
class Registration:
    person: PersonInput
    household: HouseholdChoice = field(default_factory=HouseholdChoice)
    guardian: GuardianInput = field(default_factory=GuardianInput)
    homonyms_confirmed: bool = False


@dataclass
class Review:
    """Ce que la vérification d'une inscription a trouvé, avant tout enregistrement."""

    errors: dict[str, str] = field(default_factory=dict)
    homonyms: list[repo.Row] = field(default_factory=list)
    phone_choices: dict[str, list[Phone]] = field(default_factory=dict)
    guardian_name: str | None = None
    hidden_homonyms: int = 0  # fiches de même nom que ce compte ne peut pas ouvrir : leur nombre, sans leurs repères
    phones: dict[str, str] = field(default_factory=dict)  # numéros compris, tels qu'ils seront affichés

    @property
    def blocking(self) -> bool:
        return bool(self.errors)


class RegistrationRefused(FormErrors):
    """Inscription refusée : erreurs par champ, homonymes à confirmer, numéros ambigus à préciser."""

    def __init__(self, review: Review) -> None:
        super().__init__(review.errors)
        self.review = review


# --- Outils -----------------------------------------------------------------------------------------------------------
def _next_value(session: Session, name: str) -> str:
    """Numéroteur sans trou ni réemploi. La transaction d'écriture sérialise les appels concurrents."""
    row = session.execute(
        text(
            "UPDATE sequences SET last_value = last_value + 1 WHERE name = :name RETURNING prefix, padding, last_value"
        ),
        {"name": name},
    ).one()
    prefix, padding, value = row
    return f"{prefix}{value:0{padding}d}"


def code_taken(session: Session, value: str) -> bool:
    """Un numéro de carte n'est jamais réattribué, et ne doit pas désigner aussi un exemplaire au scan."""
    return bool(
        session.scalar(select(func.count()).select_from(PatronCard).where(PatronCard.number == value))
        or session.execute(
            text(
                "SELECT 1 FROM copies WHERE barcode = :value OR inventory_number = :value"
                " UNION SELECT 1 FROM retired_codes WHERE value = :value"
            ),
            {"value": value},
        ).first()
    )


def _new_card(session: Session, patron_id: int, manual: str | None, moment: str) -> PatronCard:
    number = manual
    while number is None or code_taken(session, number):
        if manual is not None:
            raise PatronError(_("Ce numéro de carte est déjà pris, ou l’a été : il ne peut pas être réattribué."))
        number = _next_value(session, "patron_card")
    card = PatronCard(number=number, patron_id=patron_id, issued_on=moment[:10], created_at=moment)
    session.add(card)
    session.flush()
    return card


def _get_patron(session: Session, patron_id: int) -> Patron:
    patron = session.get(Patron, patron_id)
    if patron is None:
        raise BusinessError(_("Ce lecteur n’existe pas."))
    return patron


def _living(patron: Patron) -> None:
    if patron.status == "anonymized":
        raise BusinessError(_("Ce lecteur est anonymisé : sa fiche ne se modifie plus."))


def _check_version(current: int, expected: int | None) -> None:
    if expected is not None and current != expected:
        raise ConflictError(CONFLICT_MESSAGE)


def _touch(patron: Patron, user_id: int | None, moment: str | None = None) -> None:
    patron.version += 1
    patron.updated_at = moment or now_iso()
    patron.updated_by = user_id


def _text(value: str, limit: int, key: str, errors: dict[str, str]) -> str | None:
    cleaned = (value or "").strip()
    if len(cleaned) > limit:
        errors[key] = _("Ce texte est trop long.")
    return cleaned or None


def _parse_date(value: str, key: str, errors: dict[str, str]) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        parsed = rules.parse_birth(cleaned, date(2200, 1, 1))
    except PatronError:
        parsed = None
    if parsed is None or len(parsed) != 10:
        errors[key] = _("Écrivez une date complète : 31/08/2027.")
        return None
    return parsed


def _phone(text_value: str, territory: str, key: str, review: Review) -> Phone | None:
    try:
        result = parse_phone(text_value, territory or None)
    except PhoneError as error:
        review.errors[key] = str(error)
        return None
    if isinstance(result, Ambiguous):
        review.errors[key] = result.reason
        review.phone_choices[key] = list(result.candidates)
        return None
    if result is not None:
        review.phones[key] = f"{result.display} · {result.territory_label}"
    return result


@dataclass
class _Person:
    first_name: str = ""
    last_name: str = ""
    category: str = "adult"
    birth: str | None = None
    email: str | None = None
    phone: Phone | None = None
    address: rules.Address | None = None
    notes: str | None = None
    membership: str | None = None
    card: str | None = None


def _clean_contact_fields(
    email: str, phone: str, territory: str, address: AddressInput, review: Review, prefix: str = ""
) -> tuple[str | None, Phone | None, rules.Address | None]:
    errors = review.errors
    cleaned_email = cleaned_address = None
    try:
        cleaned_email = rules.clean_email(email)
    except PatronError as error:
        errors[prefix + "courriel"] = str(error)
    cleaned_phone = _phone(phone, territory, prefix + "telephone", review)
    try:
        cleaned_address = rules.clean_address(
            address.line1, address.line2, address.postal_code, address.city, address.country
        )
    except PatronError as error:
        errors[prefix + "adresse"] = str(error)
    return cleaned_email, cleaned_phone, cleaned_address


def _clean_person(data: PersonInput, review: Review, prefix: str = "") -> _Person:
    errors = review.errors
    person = _Person()
    for attribute, key, label in (("first_name", "prenom", _("prénom")), ("last_name", "nom", _("nom"))):
        try:
            setattr(person, attribute, rules.clean_name(getattr(data, attribute), label))
        except PatronError as error:
            errors[prefix + key] = str(error)
    if data.category not in rules.CATEGORIES:
        errors[prefix + "categorie"] = _("Choisissez adulte ou mineur.")
    else:
        person.category = data.category
    try:
        person.birth = rules.parse_birth(data.birth, today())
    except PatronError as error:
        errors[prefix + "naissance"] = str(error)
    if person.category == "minor" and (data.email.strip() or data.phone.strip() or data.address.line1.strip()):
        errors[prefix + "coordonnees"] = _(
            "Un mineur n’a pas de coordonnées propres : renseignez-les sur son responsable."
        )
    else:
        person.email, person.phone, person.address = _clean_contact_fields(
            data.email, data.phone, data.phone_territory, data.address, review, prefix
        )
    person.notes = _text(data.notes, rules.MAX_NOTE, prefix + "observations", errors)
    person.membership = _parse_date(data.membership_expires_on, prefix + "adhesion", errors)
    if data.card_number.strip():
        try:
            person.card = rules.check_new_card(data.card_number)
        except PatronError as error:
            errors[prefix + "carte"] = str(error)
    return person


def _add_contacts(session: Session, owner: dict[str, int], person: _Person, moment: str) -> list[str]:
    """Ajoute les coordonnées saisies ; rend les noms des champs remplis, pour le journal (jamais les valeurs)."""
    filled: list[str] = []
    if person.email:
        session.add(
            Contact(
                **owner,
                kind="email",
                value=person.email,
                search_value=person.email.lower(),
                is_primary=True,
                created_at=moment,
                updated_at=moment,
            )
        )
        filled.append("courriel")
    if person.phone:
        session.add(
            Contact(
                **owner,
                kind="phone",
                label="mobile",
                value=person.phone.e164,
                search_value=person.phone.e164.lstrip("+"),
                is_primary=True,
                created_at=moment,
                updated_at=moment,
            )
        )
        filled.append("telephone")
    if person.address:
        address = person.address
        session.add(
            Contact(
                **owner,
                kind="address",
                label="home",
                line1=address.line1,
                line2=address.line2,
                postal_code=address.postal_code,
                city=address.city,
                country=address.country,
                is_primary=True,
                created_at=moment,
                updated_at=moment,
            )
        )
        filled.append("adresse")
    return filled


def _create_patron(session: Session, person: _Person, user_id: int | None, moment: str) -> Patron:
    patron = Patron(
        public_id=new_public_id(),
        number=_next_value(session, "patron_number"),
        first_name=person.first_name,
        last_name=person.last_name,
        name_norm=rules.name_norm(person.first_name, person.last_name),
        sort_key=rules.sort_key(person.first_name, person.last_name),
        birth=person.birth,
        category=person.category,
        status="active",
        registered_on=moment[:10],
        membership_expires_on=person.membership,
        notes=person.notes,
        version=1,
        created_at=moment,
        updated_at=moment,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(patron)
    session.flush()
    return patron


# --- Inscription (`patrons.register`) ---------------------------------------------------------------------------------
def _review(
    session: Session, data: Registration, visibility: repo.Visibility
) -> tuple[Review, _Person, _Person | None, Patron | None]:
    review = Review()
    person = _clean_person(data.person, review)
    if not review.errors.get("prenom") and not review.errors.get("nom"):
        review.homonyms = repo.homonyms(session, person.first_name, person.last_name, visibility=visibility)
        review.hidden_homonyms = repo.hidden_homonyms(session, person.first_name, person.last_name, visibility)
    if person.card and code_taken(session, person.card):
        review.errors["carte"] = _("Ce numéro de carte est déjà pris, ou l’a été : il ne peut pas être réattribué.")
    choice = data.household
    if choice.household_id is not None:
        household = session.get(Household, choice.household_id)
        if household is None or household.archived_at is not None:
            review.errors["foyer"] = _("Ce foyer n’existe pas ou est archivé.")
        elif choice.role:
            try:
                rules.check_member_role(household.kind, choice.role)
            except PatronError as error:
                review.errors["role"] = str(error)
    elif choice.new_name.strip():
        if choice.kind not in rules.HOUSEHOLD_KINDS:
            review.errors["foyer"] = _("Choisissez famille ou structure.")
        elif len(choice.new_name.strip()) > rules.MAX_NAME:
            review.errors["foyer"] = _("Ce nom de foyer est trop long.")
        elif choice.role:
            try:
                rules.check_member_role(choice.kind, choice.role)
            except PatronError as error:
                review.errors["role"] = str(error)
    guardian_person: _Person | None = None
    guardian_existing: Patron | None = None
    guardian = data.guardian
    if not guardian.is_empty:
        if person.category != "minor":
            review.errors["resp_numero"] = _("Seul un mineur a des responsables.")
        if guardian.relation not in rules.RELATIONS:
            review.errors["resp_relation"] = _("Choisissez la relation.")
        if guardian.number.strip():
            number = rules.parse_patron_number(guardian.number)
            guardian_existing = session.scalar(select(Patron).where(Patron.number == number)) if number else None
            if guardian_existing is None:
                review.errors["resp_numero"] = _("Aucun lecteur ne porte ce numéro (P-000123).")
            elif guardian_existing.category != "adult" or guardian_existing.status not in ("active", "suspended"):
                review.errors["resp_numero"] = _("Un responsable doit être un adulte actif.")
            else:
                review.guardian_name = f"{guardian_existing.first_name} {guardian_existing.last_name}"
        else:
            guardian_person = _clean_person(
                PersonInput(
                    first_name=guardian.first_name,
                    last_name=guardian.last_name,
                    category="adult",
                    email=guardian.email,
                    phone=guardian.phone,
                    phone_territory=guardian.phone_territory,
                ),
                review,
                prefix="resp_",
            )
    return review, person, guardian_person, guardian_existing


def check_registration(database: Database, data: Registration, visibility: repo.Visibility = repo.EVERYTHING) -> Review:
    """L'étape « vérifier et corriger » : rien n'est enregistré. Rend erreurs, homonymes et numéros à préciser."""
    with database.session() as session:
        review, _person, _guardian, _existing = _review(session, data, visibility)
        return review


def _default_role(kind: str, category: str) -> str:
    if kind == "structure":
        return "member"
    return "child" if category == "minor" else "responsible"


def register(
    database: Database, data: Registration, user_id: int | None, visibility: repo.Visibility = repo.EVERYTHING
) -> int:
    """Inscrit une personne, et au besoin son foyer et son responsable, en une seule transaction. Rend son identifiant.

    Un homonyme doit avoir été confirmé comme une autre personne : rien n'est rapproché automatiquement (D8). Un
    mineur peut être inscrit sans responsable : sa fiche affichera « Responsable à renseigner » (D5).
    """
    with database.session(write=True) as session:
        review, person, guardian_person, guardian_existing = _review(session, data, visibility)
        if (review.homonyms or review.hidden_homonyms) and not data.homonyms_confirmed:
            review.errors.setdefault(
                "homonymes", _("Une personne porte déjà ce nom : confirmez qu’il s’agit d’une autre personne.")
            )
        if review.blocking:
            raise RegistrationRefused(review)
        moment = now_iso()
        try:
            patron = _create_patron(session, person, user_id, moment)
            _new_card(session, patron.id, person.card, moment)
        except PatronError as error:
            raise RegistrationRefused(Review(errors={"carte": str(error)})) from error
        filled = _add_contacts(session, {"patron_id": patron.id}, person, moment)
        household_id = _join_household(session, patron, data.household, moment, user_id)
        guardian_id = None
        if guardian_existing is not None or guardian_person is not None:
            if guardian_existing is None:
                assert guardian_person is not None
                guardian_existing = _create_patron(session, guardian_person, user_id, moment)
                _new_card(session, guardian_existing.id, None, moment)
                guardian_fields = _add_contacts(session, {"patron_id": guardian_existing.id}, guardian_person, moment)
                if household_id is not None:
                    session.add(
                        HouseholdMember(
                            household_id=household_id,
                            patron_id=guardian_existing.id,
                            role=_default_role(_kind(session, household_id), "adult"),
                            joined_on=moment[:10],
                        )
                    )
                audit.record(
                    session,
                    "patron.register",
                    "patron",
                    guardian_existing.id,
                    user_id=user_id,
                    details={"champs": guardian_fields, "foyer": household_id},
                )
            guardian = data.guardian
            session.add(
                Guardianship(
                    minor_id=patron.id,
                    guardian_id=guardian_existing.id,
                    relation=guardian.relation,
                    can_borrow_for=guardian.can_borrow_for,
                    is_contact=guardian.is_contact,
                    can_borrow_alone=guardian.can_borrow_alone,
                    contact_rank=1,
                    version=1,
                    created_at=moment,
                    updated_at=moment,
                )
            )
            guardian_id = guardian_existing.id
        audit.record(
            session,
            "patron.register",
            "patron",
            patron.id,
            user_id=user_id,
            details={"champs": filled, "foyer": household_id, "responsable": guardian_id},
        )
        return patron.id


def _kind(session: Session, household_id: int) -> str:
    household = session.get(Household, household_id)
    assert household is not None
    return household.kind


def _join_household(
    session: Session, patron: Patron, choice: HouseholdChoice, moment: str, user_id: int | None
) -> int | None:
    if choice.household_id is not None:
        household = session.get(Household, choice.household_id)
        assert household is not None
    elif choice.new_name.strip():
        household = Household(
            public_id=new_public_id(),
            kind=choice.kind,
            name=" ".join(choice.new_name.split()),
            name_norm=rules.name_norm(choice.new_name, ""),
            version=1,
            created_at=moment,
            updated_at=moment,
        )
        session.add(household)
        session.flush()
        audit.record(
            session, "household.create", "household", household.id, user_id=user_id, details={"type": household.kind}
        )
    else:
        return None
    role = choice.role or _default_role(household.kind, patron.category)
    session.add(HouseholdMember(household_id=household.id, patron_id=patron.id, role=role, joined_on=moment[:10]))
    audit.record(
        session,
        "patron.household_joined",
        "patron",
        patron.id,
        user_id=user_id,
        details={"foyer": household.id, "role": role},
    )
    return household.id


# --- Inscription d'une famille, en une saisie et une transaction (`patrons.register`) ---------------------------------
@dataclass(frozen=True)
class FamilyAdult:
    """Un adulte de la famille. Ses droits valent lien par lien : chaque enfant reçoit un lien qui les porte."""

    person: PersonInput
    relation: str = "parent"
    can_borrow_for: bool = False
    is_contact: bool = False


@dataclass(frozen=True)
class FamilyRegistration:
    name: str
    adults: tuple[FamilyAdult, ...] = ()
    children: tuple[PersonInput, ...] = ()
    shared_email: str = ""
    shared_phone: str = ""
    shared_phone_territory: str = ""
    shared_address: AddressInput = field(default_factory=AddressInput)
    homonyms_confirmed: bool = False


def _review_family(
    session: Session, data: FamilyRegistration, visibility: repo.Visibility
) -> tuple[Review, list[_Person], list[_Person], _Person]:
    review = Review()
    cleaned_name = " ".join((data.name or "").split())
    if not cleaned_name or len(cleaned_name) > rules.MAX_NAME:
        review.errors["nom_foyer"] = _("Indiquez un nom de famille (80 caractères au plus).")
    if not data.adults and not data.children:
        review.errors["personnes"] = _("Ajoutez au moins une personne à cette famille.")
    adults = [_clean_person(item.person, review, prefix=f"a{index}_") for index, item in enumerate(data.adults)]
    for index, item in enumerate(data.adults):
        if item.relation not in rules.RELATIONS:
            review.errors[f"a{index}_relation"] = _("Choisissez la relation.")
    children = [_clean_person(item, review, prefix=f"e{index}_") for index, item in enumerate(data.children)]
    shared = _Person()
    shared.email, shared.phone, shared.address = _clean_contact_fields(
        data.shared_email, data.shared_phone, data.shared_phone_territory, data.shared_address, review, "foyer_"
    )
    seen: set[int] = set()
    for person in [*adults, *children]:
        if person.first_name and person.last_name:
            review.hidden_homonyms += repo.hidden_homonyms(session, person.first_name, person.last_name, visibility)
            for row in repo.homonyms(session, person.first_name, person.last_name, visibility=visibility):
                if row.id not in seen:
                    seen.add(row.id)
                    review.homonyms.append(row)
    return review, adults, children, shared


def check_family(database: Database, data: FamilyRegistration, visibility: repo.Visibility = repo.EVERYTHING) -> Review:
    """L'étape « vérifier et corriger » d'une famille : rien n'est enregistré."""
    with database.session() as session:
        return _review_family(session, data, visibility)[0]


def register_family(
    database: Database, data: FamilyRegistration, user_id: int | None, visibility: repo.Visibility = repo.EVERYTHING
) -> int:
    """Crée le foyer, ses membres, leurs cartes, ses coordonnées et chaque lien de responsabilité. Tout ou rien."""
    with database.session(write=True) as session:
        review, adults, children, shared = _review_family(session, data, visibility)
        if (review.homonyms or review.hidden_homonyms) and not data.homonyms_confirmed:
            review.errors.setdefault(
                "homonymes", _("Une personne porte déjà l’un de ces noms : confirmez qu’il s’agit d’autres personnes.")
            )
        if review.blocking:
            raise RegistrationRefused(review)
        moment = now_iso()
        name = " ".join(data.name.split())
        household = Household(
            public_id=new_public_id(),
            kind="family",
            name=name,
            name_norm=rules.name_norm(name, ""),
            version=1,
            created_at=moment,
            updated_at=moment,
        )
        session.add(household)
        session.flush()
        filled = _add_contacts(session, {"household_id": household.id}, shared, moment)
        audit.record(
            session,
            "household.create",
            "household",
            household.id,
            user_id=user_id,
            details={"type": "family", "champs": filled},
        )

        def enrol(person: _Person, role: str) -> Patron:
            patron = _create_patron(session, person, user_id, moment)
            try:
                _new_card(session, patron.id, person.card, moment)
            except PatronError as error:
                raise RegistrationRefused(Review(errors={"carte": str(error)})) from error
            fields = _add_contacts(session, {"patron_id": patron.id}, person, moment)
            session.add(
                HouseholdMember(household_id=household.id, patron_id=patron.id, role=role, joined_on=moment[:10])
            )
            audit.record(
                session,
                "patron.register",
                "patron",
                patron.id,
                user_id=user_id,
                details={"champs": fields, "foyer": household.id},
            )
            return patron

        grown = [enrol(person, "responsible") for person in adults]
        young = [enrol(person, "child") for person in children]
        for rank, (adult, entry) in enumerate(zip(grown, data.adults, strict=True), start=1):
            for child in young:
                session.add(
                    Guardianship(
                        minor_id=child.id,
                        guardian_id=adult.id,
                        relation=entry.relation,
                        can_borrow_for=entry.can_borrow_for,
                        is_contact=entry.is_contact,
                        contact_rank=min(rank, 9),
                        version=1,
                        created_at=moment,
                        updated_at=moment,
                    )
                )
                audit.record(
                    session,
                    "patron.guardian_added",
                    "patron",
                    child.id,
                    user_id=user_id,
                    details={"responsable": adult.id, "emprunter": entry.can_borrow_for, "contacter": entry.is_contact},
                )
        return household.id


# --- Fiche (`patrons.edit`) -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityEdit:
    first_name: str
    last_name: str
    birth: str = ""
    notes: str = ""
    membership_expires_on: str = ""


def update_identity(
    database: Database, patron_id: int, data: IdentityEdit, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        _check_version(patron.version, expected_version)
        review = Review()
        cleaned = _clean_person(
            PersonInput(
                first_name=data.first_name,
                last_name=data.last_name,
                category=patron.category,
                birth=data.birth,
                notes=data.notes,
                membership_expires_on=data.membership_expires_on,
            ),
            review,
        )
        if review.errors:
            raise FormErrors(review.errors)
        values: dict[str, Any] = {
            "first_name": cleaned.first_name,
            "last_name": cleaned.last_name,
            "birth": cleaned.birth,
            "notes": cleaned.notes,
            "membership_expires_on": cleaned.membership,
        }
        changed = [key for key, value in values.items() if getattr(patron, key) != value]
        if not changed:
            return
        for key, value in values.items():
            setattr(patron, key, value)
        patron.name_norm = rules.name_norm(patron.first_name, patron.last_name)
        patron.sort_key = rules.sort_key(patron.first_name, patron.last_name)
        _touch(patron, user_id)
        audit.record(session, "patron.update", "patron", patron.id, user_id=user_id, details={"champs": changed})


@dataclass(frozen=True)
class ContactsEdit:
    email: str = ""
    phone: str = ""
    phone_territory: str = ""
    address: AddressInput = field(default_factory=AddressInput)


def update_contacts(
    database: Database, patron_id: int, data: ContactsEdit, expected_version: int | None, user_id: int | None
) -> None:
    """Remplace les coordonnées propres d'un adulte. Réservé à qui les voit déjà (`view_personal` et `edit`)."""
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        _check_version(patron.version, expected_version)
        review = Review()
        person = _clean_person(
            PersonInput(
                first_name=patron.first_name,
                last_name=patron.last_name,
                category=patron.category,
                email=data.email,
                phone=data.phone,
                phone_territory=data.phone_territory,
                address=data.address,
            ),
            review,
        )
        if review.errors:
            raise RegistrationRefused(review)
        moment = now_iso()
        session.execute(delete(Contact).where(Contact.patron_id == patron.id))
        filled = _add_contacts(session, {"patron_id": patron.id}, person, moment)
        _touch(patron, user_id, moment)
        audit.record(session, "patron.contacts", "patron", patron.id, user_id=user_id, details={"champs": filled})


def confirm_majority(database: Database, patron_id: int, expected_version: int | None, user_id: int | None) -> None:
    """Passage à la majorité, **confirmé par une personne**. Les liens de responsabilité ne sont pas retirés : ils sont
    seulement proposés au retrait sur la fiche."""
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        _check_version(patron.version, expected_version)
        if patron.category != "minor":
            raise BusinessError(_("Ce lecteur est déjà inscrit comme adulte."))
        patron.category = "adult"
        _touch(patron, user_id)
        audit.record(
            session, "patron.majority", "patron", patron.id, user_id=user_id, details={"champs": ["categorie"]}
        )


def renew_membership(
    database: Database, patron_id: int, until: str, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        _check_version(patron.version, expected_version)
        errors: dict[str, str] = {}
        value = _parse_date(until, "adhesion", errors) if until.strip() else (today() + timedelta(days=365)).isoformat()
        if errors:
            raise FormErrors(errors)
        patron.membership_expires_on = value
        _touch(patron, user_id)
        audit.record(session, "patron.renew", "patron", patron.id, user_id=user_id, details={"champs": ["adhesion"]})


STATUS_ACTIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "suspend": (("active",), "suspended"),
    "reactivate": (("suspended", "archived"), "active"),
    "archive": (("active", "suspended"), "archived"),
}


def change_status(
    database: Database, patron_id: int, action: str, expected_version: int | None, user_id: int | None
) -> None:
    """Suspendre, réactiver, archiver (`patrons.archive`). Archiver est refusé s'il reste un prêt en cours (D14).

    Suspendre reste possible : un lecteur suspendu peut toujours rendre.
    """
    if action not in STATUS_ACTIONS:
        raise BusinessError(_("Cette action n’existe pas."))
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        _check_version(patron.version, expected_version)
        allowed, target = STATUS_ACTIONS[action]
        if patron.status not in allowed:
            raise BusinessError(
                _("Impossible depuis le statut « {status} ».").format(status=rules.STATUSES[patron.status])
            )
        if target == "archived" and (loans := active_loans(session, patron.id)):
            raise BusinessError(
                _("Ce lecteur a {n} prêt(s) en cours : l’archivage attendra leur retour.").format(n=loans)
            )
        moment = now_iso()
        patron.status = target
        patron.archived_at = moment if target == "archived" else None
        _touch(patron, user_id, moment)
        audit.record(session, f"patron.{action}", "patron", patron.id, user_id=user_id, details={"champs": ["statut"]})


# --- Cartes (`patrons.edit`, ou `patrons.register` à l'inscription) ---------------------------------------------------
def end_card(
    database: Database,
    patron_id: int,
    reason: str,
    *,
    replace: bool,
    manual_number: str = "",
    user_id: int | None,
) -> str | None:
    """Met fin à la carte en service (perdue, abîmée, révoquée) et, au besoin, en remet une nouvelle.

    L'ancienne reste dans l'historique : son numéro ne sera jamais redonné (D1). Rend le numéro de la nouvelle carte.
    """
    if reason not in ("lost", "damaged", "revoked"):
        raise BusinessError(_("Choisissez pourquoi la carte n’est plus en service."))
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        moment = now_iso()
        current = session.scalar(
            select(PatronCard).where(PatronCard.patron_id == patron.id, PatronCard.ended_on.is_(None))
        )
        if current is None and not replace:
            raise BusinessError(_("Ce lecteur n’a pas de carte en service."))
        if current is not None:
            current.ended_on, current.end_reason = moment[:10], reason
            session.flush()
        new_number = None
        if replace:
            try:
                manual = rules.check_new_card(manual_number) if manual_number.strip() else None
                new_number = _new_card(session, patron.id, manual, moment).number
            except PatronError as error:
                raise FormErrors({"carte": str(error)}) from error
        _touch(patron, user_id, moment)
        audit.record(
            session,
            "patron.card",
            "patron",
            patron.id,
            user_id=user_id,
            details={"fin": reason if current else None, "remplacee": replace},
        )
        return new_number


# --- Foyers et structures ---------------------------------------------------------------------------------------------
def _get_household(session: Session, household_id: int) -> Household:
    household = session.get(Household, household_id)
    if household is None:
        raise BusinessError(_("Ce foyer n’existe pas."))
    return household


def create_household(database: Database, kind: str, name: str, notes: str, user_id: int | None) -> int:
    errors: dict[str, str] = {}
    cleaned = " ".join((name or "").split())
    if kind not in rules.HOUSEHOLD_KINDS:
        errors["type"] = _("Choisissez famille ou structure.")
    if not cleaned or len(cleaned) > rules.MAX_NAME:
        errors["nom"] = _("Indiquez un nom, sans adresse (80 caractères au plus).")
    note = _text(notes, rules.MAX_NOTE, "notes", errors)
    if errors:
        raise FormErrors(errors)
    with database.session(write=True) as session:
        moment = now_iso()
        household = Household(
            public_id=new_public_id(),
            kind=kind,
            name=cleaned,
            name_norm=rules.name_norm(cleaned, ""),
            notes=note,
            version=1,
            created_at=moment,
            updated_at=moment,
        )
        session.add(household)
        session.flush()
        audit.record(session, "household.create", "household", household.id, user_id=user_id, details={"type": kind})
        return household.id


def update_household(
    database: Database, household_id: int, name: str, notes: str, expected_version: int | None, user_id: int | None
) -> None:
    errors: dict[str, str] = {}
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > rules.MAX_NAME:
        errors["nom"] = _("Indiquez un nom, sans adresse (80 caractères au plus).")
    note = _text(notes, rules.MAX_NOTE, "notes", errors)
    if errors:
        raise FormErrors(errors)
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        _check_version(household.version, expected_version)
        household.name, household.name_norm, household.notes = cleaned, rules.name_norm(cleaned, ""), note
        household.version += 1
        household.updated_at = now_iso()
        audit.record(
            session, "household.update", "household", household.id, user_id=user_id, details={"champs": ["nom"]}
        )


def set_household_contacts(
    database: Database, household_id: int, data: ContactsEdit, expected_version: int | None, user_id: int | None
) -> None:
    """Coordonnées partagées d'un foyer, ou professionnelles d'une structure (`view_personal` et `edit`)."""
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        _check_version(household.version, expected_version)
        review = Review()
        person = _Person()
        person.email, person.phone, person.address = _clean_contact_fields(
            data.email, data.phone, data.phone_territory, data.address, review
        )
        if review.errors:
            raise RegistrationRefused(review)
        moment = now_iso()
        session.execute(delete(Contact).where(Contact.household_id == household.id))
        filled = _add_contacts(session, {"household_id": household.id}, person, moment)
        household.version += 1
        household.updated_at = moment
        audit.record(
            session, "household.contacts", "household", household.id, user_id=user_id, details={"champs": filled}
        )


def _check_role(household: Household, patron: Patron, role: str) -> None:
    try:
        rules.check_member_role(household.kind, role)
    except PatronError as error:
        raise BusinessError(str(error)) from error
    if role == "referent" and patron.category != "adult":
        raise BusinessError(_("Un référent est un adulte."))


def add_member(database: Database, household_id: int, patron_id: int, role: str, user_id: int | None) -> None:
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        patron = _get_patron(session, patron_id)
        _living(patron)
        if household.archived_at is not None:
            raise BusinessError(_("Ce foyer est archivé."))
        role = role or _default_role(household.kind, patron.category)
        _check_role(household, patron, role)
        if session.get(HouseholdMember, (household.id, patron.id)) is not None:
            raise BusinessError(_("Ce lecteur fait déjà partie de ce foyer."))
        session.add(
            HouseholdMember(household_id=household.id, patron_id=patron.id, role=role, joined_on=now_iso()[:10])
        )
        household.version += 1
        audit.record(
            session,
            "patron.household_joined",
            "patron",
            patron.id,
            user_id=user_id,
            details={"foyer": household.id, "role": role},
        )


def change_member_role(database: Database, household_id: int, patron_id: int, role: str, user_id: int | None) -> None:
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        member = session.get(HouseholdMember, (household_id, patron_id))
        if member is None:
            raise BusinessError(_("Ce lecteur ne fait pas partie de ce foyer."))
        _check_role(household, _get_patron(session, patron_id), role)
        member.role = role
        household.version += 1
        audit.record(
            session,
            "patron.household_role",
            "patron",
            patron_id,
            user_id=user_id,
            details={"foyer": household.id, "role": role},
        )


def set_structure_right(
    database: Database, household_id: int, patron_id: int, allowed: bool, user_id: int | None
) -> None:
    """« Peut emprunter pour la structure » : explicite, lien par lien, jamais déduit du rôle de référent (D2 de L4)."""
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        member = session.get(HouseholdMember, (household_id, patron_id))
        if member is None:
            raise BusinessError(_("Ce lecteur ne fait pas partie de ce foyer."))
        if household.kind != "structure":
            raise BusinessError(_("Ce droit ne concerne que les structures."))
        if allowed and _get_patron(session, patron_id).category != "adult":
            raise BusinessError(_("Une structure emprunte par un adulte."))
        if bool(member.can_borrow_for_structure) == allowed:
            return
        member.can_borrow_for_structure = allowed
        household.version += 1
        audit.record(
            session,
            "patron.household_right",
            "patron",
            patron_id,
            user_id=user_id,
            details={"foyer": household.id, "emprunter": allowed},
        )


def remove_member(database: Database, household_id: int, patron_id: int, user_id: int | None) -> None:
    """Retire le lien, jamais la fiche. Un foyer resté vide est proposé à l'archivage, jamais supprimé (D4)."""
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        member = session.get(HouseholdMember, (household_id, patron_id))
        if member is None:
            raise BusinessError(_("Ce lecteur ne fait pas partie de ce foyer."))
        session.delete(member)
        household.version += 1
        audit.record(
            session, "patron.household_left", "patron", patron_id, user_id=user_id, details={"foyer": household.id}
        )


def set_household_archived(
    database: Database, household_id: int, archived: bool, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        household = _get_household(session, household_id)
        _check_version(household.version, expected_version)
        count = (
            session.scalar(
                select(func.count()).select_from(HouseholdMember).where(HouseholdMember.household_id == household.id)
            )
            or 0
        )
        if archived and count:
            raise BusinessError(_("Ce foyer a encore des membres : retirez-les d’abord."))
        household.archived_at = now_iso() if archived else None
        household.version += 1
        audit.record(
            session,
            "household.archive" if archived else "household.unarchive",
            "household",
            household.id,
            user_id=user_id,
        )


# --- Responsables : chaque droit est explicite (D5) -------------------------------------------------------------------
@dataclass(frozen=True)
class Rights:
    relation: str
    can_borrow_for: bool
    is_contact: bool
    contact_rank: int = 1
    can_borrow_alone: bool = False


def _rights_trace(guardian_id: int, rights: Rights) -> dict[str, object]:
    """Quel droit, pour quel lien : jamais une valeur personnelle."""
    return {
        "responsable": guardian_id,
        "emprunter": rights.can_borrow_for,
        "contacter": rights.is_contact,
        "seul": rights.can_borrow_alone,
    }


def _check_rights(rights: Rights) -> None:
    if rights.relation not in rules.RELATIONS:
        raise BusinessError(_("Choisissez la relation."))
    if not 1 <= rights.contact_rank <= 9:
        raise BusinessError(_("L’ordre de contact va de 1 à 9."))


def add_guardian(database: Database, minor_id: int, guardian_id: int, rights: Rights, user_id: int | None) -> None:
    _check_rights(rights)
    with database.session(write=True) as session:
        minor, guardian = _get_patron(session, minor_id), _get_patron(session, guardian_id)
        _living(minor)
        try:
            rules.check_guardianship(minor.category, guardian.category, minor.id == guardian.id, guardian.status)
        except PatronError as error:
            raise BusinessError(str(error)) from error
        if session.get(Guardianship, (minor.id, guardian.id)) is not None:
            raise BusinessError(_("Cet adulte est déjà responsable de ce mineur."))
        moment = now_iso()
        session.add(
            Guardianship(
                minor_id=minor.id,
                guardian_id=guardian.id,
                relation=rights.relation,
                can_borrow_for=rights.can_borrow_for,
                is_contact=rights.is_contact,
                can_borrow_alone=rights.can_borrow_alone,
                contact_rank=rights.contact_rank,
                version=1,
                created_at=moment,
                updated_at=moment,
            )
        )
        audit.record(
            session,
            "patron.guardian_added",
            "patron",
            minor.id,
            user_id=user_id,
            details=_rights_trace(guardian.id, rights),
        )


def update_guardian(
    database: Database,
    minor_id: int,
    guardian_id: int,
    rights: Rights,
    expected_version: int | None,
    user_id: int | None,
) -> None:
    _check_rights(rights)
    with database.session(write=True) as session:
        link = session.get(Guardianship, (minor_id, guardian_id))
        if link is None:
            raise BusinessError(_("Ce lien de responsabilité n’existe plus."))
        _check_version(link.version, expected_version)
        link.relation, link.can_borrow_for, link.is_contact = rights.relation, rights.can_borrow_for, rights.is_contact
        link.contact_rank, link.can_borrow_alone = rights.contact_rank, rights.can_borrow_alone
        link.version += 1
        link.updated_at = now_iso()
        audit.record(
            session,
            "patron.guardian_updated",
            "patron",
            minor_id,
            user_id=user_id,
            details=_rights_trace(guardian_id, rights),
        )


def remove_guardian(database: Database, minor_id: int, guardian_id: int, user_id: int | None) -> None:
    with database.session(write=True) as session:
        link = session.get(Guardianship, (minor_id, guardian_id))
        if link is None:
            raise BusinessError(_("Ce lien de responsabilité n’existe plus."))
        session.delete(link)
        audit.record(
            session,
            "patron.guardian_removed",
            "patron",
            minor_id,
            user_id=user_id,
            details={"responsable": guardian_id},
        )


# --- Conservation et anonymisation (`patrons.anonymize`, D12, P2) -----------------------------------------------------
def place_hold(database: Database, patron_id: int, reason: str, until: str, user_id: int | None) -> None:
    errors: dict[str, str] = {}
    cleaned = " ".join((reason or "").split())
    if not cleaned or len(cleaned) > rules.MAX_SHORT:
        errors["motif"] = _("Indiquez un motif court, sans donnée personnelle.")
    end = _parse_date(until, "jusqu_au", errors) if until.strip() else None
    if errors:
        raise FormErrors(errors)
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        _living(patron)
        session.add(PatronHold(patron_id=patron.id, reason=cleaned, until=end, placed_at=now_iso(), placed_by=user_id))
        audit.record(session, "patron.hold", "patron", patron.id, user_id=user_id, details={"jusqu_au": end})


def lift_hold(database: Database, hold_id: int, user_id: int | None) -> int:
    with database.session(write=True) as session:
        hold = session.get(PatronHold, hold_id)
        if hold is None or hold.lifted_at is not None:
            raise BusinessError(_("Cette mise en conservation est déjà levée."))
        hold.lifted_at, hold.lifted_by = now_iso(), user_id
        audit.record(session, "patron.hold_lifted", "patron", hold.patron_id, user_id=user_id)
        return hold.patron_id


def active_loans(session: Session, patron_id: int) -> int:
    """Prêts en cours dont cette personne est l'emprunteur ou le responsable. Un prêt annulé n'en est plus un."""
    return int(
        session.execute(
            text(
                "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL AND cancelled_at IS NULL"
                " AND (patron_id = :id OR responsible_id = :id)"
            ),
            {"id": patron_id},
        ).scalar()
        or 0
    )


@dataclass(frozen=True)
class AnonymizeInput:
    typed_number: str
    password: str
    reason: str
    reason_text: str = ""


@dataclass(frozen=True)
class AnonymizeResult:
    contacts: int
    households: int
    guardianships: int
    cards: int


def anonymize_blockers(session: Session, patron: Patron) -> list[str]:
    """Ce qui interdit ou diffère l'anonymisation : une mise en conservation active, un prêt en cours."""
    blockers = []
    holds = session.scalars(select(PatronHold).where(PatronHold.patron_id == patron.id))
    if any(repo.hold_is_active(hold, today()) for hold in holds):
        blockers.append(_("Une mise en conservation est active : levez-la d’abord, si elle n’a plus lieu d’être."))
    loans = active_loans(session, patron.id)
    if loans:
        blockers.append(_("Ce lecteur a {n} prêt(s) en cours.").format(n=loans))
    return blockers


def _count(result: object) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


ERASED_REASON = "effacé"
ERASED_HOUSEHOLD = _("Foyer anonymisé")
JOURNAL_LINKS = ("foyer", "responsable")


def _names_someone(text_value: str, patron: Patron) -> bool:
    """Le motif reste au journal : il ne doit pas réintroduire ce que l'opération efface."""
    typed = rules.name_norm(text_value, "")
    parts = [part for part in rules.name_norm(patron.first_name, patron.last_name).split() if len(part) > 2]
    words = set(typed.split())
    return any(part in words for part in parts) or "@" in text_value or sum(char.isdigit() for char in text_value) >= 6


@dataclass(frozen=True)
class AnonymizePreview:
    """Ce que l'écran annonce avant de confirmer. Aucun nombre de sauvegardes : aucun inventaire fiable ne l'établit."""

    contacts: int
    households: int
    guardianships: int
    cards: int
    blockers: list[str]
    exports: list[str]


def anonymize_preview(database: Database, patron_id: int) -> AnonymizePreview:
    with database.session() as session:
        patron = _get_patron(session, patron_id)

        def count(statement: Any) -> int:
            return session.scalar(select(func.count()).select_from(statement.subquery())) or 0

        exports = session.scalars(
            select(AuditEntry.at)
            .where(
                AuditEntry.entity_type == "patron",
                AuditEntry.entity_id == patron.id,
                AuditEntry.action == "patron.export",
            )
            .order_by(AuditEntry.id)
        )
        return AnonymizePreview(
            count(select(Contact.id).where(Contact.patron_id == patron.id)),
            count(select(HouseholdMember.household_id).where(HouseholdMember.patron_id == patron.id)),
            count(
                select(Guardianship.minor_id).where(
                    (Guardianship.minor_id == patron.id) | (Guardianship.guardian_id == patron.id)
                )
            ),
            count(select(PatronCard.id).where(PatronCard.patron_id == patron.id)),
            anonymize_blockers(session, patron),
            list(exports),
        )


def _neutralize_emptied_households(session: Session, household_ids: list[int], moment: str) -> int:
    """Un foyer resté sans membre garderait un nom de famille et une adresse : ils sont effacés, le foyer reste."""
    emptied = 0
    for household_id in household_ids:
        if session.scalar(
            select(func.count()).select_from(HouseholdMember).where(HouseholdMember.household_id == household_id)
        ):
            continue
        household = _get_household(session, household_id)
        session.execute(delete(Contact).where(Contact.household_id == household_id))
        household.name, household.name_norm, household.notes = (
            ERASED_HOUSEHOLD,
            rules.name_norm(ERASED_HOUSEHOLD, ""),
            None,
        )
        household.version += 1
        household.updated_at = moment
        emptied += 1
    return emptied


def _unlink_journal(session: Session, patron_id: int) -> None:
    """Les entrées passées restent, sans le foyer ni le responsable qui permettraient de remonter à la personne."""
    entries = session.scalars(
        select(AuditEntry).where(
            AuditEntry.entity_type == "patron", AuditEntry.entity_id == patron_id, AuditEntry.details.is_not(None)
        )
    )
    for entry in entries:
        data = json.loads(entry.details or "{}")
        if any(data.get(key) is not None for key in JOURNAL_LINKS):
            entry.details = json.dumps({**data, **dict.fromkeys(JOURNAL_LINKS)}, ensure_ascii=False)


def anonymize(database: Database, patron_id: int, data: AnonymizeInput, user_id: int | None) -> AnonymizeResult:
    """Irréversible (D12). Confirmée par le numéro permanent retapé, le mot de passe de l'administrateur et un motif.

    Aucune carte active n'est exigée. Ne garde que ce qui sert à la traçabilité et aux statistiques ; le journal ne
    reçoit aucune des valeurs effacées. Les sauvegardes antérieures ne sont pas modifiées.
    """
    with database.session(write=True) as session:
        patron = _get_patron(session, patron_id)
        if patron.status == "anonymized":
            raise BusinessError(_("Ce lecteur est déjà anonymisé."))
        blockers = anonymize_blockers(session, patron)
        if blockers:
            raise BusinessError(" ".join(blockers))
        errors: dict[str, str] = {}
        typed = rules.parse_patron_number(data.typed_number)
        if typed != patron.number:
            card = rules.normalize_card(data.typed_number)
            is_card = card is not None and session.scalar(
                select(func.count()).select_from(PatronCard).where(PatronCard.number == card)
            )
            errors["numero"] = (
                _("C’est un numéro de carte : retapez le numéro de lecteur permanent, {number}.").format(
                    number=patron.number
                )
                if is_card
                else _("Le numéro retapé ne correspond pas au numéro de lecteur permanent.")
            )
        if data.reason not in rules.ANONYMIZE_REASONS:
            errors["motif"] = _("Choisissez un motif.")
        elif data.reason == "other" and not data.reason_text.strip():
            errors["motif"] = _("Précisez le motif, sans donnée personnelle.")
        if len(data.reason_text.strip()) > rules.MAX_SHORT:
            errors["motif"] = _("Ce motif est trop long.")
        elif _names_someone(data.reason_text, patron):
            errors["motif"] = _("Le motif reste au journal : n’y écrivez ni nom, ni coordonnée, ni numéro.")
        user = session.get(User, user_id) if user_id is not None else None
        if user is None or not passwords.verify_password(user.password_hash, data.password):
            errors["mot_de_passe"] = _("Mot de passe incorrect.")
        if errors:
            raise FormErrors(errors)
        moment = now_iso()
        contacts = _count(session.execute(delete(Contact).where(Contact.patron_id == patron.id)))
        joined = list(
            session.scalars(select(HouseholdMember.household_id).where(HouseholdMember.patron_id == patron.id))
        )
        memberships = _count(session.execute(delete(HouseholdMember).where(HouseholdMember.patron_id == patron.id)))
        emptied = _neutralize_emptied_households(session, joined, moment)
        _unlink_journal(session, patron.id)
        # Les prêts terminés restent, reliés à la fiche anonymisée ; ils ne disent plus qui en a répondu (D14)
        session.execute(text("UPDATE loans SET responsible_id = NULL WHERE responsible_id = :id"), {"id": patron.id})
        links = _count(
            session.execute(
                delete(Guardianship).where(
                    (Guardianship.minor_id == patron.id) | (Guardianship.guardian_id == patron.id)
                )
            )
        )
        session.execute(
            update(PatronCard)
            .where(PatronCard.patron_id == patron.id, PatronCard.ended_on.is_(None))
            .values(ended_on=moment[:10], end_reason="anonymized")
        )
        cards = _count(
            session.execute(update(PatronCard).where(PatronCard.patron_id == patron.id).values(patron_id=None))
        )
        session.execute(update(PatronHold).where(PatronHold.patron_id == patron.id).values(reason=ERASED_REASON))
        patron.first_name = patron.last_name = patron.name_norm = patron.sort_key = ""
        patron.birth = patron.notes = patron.membership_expires_on = None
        patron.status, patron.anonymized_at = "anonymized", moment
        _touch(patron, user_id, moment)
        result = AnonymizeResult(contacts, memberships, links, cards)
        audit.record(
            session,
            "patron.anonymize",
            "patron",
            patron.id,
            user_id=user_id,
            details={
                "motif": data.reason,
                "precision": data.reason_text.strip() or None,
                "coordonnees_effacees": contacts,
                "foyers_quittes": memberships,
                "responsabilites_retirees": links,
                "cartes_detachees": cards,
                "foyers_neutralises": emptied,
            },
        )
        return result


# --- Export individuel (`patrons.export`, D13) et affichage des coordonnées (D17) -------------------------------------
def export(
    database: Database, patron_id: int, user_id: int | None, visibility: repo.Visibility = repo.EVERYTHING
) -> dict[str, Any]:
    """Toutes les données de la personne, rien d'une autre : ses responsables sont cités, sans leurs coordonnées."""
    with database.session(write=True) as session:
        sheet = repo.sheet(session, patron_id, today(), visibility)
        if sheet is None:
            raise BusinessError(_("Ce lecteur n’existe pas."))
        patron = sheet.patron
        data: dict[str, Any] = {
            "format": "biblyo.lecteur.v1",
            "exporte_le": now_iso(),
            "lecteur": {
                "identifiant": patron.public_id,
                "numero": patron.number,
                "prenom": patron.first_name,
                "nom": patron.last_name,
                "categorie": rules.CATEGORIES[patron.category],
                "naissance": patron.birth,
                "statut": rules.STATUSES[patron.status],
                "inscrit_le": patron.registered_on,
                "adhesion_jusqu_au": patron.membership_expires_on,
                "observations": patron.notes,
            },
            "cartes": [
                {
                    "numero": card.number,
                    "remise_le": card.issued_on,
                    "fin_le": card.ended_on,
                    "motif_fin": card.end_label,
                }
                for card in sheet.cards
            ],
            "coordonnees": [
                {"type": line.kind, "libelle": line.label_text, "valeur": line.display, "foyer": line.shared_by}
                for line in repo.contacts_of(session, patron.id)
            ],
            "foyers": [
                {"nom": item.name, "type": item.kind_label, "role": item.role_label, "depuis": item.joined_on}
                for item in sheet.households
            ],
            "responsables": [
                {
                    "numero": link.other_number,
                    "nom": link.other_name,
                    "relation": link.relation_label,
                    "peut_emprunter": link.can_borrow_for,
                    "peut_emprunter_seul": link.can_borrow_alone,
                    "a_contacter": link.is_contact,
                }
                for link in sheet.guardians
            ],
            "responsable_de": [
                {"numero": link.other_number, "nom": link.other_name, "relation": link.relation_label}
                for link in sheet.wards
            ],
            "conservations": [
                {"motif": hold.reason, "jusqu_au": hold.until, "pose_le": hold.placed_at, "levee_le": hold.lifted_at}
                for hold in sheet.holds
            ],
            "historique": [
                {"date": line.at, "action": line.action, "champs": line.fields}
                for line in repo.history(session, patron.id, limit=500)
            ],
        }
        audit.record(session, "patron.export", "patron", patron.id, user_id=user_id)
        return data


def record_search_limited(database: Database, user_id: int | None) -> None:
    """Trop de recherches de lecteurs en peu de temps : dit qui et quand, jamais ce qui était cherché."""
    with database.session(write=True) as session:
        audit.record(session, "patron.search_limited", "user", user_id, user_id=user_id)


def record_household_contacts_shown(database: Database, household_id: int, user_id: int | None) -> None:
    with database.session(write=True) as session:
        audit.record(session, "household.contacts_shown", "household", household_id, user_id=user_id)


def record_contacts_shown(database: Database, patron_id: int, user_id: int | None) -> None:
    """L'affichage des coordonnées à la demande est journalisé (D10, D17)."""
    with database.session(write=True) as session:
        audit.record(session, "patron.contacts_shown", "patron", patron_id, user_id=user_id)
