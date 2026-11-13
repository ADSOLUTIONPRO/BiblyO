# SPDX-License-Identifier: EUPL-1.2
"""Écritures des prêts et retours (plan de L4, D1 à D16). Tout passe ici, en une transaction, tout ou rien.

Chaque règle est revérifiée au moment d'écrire : ce que l'écran a montré peut avoir changé. Le journal ne reçoit que
des identifiants d'opération et des nombres : ni titre, ni lecteur, ni carte.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import from_iso, now, now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import TIMEZONE, _
from biblyo.core.settings_store import get_setting, set_setting
from biblyo.modules.catalogue.models import Copy, DocumentType
from biblyo.modules.circulation import repository as repo
from biblyo.modules.circulation import rules
from biblyo.modules.circulation.models import Loan, LoanEvent, LoanOperation, LoanReminder, LoanRule
from biblyo.modules.circulation.rules import CirculationError, Verdict
from biblyo.modules.holdings.models import Location
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.rules import french_date


def today() -> date:
    """La date à la bibliothèque : une échéance est un jour calendaire, sans heure."""
    return now().astimezone(TIMEZONE).date()


@dataclass(frozen=True)
class Actor:
    """Qui agit, et ce qu'il a le droit de faire. La route le calcule depuis la session, jamais depuis le formulaire."""

    user_id: int | None
    permissions: frozenset[str]


class LoanRefused(BusinessError):
    """Opération refusée : rien n'a été écrit. Chaque verdict dit pourquoi, par exemplaire quand c'est possible."""

    def __init__(self, general: Sequence[Verdict], by_copy: dict[int, list[Verdict]] | None = None) -> None:
        self.general = list(general)
        self.by_copy = by_copy or {}
        messages = [item.message for item in self.general]
        messages += [item.message for found in self.by_copy.values() for item in found]
        super().__init__(" ".join(dict.fromkeys(messages)))


@contextmanager
def _business_rules() -> Iterator[None]:
    """Une règle pure refusée devient un refus métier ; un conflit en base, l'interdiction R12 : rien n'est écrit."""
    try:
        yield
    except CirculationError as error:
        raise BusinessError(str(error)) from error
    except IntegrityError as error:
        raise LoanRefused([rules.verdict("R12")]) from error


def _require(actor: Actor, *permissions: str) -> None:
    if not actor.permissions & set(permissions):
        raise BusinessError(_("Votre rôle ne permet pas cette opération."))


def _get_loan(session: Session, loan_id: int) -> Loan:
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise BusinessError(_("Ce prêt n’existe pas."))
    return loan


def _check_version(loan: Loan, expected: int | None) -> None:
    if expected is not None and loan.version != expected:
        raise LoanRefused([rules.verdict("R12")])


def _is_active(loan: Loan) -> bool:
    return loan.returned_at is None and loan.cancelled_at is None


def _event(
    session: Session,
    loan: Loan,
    kind: str,
    moment: str,
    actor: Actor,
    *,
    reason: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    session.add(
        LoanEvent(
            loan_id=loan.id,
            kind=kind,
            at=moment,
            user_id=actor.user_id,
            reason_code=reason,
            details=json.dumps(details, ensure_ascii=False) if details else None,
        )
    )


def _operation(session: Session, kind: str, moment: str, actor: Actor) -> LoanOperation:
    operation = LoanOperation(kind=kind, created_at=moment, user_id=actor.user_id)
    session.add(operation)
    session.flush()
    return operation


def _names(session: Session, *patron_ids: int | None) -> list[str]:
    """Les noms à refuser dans un motif, qui resterait au journal."""
    found = session.execute(
        select(Patron.first_name, Patron.last_name).where(Patron.id.in_([item for item in patron_ids if item]))
    ).all()
    return [f"{first} {last}" for first, last in found]


def _reason(
    session: Session, code: str | None, text: str | None, allowed: dict[str, str], *patron_ids: int | None
) -> tuple[str | None, str | None]:
    if not code:
        return None, None
    return rules.clean_reason(code, text, allowed, _names(session, *patron_ids))


def _override_details(reason_text: str | None, *codes: str) -> dict[str, object]:
    details: dict[str, object] = {"regles": list(codes)}
    if reason_text:
        details["motif"] = reason_text  # déjà vérifié : ni nom, ni coordonnée, ni numéro
    return details


# --- Prêter (D1, D2, D3, D6, D8) --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LendItem:
    copy_id: int
    due_on: str | None = None


@dataclass(frozen=True)
class LendRequest:
    patron_id: int
    items: tuple[LendItem, ...]
    mode: str
    guardian_id: int | None = None
    household_id: int | None = None
    loaned_on: str | None = None
    reason_code: str | None = None
    reason_text: str | None = None


@dataclass(frozen=True)
class LendResult:
    operation_id: int
    loan_ids: tuple[int, ...]
    due_dates: dict[int, str]
    warnings: tuple[Verdict, ...] = ()


def _parse_day(value: str, message: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CirculationError(message) from None


@dataclass
class _Plan:
    general: list[Verdict] = field(default_factory=list)
    by_copy: dict[int, list[Verdict]] = field(default_factory=dict)
    due: dict[int, str] = field(default_factory=dict)
    rule_due: dict[int, str] = field(default_factory=dict)  # l'échéance de la règle, avant tout choix
    maximums: list[int | None] = field(default_factory=list)


def _plan_items(
    session: Session, request: LendRequest, category: str, loaned_on: date, current: date, plan: _Plan
) -> None:
    all_rules = repo.loan_rules(session)
    closed = repo.closed_weekday(session)
    for item in request.items:
        copy = session.get(Copy, item.copy_id)
        if copy is None:
            plan.by_copy[item.copy_id] = [rules.verdict("R4")]
            continue
        found = rules.copy_verdicts(repo.copy_facts(session, copy))
        rule = rules.pick_rule(all_rules, repo.document_type_of(session, copy), category)
        rule_due = rules.due_date(loaned_on, rule.loan_days)
        chosen = _parse_day(item.due_on, _("Cette date de retour n’existe pas.")) if item.due_on else rule_due
        rules.check_dates(loaned_on, chosen)
        found += rules.date_verdicts(loaned_on, chosen, rule_due, current, closed)
        plan.by_copy[item.copy_id] = found
        plan.due[item.copy_id] = chosen.isoformat()
        plan.rule_due[item.copy_id] = rule_due.isoformat()
        plan.maximums.append(rule.max_active_loans)


def _lowest(maximums: Sequence[int | None]) -> int | None:
    known = [item for item in maximums if item is not None]
    return min(known) if known else None


def _loaned_day(request: LendRequest, current: date) -> date:
    if not request.loaned_on:
        return current
    loaned_on = _parse_day(request.loaned_on, _("Cette date de prêt n’existe pas."))
    if loaned_on > current:
        raise CirculationError(_("Une date de prêt ne peut pas être dans le futur."))
    return loaned_on


def _evaluate(
    session: Session, request: LendRequest, patron: Patron, loaned_on: date, current: date
) -> tuple[rules.Borrowing | None, _Plan]:
    """Tous les verdicts d'une demande, sans rien écrire : le même calcul pour l'écran et pour la validation."""
    facts = repo.borrower_facts(session, patron, current)
    structure = repo.structure_facts(session, request.household_id, patron.id)
    borrowing, frame = rules.decide_borrowing(
        facts, request.mode, chosen_guardian=request.guardian_id, structure=structure
    )
    facts = repo.with_responsible(session, facts, borrowing.responsible_id if borrowing else None, current)
    plan = _Plan(general=rules.borrower_verdicts(facts, current, repo.overdue_block(session)) + frame)
    category = rules.rule_category(patron.category, request.mode)
    _plan_items(session, request, category, loaned_on, current, plan)
    household = borrowing.household_id if borrowing else None
    plan.general += rules.limit_verdicts(
        repo.active_count(session, patron.id, household),
        len(request.items),
        _lowest(plan.maximums),
        repo.limit_mode(session),
    )
    return borrowing, plan


@dataclass(frozen=True)
class Preview:
    borrowing: rules.Borrowing | None
    general: tuple[Verdict, ...]
    by_copy: dict[int, list[Verdict]]
    due: dict[int, str]
    rule_due: dict[int, str] = field(default_factory=dict)


def preview(session: Session, request: LendRequest) -> Preview:
    """Ce que le comptoir montre avant de valider. Rien n'est écrit, et tout sera revérifié à la validation."""
    with _business_rules():
        patron = session.get(Patron, request.patron_id)
        if patron is None:
            raise LoanRefused([rules.verdict("R4")])
        current = today()
        borrowing, plan = _evaluate(session, request, patron, _loaned_day(request, current), current)
        return Preview(borrowing, tuple(plan.general), plan.by_copy, plan.due, plan.rule_due)


def lend(database: Database, request: LendRequest, actor: Actor) -> LendResult:
    """Plusieurs exemplaires, une opération, **tout ou rien** (D6). Un refus ne laisse aucune trace écrite."""
    _require(actor, rules.LEND)
    copy_ids = [item.copy_id for item in request.items]
    if not copy_ids:
        raise BusinessError(_("Aucun exemplaire à prêter."))
    if len(set(copy_ids)) != len(copy_ids):
        raise BusinessError(_("Un même exemplaire figure deux fois dans la liste."))
    with _business_rules(), database.session(write=True) as session:
        patron = session.get(Patron, request.patron_id)
        if patron is None:
            raise LoanRefused([rules.verdict("R4")])
        current = today()
        loaned_on = _loaned_day(request, current)
        borrowing, plan = _evaluate(session, request, patron, loaned_on, current)
        code, reason_text = _reason(
            session, request.reason_code, request.reason_text, rules.OVERRIDE_REASONS, patron.id, request.guardian_id
        )
        every = plan.general + [item for found in plan.by_copy.values() for item in found]
        refused = rules.refusals(every, actor.permissions, has_reason=code is not None)
        if refused or borrowing is None:
            raise LoanRefused(
                [item for item in plan.general if item in refused],
                {
                    key: kept
                    for key, found in plan.by_copy.items()
                    if (kept := [item for item in found if item in refused])
                },
            )
        moment = now_iso()
        operation = _operation(session, "lend", moment, actor)
        loaned_at = moment if loaned_on == current else f"{loaned_on.isoformat()}T12:00:00Z"
        loans: list[Loan] = []
        for copy_id in copy_ids:
            loan = Loan(
                operation_id=operation.id,
                copy_id=copy_id,
                patron_id=patron.id,
                responsible_id=borrowing.responsible_id,
                household_id=borrowing.household_id,
                borrow_mode=borrowing.mode,
                loaned_at=loaned_at,
                due_on=plan.due[copy_id],
                loaned_by=actor.user_id,
            )
            session.add(loan)
            loans.append(loan)
        session.flush()
        for loan in loans:
            _event(session, loan, "created", moment, actor, details={"operation": operation.id})
            applied = rules.overridable(plan.general + plan.by_copy[loan.copy_id])
            if applied:
                details = _override_details(reason_text, *(item.code for item in applied))
                _event(session, loan, "override", moment, actor, reason=code, details=details)
        audit.record(
            session,
            "loan.lend",
            "loan_operation",
            operation.id,
            user_id=actor.user_id,
            details={"prets": len(loans), "derogations": bool(rules.overridable(every))},
        )
        cautions = tuple(rules.warnings(every))
        return LendResult(
            operation.id, tuple(loan.id for loan in loans), {loan.copy_id: loan.due_on for loan in loans}, cautions
        )


# --- Rendre (D10, R25) ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReturnResult:
    copy_id: int
    loan_id: int | None
    operation_id: int | None
    days_overdue: int = 0
    location_path: str | None = None
    shelf_position: int | None = None
    verdict: Verdict | None = None


def return_copy(
    database: Database,
    copy_id: int,
    actor: Actor,
    *,
    expected_version: int | None = None,
    expected_loan: int | None = None,
) -> ReturnResult:
    """Un lecteur suspendu peut toujours rendre. L'exemplaire n'a pas quitté sa place : le retour dit où il va (D10).

    `expected_loan` : le prêt que la personne a vu à l'écran. S'il a déjà été rendu, rien n'est fait ; si l'exemplaire
    est reparti entre-temps dans un autre prêt, celui-ci n'est jamais rendu à sa place (R12)."""
    _require(actor, rules.LEND)
    with _business_rules(), database.session(write=True) as session:
        copy = session.get(Copy, copy_id)
        if copy is None:
            raise LoanRefused([rules.verdict("R4")])
        path = (
            session.scalar(select(Location.path).where(Location.id == copy.location_id)) if copy.location_id else None
        )
        loan = repo.active_loan(session, copy_id)
        if loan is not None and expected_loan is not None and loan.id != expected_loan:
            raise LoanRefused([rules.verdict("R12")])
        if loan is None:
            return ReturnResult(
                copy_id,
                None,
                None,
                location_path=path,
                shelf_position=copy.shelf_position,
                verdict=rules.nothing_to_return(),
            )
        _check_version(loan, expected_version)
        moment = now_iso()
        operation = _operation(session, "return", moment, actor)
        late = rules.days_overdue(date.fromisoformat(loan.due_on), today())
        loan.returned_at, loan.returned_by = max(moment, loan.loaned_at), actor.user_id
        loan.version += 1
        _event(session, loan, "returned", moment, actor, details={"operation": operation.id, "retard": late})
        audit.record(
            session, "loan.return", "loan_operation", operation.id, user_id=actor.user_id, details={"retard": late}
        )
        return ReturnResult(copy_id, loan.id, operation.id, late, path, copy.shelf_position)


# --- Renouveler (D7, R15, R23) ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RenewResult:
    loan_id: int
    due_on: str
    warnings: tuple[Verdict, ...] = ()


def _rule_of(session: Session, loan: Loan, patron: Patron) -> rules.LoanRule:
    copy = session.get(Copy, loan.copy_id)
    document_type = repo.document_type_of(session, copy) if copy is not None else None
    category = rules.rule_category(patron.category, loan.borrow_mode)
    return rules.pick_rule(repo.loan_rules(session), document_type, category)


def renew(
    database: Database,
    loan_id: int,
    actor: Actor,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
    expected_version: int | None = None,
) -> RenewResult:
    _require(actor, rules.RENEW)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        if not _is_active(loan):
            raise BusinessError(_("Ce prêt est terminé : il ne se renouvelle plus."))
        _check_version(loan, expected_version)
        patron = session.get(Patron, loan.patron_id)
        assert patron is not None  # clé étrangère RESTRICT
        current = today()
        rule = _rule_of(session, loan, patron)
        due = date.fromisoformat(loan.due_on)
        found = rules.renewal_verdicts(loan.renewals, rule.max_renewals, due, current)
        code, text_value = _reason(session, reason_code, reason_text, rules.OVERRIDE_REASONS, loan.patron_id)
        if refused := rules.refusals(found, actor.permissions, has_reason=code is not None):
            raise LoanRefused(refused)
        moment = now_iso()
        operation = _operation(session, "renew", moment, actor)
        previous, loan.due_on = loan.due_on, rules.renewed_due(due, current, rule.renewal_days).isoformat()
        loan.renewals += 1
        loan.version += 1
        details = {"operation": operation.id, "avant": previous, "apres": loan.due_on}
        _event(session, loan, "renewed", moment, actor, details=details)
        if rules.overridable(found):
            _event(session, loan, "override", moment, actor, reason=code, details=_override_details(text_value, "R15"))
        audit.record(session, "loan.renew", "loan_operation", operation.id, user_id=actor.user_id)
        return RenewResult(loan.id, loan.due_on, tuple(rules.warnings(found)))


# --- Annuler (D9, R26) ------------------------------------------------------------------------------------------------
def _later_loan(session: Session, loan: Loan) -> bool:
    return bool(session.scalar(select(exists().where(Loan.copy_id == loan.copy_id, Loan.id > loan.id))))


def _reminded(session: Session, loan_id: int) -> bool:
    return bool(session.scalar(select(exists().where(LoanReminder.loan_id == loan_id))))


def _elapsed(operation: LoanOperation, actor: Actor) -> tuple[timedelta, bool]:
    return now() - from_iso(operation.created_at), operation.user_id == actor.user_id and actor.user_id is not None


def _decide_cancel(
    operation: LoanOperation,
    actor: Actor,
    code: str | None,
    text_value: str | None,
    **dependent: bool,
) -> tuple[str | None, str | None]:
    if operation.cancelled_at is not None:
        raise BusinessError(_("Cette opération est déjà annulée."))
    elapsed, own = _elapsed(operation, actor)
    decision = rules.cancel_decision(rules.CancelFacts(elapsed, own, **dependent), actor.permissions)
    if not decision.allowed:
        assert decision.verdict is not None
        raise LoanRefused([decision.verdict])
    if decision.needs_reason and code is None:
        raise BusinessError(_("Indiquez le motif de cette annulation."))
    return (code, text_value) if decision.needs_reason else (None, None)


def _close_operation(operation: LoanOperation, moment: str, actor: Actor) -> None:
    operation.cancelled_at, operation.cancelled_by = moment, actor.user_id


def _cancel_details(operation: LoanOperation, text_value: str | None, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {"operation": operation.id, **extra}
    if text_value:
        details["motif"] = text_value
    return details


def cancel_lend(
    database: Database,
    operation_id: int,
    actor: Actor,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
) -> int:
    """Annule tout un prêt groupé. **Rien n'est supprimé** : la ligne reste, marquée annulée, avec son événement."""
    _require(actor, rules.LEND, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        operation = session.get(LoanOperation, operation_id)
        if operation is None or operation.kind != "lend":
            raise BusinessError(_("Cette opération n’existe pas."))
        loans = list(session.scalars(select(Loan).where(Loan.operation_id == operation.id)))
        code, text_value = _reason(
            session, reason_code, reason_text, rules.CANCEL_REASONS, *(i.patron_id for i in loans)
        )
        code, text_value = _decide_cancel(
            operation,
            actor,
            code,
            text_value,
            relent=any(_later_loan(session, loan) for loan in loans),
            returned=any(loan.returned_at is not None for loan in loans),
            renewed=any(loan.renewals > 0 for loan in loans),
            reminded=any(_reminded(session, loan.id) for loan in loans),
        )
        moment = now_iso()
        _close_operation(operation, moment, actor)
        for loan in loans:
            loan.cancelled_at = moment
            loan.version += 1
            _event(
                session, loan, "cancelled", moment, actor, reason=code, details=_cancel_details(operation, text_value)
            )
        audit.record(session, "loan.cancel", "loan_operation", operation.id, user_id=actor.user_id)
        return len(loans)


def _last_event(session: Session, loan_id: int, kind: str) -> LoanEvent | None:
    return session.scalar(
        select(LoanEvent)
        .where(LoanEvent.loan_id == loan_id, LoanEvent.kind == kind)
        .order_by(LoanEvent.id.desc())
        .limit(1)
    )


def _operation_of(session: Session, event: LoanEvent | None) -> LoanOperation:
    data = json.loads(event.details or "{}") if event is not None else {}
    operation = session.get(LoanOperation, data.get("operation", 0))
    if operation is None:
        raise BusinessError(_("Cette opération n’existe pas."))
    return operation


def cancel_return(
    database: Database,
    loan_id: int,
    actor: Actor,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
) -> None:
    """Le livre rendu par erreur repart en prêt, avec son échéance. Refusé si l'exemplaire a repris une autre vie."""
    _require(actor, rules.LEND, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        if loan.returned_at is None or loan.cancelled_at is not None:
            raise BusinessError(_("Ce prêt n’a pas de retour à annuler."))
        operation = _operation_of(session, _last_event(session, loan.id, "returned"))
        code, text_value = _reason(session, reason_code, reason_text, rules.CANCEL_REASONS, loan.patron_id)
        copy = session.get(Copy, loan.copy_id)
        patron = session.get(Patron, loan.patron_id)
        needs_responsible = (
            loan.borrow_mode in (rules.MODE_WITH_ADULT, rules.MODE_ALONE) and loan.responsible_id is None
        )
        changed = copy is None or copy.status != "available" or patron is None or patron.status == "anonymized"
        code, text_value = _decide_cancel(
            operation,
            actor,
            code,
            text_value,
            relent=_later_loan(session, loan) or changed or needs_responsible,
        )
        moment = now_iso()
        _close_operation(operation, moment, actor)
        loan.returned_at, loan.returned_by = None, None
        loan.version += 1
        _event(session, loan, "cancelled", moment, actor, reason=code, details=_cancel_details(operation, text_value))
        audit.record(session, "loan.cancel", "loan_operation", operation.id, user_id=actor.user_id)


def cancel_renewal(
    database: Database,
    loan_id: int,
    actor: Actor,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
) -> None:
    _require(actor, rules.RENEW, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        event = _last_event(session, loan.id, "renewed")
        operation = _operation_of(session, event)
        assert event is not None
        code, text_value = _reason(session, reason_code, reason_text, rules.CANCEL_REASONS, loan.patron_id)
        data = json.loads(event.details or "{}")
        reminded = bool(
            session.scalar(select(exists().where(LoanReminder.loan_id == loan.id, LoanReminder.created_at >= event.at)))
        )
        code, text_value = _decide_cancel(
            operation,
            actor,
            code,
            text_value,
            returned=not _is_active(loan),
            relent=loan.due_on != data.get("apres"),
            reminded=reminded,
        )
        moment = now_iso()
        _close_operation(operation, moment, actor)
        loan.due_on = str(data["avant"])
        loan.renewals -= 1
        loan.version += 1
        _event(session, loan, "cancelled", moment, actor, reason=code, details=_cancel_details(operation, text_value))
        audit.record(session, "loan.cancel", "loan_operation", operation.id, user_id=actor.user_id)


# --- Corriger (D8, D9) : la trace reste, rien n'est effacé ------------------------------------------------------------
def change_due(
    database: Database,
    loan_id: int,
    new_due: str,
    actor: Actor,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
    expected_version: int | None = None,
) -> str:
    """Raccourcir est libre pour qui peut prêter ; allonger, ou corriger une erreur, demande une dérogation (D8)."""
    _require(actor, rules.LEND, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        if not _is_active(loan):
            raise BusinessError(_("Ce prêt est terminé : son échéance ne change plus."))
        _check_version(loan, expected_version)
        chosen = _parse_day(new_due, _("Cette date de retour n’existe pas."))
        rules.check_dates(date.fromisoformat(loan.loaned_at[:10]), chosen)
        if new_due == loan.due_on:
            return loan.due_on
        longer = chosen > date.fromisoformat(loan.due_on)
        code, text_value = _reason(session, reason_code, reason_text, rules.CORRECTION_REASONS, loan.patron_id)
        if longer:
            found = [rules.verdict("R17", due=french_date(loan.due_on))]
            if refused := rules.refusals(found, actor.permissions, has_reason=code is not None):
                raise LoanRefused(refused)
        moment = now_iso()
        previous, loan.due_on = loan.due_on, chosen.isoformat()
        loan.version += 1
        details: dict[str, object] = {"avant": previous, "apres": loan.due_on}
        if text_value:
            details["motif"] = text_value
        _event(session, loan, "due_changed", moment, actor, reason=code, details=details)
        audit.record(session, "loan.due_changed", "loan", loan.id, user_id=actor.user_id, details={"allonge": longer})
        return loan.due_on


def correct_return_date(
    database: Database,
    loan_id: int,
    returned_on: str,
    actor: Actor,
    *,
    reason_code: str | None,
    reason_text: str | None = None,
) -> None:
    _require(actor, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        if loan.returned_at is None or loan.cancelled_at is not None:
            raise BusinessError(_("Ce prêt n’a pas de retour à corriger."))
        code, text_value = _reason(session, reason_code, reason_text, rules.CORRECTION_REASONS, loan.patron_id)
        if code is None:
            raise BusinessError(_("Indiquez le motif de cette correction."))
        chosen = _parse_day(returned_on, _("Cette date de retour n’existe pas."))
        new_value = f"{chosen.isoformat()}T12:00:00Z"
        if chosen > today() or new_value < loan.loaned_at:
            raise CirculationError(_("Cette date de retour n’est pas possible pour ce prêt."))
        moment = now_iso()
        previous, loan.returned_at = loan.returned_at, new_value
        loan.version += 1
        details: dict[str, object] = {"champ": "retour", "avant": previous[:10], "apres": chosen.isoformat()}
        if text_value:
            details["motif"] = text_value
        _event(session, loan, "corrected", moment, actor, reason=code, details=details)
        audit.record(session, "loan.corrected", "loan", loan.id, user_id=actor.user_id)


def correct_borrower(
    database: Database,
    loan_id: int,
    request: LendRequest,
    actor: Actor,
    *,
    expected_version: int | None = None,
) -> int:
    """Mauvais lecteur ou responsable : le prêt fautif est clos par `corrected`, un prêt lié le remplace (D9)."""
    _require(actor, rules.OVERRIDE)
    with _business_rules(), database.session(write=True) as session:
        loan = _get_loan(session, loan_id)
        if not _is_active(loan):
            raise BusinessError(_("Ce prêt est terminé : il ne se corrige plus ainsi."))
        _check_version(loan, expected_version)
        patron = session.get(Patron, request.patron_id)
        if patron is None:
            raise LoanRefused([rules.verdict("R4")])
        code, text_value = _reason(
            session,
            request.reason_code,
            request.reason_text,
            rules.CORRECTION_REASONS,
            loan.patron_id,
            request.patron_id,
            request.guardian_id,
        )
        if code is None:
            raise BusinessError(_("Indiquez le motif de cette correction."))
        current = today()
        facts = repo.borrower_facts(session, patron, current)
        structure = repo.structure_facts(session, request.household_id, patron.id)
        borrowing, frame = rules.decide_borrowing(
            facts, request.mode, chosen_guardian=request.guardian_id, structure=structure
        )
        found = rules.borrower_verdicts(facts, current, repo.overdue_block(session)) + frame
        if rules.forbidden(found) or borrowing is None:
            raise LoanRefused(rules.forbidden(found))
        moment = now_iso()
        operation = _operation(session, "lend", moment, actor)
        loan.cancelled_at = moment
        loan.version += 1
        session.flush()
        replacement = Loan(
            operation_id=operation.id,
            copy_id=loan.copy_id,
            patron_id=patron.id,
            responsible_id=borrowing.responsible_id,
            household_id=borrowing.household_id,
            borrow_mode=borrowing.mode,
            loaned_at=loan.loaned_at,
            due_on=loan.due_on,
            renewals=loan.renewals,
            loaned_by=loan.loaned_by,
        )
        session.add(replacement)
        session.flush()
        details: dict[str, object] = {"remplace_par": replacement.id}
        if text_value:
            details["motif"] = text_value
        _event(session, loan, "corrected", moment, actor, reason=code, details=details)
        _event(session, replacement, "created", moment, actor, details={"corrige": loan.id, "operation": operation.id})
        audit.record(
            session, "loan.corrected", "loan", loan.id, user_id=actor.user_id, details={"remplace_par": replacement.id}
        )
        return replacement.id


# --- Rappels (D15) : valeurs structurées, aucun envoi -----------------------------------------------------------------
def note_reminder(
    database: Database,
    loan_id: int,
    channel: str,
    result: str,
    actor: Actor,
    *,
    reminded_on: str | None = None,
) -> int:
    """Note un rappel fait **hors de biblyO**. Rien n'est envoyé, et aucun champ de texte n'existe."""
    _require(actor, rules.HISTORY)
    with _business_rules(), database.session(write=True) as session:
        rules.check_reminder(channel, result)
        loan = _get_loan(session, loan_id)
        if not _is_active(loan):
            raise BusinessError(_("Ce prêt est terminé : il n’y a plus de rappel à noter."))
        day = _parse_day(reminded_on, _("Cette date n’existe pas.")) if reminded_on else today()
        if day > today():
            raise CirculationError(_("Un rappel ne peut pas être daté du futur."))
        reminder = LoanReminder(
            loan_id=loan.id,
            reminded_on=day.isoformat(),
            channel=channel,
            result=result,
            user_id=actor.user_id,
            created_at=now_iso(),
        )
        session.add(reminder)
        session.flush()
        audit.record(
            session,
            "loan.reminder",
            "loan",
            loan.id,
            user_id=actor.user_id,
            details={"canal": channel, "resultat": result},
        )
        return reminder.id


# --- Réglages (`circulation.settings`) : durées, plafonds, seuils, hypothèse de conservation ------------------------
@dataclass(frozen=True)
class RuleValues:
    loan_days: str
    renewal_days: str
    max_renewals: str
    max_active_loans: str


@dataclass(frozen=True)
class SettingsInput:
    rules: dict[int, RuleValues]
    limit_mode: str
    overdue_block: str
    closed_weekday: str
    retention_months: str


def _parsed_rule(values: RuleValues) -> tuple[int, int, int, int | None]:
    return rules.parse_rule(values.loan_days, values.renewal_days, values.max_renewals, values.max_active_loans)


def update_settings(database: Database, data: SettingsInput, actor: Actor) -> None:
    """Tout ou rien : une seule valeur refusée, et rien n'est enregistré. Le journal dit quoi, jamais les valeurs."""
    _require(actor, rules.SETTINGS)
    errors: dict[str, str] = {}
    parsed: dict[int, tuple[int, int, int, int | None]] = {}
    for rule_id, values in data.rules.items():
        try:
            parsed[rule_id] = _parsed_rule(values)
        except CirculationError as error:
            errors[f"regle_{rule_id}"] = str(error)
    if data.limit_mode not in rules.LIMIT_MODES:
        errors["mode_plafond"] = _("Choisissez un mode.")
    try:
        block = rules.parse_bound(data.overdue_block, 1, rules.MAX_OVERDUE_BLOCK, optional=True)
    except CirculationError as error:
        errors["seuil_retards"] = str(error)
    if data.closed_weekday not in ("", *(code for code, _label in rules.WEEKDAYS)):
        errors["fermeture"] = _("Choisissez un jour.")
    try:
        months = rules.parse_bound(data.retention_months, 1, rules.MAX_RETENTION_MONTHS)
    except CirculationError as error:
        errors["conservation_mois"] = str(error)
    if errors:
        raise FormErrors(errors)
    changed: list[str] = []
    with database.session(write=True) as session:
        for rule in session.scalars(select(LoanRule).where(LoanRule.id.in_(list(parsed)))):
            target = parsed[rule.id]
            current = (rule.loan_days, rule.renewal_days, rule.max_renewals, rule.max_active_loans)
            if current != target:
                rule.loan_days, rule.renewal_days, rule.max_renewals, rule.max_active_loans = target
                changed.append(f"regle_{rule.id}")
        wanted = {
            "circulation.limit_mode": data.limit_mode,
            "circulation.overdue_block": block,
            "circulation.closed_weekday": int(data.closed_weekday) if data.closed_weekday else None,
            "circulation.retention_months": months,
        }
        for key, value in wanted.items():
            if get_setting(session, key, None) != value:
                set_setting(session, key, value, actor.user_id)
                changed.append(key)
        if changed:
            audit.record(
                session, "circulation.settings", "loan_rule", None, user_id=actor.user_id, details={"champs": changed}
            )


def add_rule(database: Database, document_type: str, category: str, values: RuleValues, actor: Actor) -> int:
    """Une règle propre à un type de document, pour une catégorie ou pour toutes."""
    _require(actor, rules.SETTINGS)
    try:
        loan_days, renewal_days, renewals, maximum = _parsed_rule(values)
    except CirculationError as error:
        raise FormErrors({"ajout": str(error)}) from error
    if category and category not in rules.RULE_CATEGORIES:
        raise FormErrors({"ajout": _("Choisissez une catégorie.")})
    with database.session(write=True) as session:
        type_id = int(document_type) if document_type.isdecimal() and len(document_type) < 10 else None
        if type_id is None or session.get(DocumentType, type_id) is None:
            raise FormErrors({"ajout": _("Choisissez un type de document.")})
        same = select(LoanRule.id).where(
            LoanRule.document_type_id == type_id,
            LoanRule.patron_category.is_(None) if not category else LoanRule.patron_category == category,
        )
        if session.scalar(same) is not None:
            raise FormErrors({"ajout": _("Une règle existe déjà pour cette portée : modifiez-la dans le tableau.")})
        rule = LoanRule(
            document_type_id=type_id,
            patron_category=category or None,
            loan_days=loan_days,
            renewal_days=renewal_days,
            max_renewals=renewals,
            max_active_loans=maximum,
        )
        session.add(rule)
        session.flush()
        audit.record(session, "circulation.rule_added", "loan_rule", rule.id, user_id=actor.user_id)
        return rule.id


def delete_rule(database: Database, rule_id: int, actor: Actor) -> None:
    _require(actor, rules.SETTINGS)
    with database.session(write=True) as session:
        rule = session.get(LoanRule, rule_id)
        if rule is None:
            raise BusinessError(_("Cette règle n’existe plus."))
        if rule.document_type_id is None and rule.patron_category in repo.BASE_SCOPES:
            raise BusinessError(_("Les règles par catégorie restent : modifiez-les plutôt."))
        session.delete(rule)
        audit.record(session, "circulation.rule_removed", "loan_rule", rule_id, user_id=actor.user_id)
