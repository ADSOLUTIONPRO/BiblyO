# SPDX-License-Identifier: EUPL-1.2
"""Prêts et retours sur une vraie base migrée. Personnes inventées, courriels en example.org."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from biblyo.core.clock import set_clock
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import repository as repo
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest, LoanRefused
from biblyo.modules.holdings import service as holdings
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import AnonymizeInput, PersonInput, Registration

from ..factories import PASSWORD, make_notice, make_user

MOMENT = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
VOLUNTEER_RIGHTS = frozenset({rules.LEND, rules.RENEW})
LIBRARIAN_RIGHTS = VOLUNTEER_RIGHTS | {rules.OVERRIDE, rules.OVERRIDE_LIMIT, rules.HISTORY, rules.SETTINGS}


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


class Clock:
    def __init__(self) -> None:
        self.moment = MOMENT

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment += timedelta(**delta)


@pytest.fixture
def clock():
    instance = Clock()
    set_clock(instance)
    yield instance
    set_clock(None)


@pytest.fixture
def volunteer(settings, clock):
    return Actor(make_user(settings, "benevole", "volunteer"), VOLUNTEER_RIGHTS)


@pytest.fixture
def other_volunteer(settings, clock):
    return Actor(make_user(settings, "autre", "volunteer"), VOLUNTEER_RIGHTS)


@pytest.fixture
def librarian(settings, clock):
    return Actor(make_user(settings, "biblio", "librarian"), LIBRARIAN_RIGHTS)


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _copies(settings, database, count=1, title="Le Lotus bleu"):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return ids


def _adult(database, first="Paul", last="Martin", email="paul@example.org", **extra):
    person = PersonInput(first_name=first, last_name=last, category="adult", email=email, **extra)
    return patrons.register(database, Registration(person=person), None)


def _minor(database, first="Léa", last="Martin"):
    person = PersonInput(first_name=first, last_name=last, category="minor")
    return patrons.register(database, Registration(person=person), None)


def _link(settings, minor, adult, *, can_borrow_for=1, is_contact=1, alone=0, rank=1):
    _execute(
        settings,
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
        " can_borrow_alone, version, created_at, updated_at) VALUES (?, ?, 'parent', ?, ?, ?, ?, 1, 'x', 'x')",
        minor,
        adult,
        can_borrow_for,
        is_contact,
        rank,
        alone,
    )


def _lend(database, actor, patron, *copies, mode=rules.MODE_SELF, **options):
    items = tuple(LendItem(copy) for copy in copies)
    return service.lend(database, LendRequest(patron, items, mode, **options), actor)


def _codes(refusal):
    found = [item.code for item in refusal.general]
    return found + [item.code for items in refusal.by_copy.values() for item in items]


def _status(settings, loan_id):
    return _rows(settings, "SELECT returned_at, cancelled_at FROM loans WHERE id = ?", loan_id)[0]


def _events(settings, loan_id):
    return [row[0] for row in _rows(settings, "SELECT kind FROM loan_events WHERE loan_id = ? ORDER BY id", loan_id)]


def _count(settings, table):
    return _rows(settings, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608


# --- Prêter : qui utilise, qui répond ---------------------------------------------------------------------------------
def test_two_copies_of_a_notice_to_two_homonyms(settings, database, volunteer):
    first, second = _copies(settings, database, 2)
    anne = _adult(database, "Anne", "Durand", "a1@example.org")
    other = patrons.register(
        database,
        Registration(
            person=PersonInput(first_name="Anne", last_name="Durand", category="adult", email="a2@example.org"),
            homonyms_confirmed=True,
        ),
        None,
    )
    _lend(database, volunteer, anne, first)
    _lend(database, volunteer, other, second)
    assert sorted(_rows(settings, "SELECT copy_id, patron_id FROM loans")) == [(first, anne), (second, other)]


def test_a_copy_cannot_leave_twice(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    _lend(database, volunteer, _adult(database), copy)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _adult(database, "Marc", "Roy", "m@example.org"), copy)
    assert _codes(caught.value) == ["R1"]
    assert "Martin" not in caught.value.message and "Paul" not in caught.value.message  # jamais le détenteur


def test_two_counters_cannot_lend_the_same_copy(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    readers = [_adult(database, f"Lec{i}", "Teur", f"l{i}@example.org") for i in range(6)]
    barrier = threading.Barrier(len(readers))
    outcomes: list[str] = []

    def attempt(patron):
        barrier.wait()
        instance = Database(settings.database_path)
        try:
            _lend(instance, volunteer, patron, copy)
            outcomes.append("ok")
        except LoanRefused as error:
            outcomes.append(_codes(error)[0])
        finally:
            instance.dispose()

    threads = [threading.Thread(target=attempt, args=(item,)) for item in readers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1 and set(outcomes) <= {"ok", "R1", "R12"}
    assert _count(settings, "loans") == 1


def test_a_batch_is_all_or_nothing(settings, database, volunteer):
    good, taken = _copies(settings, database, 2)
    _lend(database, volunteer, _adult(database, "Marc", "Roy", "m@example.org"), taken)
    before = _count(settings, "loan_operations")
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _adult(database), good, taken)
    assert set(caught.value.by_copy) == {taken}
    assert _count(settings, "loan_operations") == before and _count(settings, "loans") == 1


def test_the_same_copy_twice_in_a_list_is_refused(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    with pytest.raises(BusinessError, match="deux fois"):
        _lend(database, volunteer, _adult(database), copy, copy)


def test_an_empty_list_is_refused(database, volunteer):
    with pytest.raises(BusinessError, match="Aucun"):
        service.lend(database, LendRequest(1, (), rules.MODE_SELF), volunteer)


@pytest.mark.parametrize("status", ["in_processing", "in_repair", "missing", "lost", "withdrawn"])
def test_a_copy_that_cannot_leave_is_refused(settings, database, volunteer, status):
    (copy,) = _copies(settings, database)
    _execute(settings, "UPDATE copies SET status = ? WHERE id = ?", status, copy)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _adult(database), copy)
    assert _codes(caught.value) == ["R3"]


def test_a_reference_only_copy_is_refused(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    _execute(settings, "UPDATE copies SET is_loanable = 0 WHERE id = ?", copy)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _adult(database), copy)
    assert _codes(caught.value) == ["R2"]


@pytest.mark.parametrize(("status", "code"), [("suspended", "R6"), ("archived", "R5")])
def test_a_withdrawn_or_suspended_reader_is_refused(settings, database, volunteer, status, code):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    _execute(settings, "UPDATE patrons SET status = ? WHERE id = ?", status, patron)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, patron, copy)
    assert _codes(caught.value) == [code]


def test_an_unknown_reader_or_copy(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, 9999, copy)
    assert _codes(caught.value) == ["R4"]
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _adult(database), 9999)
    assert _codes(caught.value) == ["R4"]


def test_due_date_follows_the_rule_of_the_category(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    result = _lend(database, volunteer, _adult(database), copy)
    assert result.due_dates == {copy: "2026-12-07"}  # 28 jours
    assert _events(settings, result.loan_ids[0]) == ["created"]


# --- Mineurs (D1, P1) -------------------------------------------------------------------------------------------------
def test_minor_with_an_authorized_and_reachable_adult(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent)
    result = _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=parent)
    assert result.due_dates == {copy: "2026-11-30"}  # 21 jours
    assert _rows(settings, "SELECT responsible_id, borrow_mode FROM loans") == [(parent, "with_adult")]


def test_minor_without_any_guardian(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, _minor(database), copy, mode=rules.MODE_WITH_ADULT, guardian_id=1)
    assert _codes(caught.value) == ["R7"]


def test_adult_not_authorized_on_this_link(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent, stranger = _minor(database), _adult(database), _adult(database, "Eve", "Roy", "e@example.org")
    _link(settings, child, parent)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=stranger)
    assert _codes(caught.value) == ["R8"]


def test_authorized_but_unreachable_adult(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child = _minor(database)
    parent = patrons.register(
        database, Registration(person=PersonInput(first_name="Sam", last_name="Roy", category="adult")), None
    )
    _link(settings, child, parent)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=parent)
    assert _codes(caught.value) == ["R7"]


def test_a_household_gives_no_right(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, can_borrow_for=0)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=parent)
    assert _codes(caught.value) == ["R7"]


def test_child_alone_with_the_four_conditions_without_the_guardian_present(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, can_borrow_for=0, alone=1)
    _lend(database, volunteer, child, copy, mode=rules.MODE_ALONE)
    assert _rows(settings, "SELECT responsible_id, borrow_mode FROM loans") == [(parent, "alone")]


@pytest.mark.parametrize("lacking", ["right", "contact", "coordinate", "active"])
def test_child_alone_is_refused_without_each_condition(settings, database, volunteer, lacking):
    (copy,) = _copies(settings, database)
    child = _minor(database)
    if lacking == "coordinate":
        parent = patrons.register(
            database, Registration(person=PersonInput(first_name="Sam", last_name="Roy", category="adult")), None
        )
    else:
        parent = _adult(database)
    _link(
        settings, child, parent, can_borrow_for=0, alone=int(lacking != "right"), is_contact=int(lacking != "contact")
    )
    if lacking == "active":
        _execute(settings, "UPDATE patrons SET status = 'suspended' WHERE id = ?", parent)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_ALONE)
    assert _codes(caught.value) == ["R7"]


def test_revoking_blocks_new_loans_and_keeps_current_ones(settings, database, volunteer):
    first, second = _copies(settings, database, 2)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent, can_borrow_for=0, alone=1)
    loan = _lend(database, volunteer, child, first, mode=rules.MODE_ALONE).loan_ids[0]
    _execute(settings, "UPDATE guardianships SET can_borrow_alone = 0")
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, second, mode=rules.MODE_ALONE)
    assert _codes(caught.value) == ["R7"] and _status(settings, loan) == (None, None)


def test_the_two_rights_never_imply_each_other(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_ALONE)
    assert _codes(caught.value) == ["R7"]


def test_two_guardians_with_different_rights(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, mother, father = _minor(database), _adult(database), _adult(database, "Eve", "Roy", "e@example.org")
    _link(settings, child, mother)
    _link(settings, child, father, can_borrow_for=0, alone=1, rank=2)
    with pytest.raises(LoanRefused):
        _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=father)
    _lend(database, volunteer, child, copy, mode=rules.MODE_ALONE)
    assert _rows(settings, "SELECT responsible_id FROM loans") == [(father,)]


def test_majority_to_confirm_stays_a_minor(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child = _minor(database)
    _execute(settings, "UPDATE patrons SET birth = '2008' WHERE id = ?", child)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=1)
    assert "R7" in _codes(caught.value)


# --- Structures (D2) --------------------------------------------------------------------------------------------------
def _structure(settings, adult, *, allowed=1, role="referent"):
    _execute(
        settings,
        "INSERT INTO households (id, public_id, kind, name, name_norm, version, created_at, updated_at)"
        " VALUES (7, 'h7', 'structure', 'École', 'ecole', 1, 'x', 'x')",
    )
    _execute(
        settings,
        "INSERT INTO household_members (household_id, patron_id, role, joined_on, can_borrow_for_structure)"
        " VALUES (7, ?, ?, 'x', ?)",
        adult,
        role,
        allowed,
    )


def test_a_referent_without_the_authorization_is_refused(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    adult = _adult(database)
    _structure(settings, adult, allowed=0)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, adult, copy, mode=rules.MODE_STRUCTURE, household_id=7)
    assert _codes(caught.value) == ["R13"]


def test_an_authorized_adult_borrows_for_the_structure_with_its_own_rule(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    adult = _adult(database)
    _structure(settings, adult)
    result = _lend(database, volunteer, adult, copy, mode=rules.MODE_STRUCTURE, household_id=7)
    assert result.due_dates == {copy: "2026-12-21"}  # 42 jours
    assert _rows(settings, "SELECT household_id, borrow_mode FROM loans") == [(7, "structure")]


def test_structure_loans_count_on_the_structure_not_on_the_person(settings, database, volunteer):
    copies = _copies(settings, database, 6)
    adult = _adult(database)
    _structure(settings, adult)
    _lend(database, volunteer, adult, *copies[:5], mode=rules.MODE_STRUCTURE, household_id=7)
    result = _lend(database, volunteer, adult, copies[5])
    assert not [item for item in result.warnings if item.code in ("R14", "R20")]  # le compteur personnel est vide


# --- Plafonds et dérogations (D3) -------------------------------------------------------------------------------------
def test_limit_warns_by_default(settings, database, volunteer):
    copies = _copies(settings, database, 6)
    patron = _adult(database)
    _lend(database, volunteer, patron, *copies[:5])
    result = _lend(database, volunteer, patron, copies[5])
    assert [item.code for item in result.warnings] == ["R14"]
    assert _count(settings, "loans") == 6


def _block_limit(settings):
    _execute(settings, "UPDATE settings SET value = '\"bloquer\"' WHERE key = 'circulation.limit_mode'")


def test_blocking_limit_stops_a_volunteer(settings, database, volunteer):
    copies = _copies(settings, database, 6)
    patron = _adult(database)
    _lend(database, volunteer, patron, *copies[:5])
    _block_limit(settings)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, patron, copies[5], reason_code="librarian_decision")
    assert _codes(caught.value) == ["R14"]


def test_blocking_limit_is_overridden_with_a_reason_and_traced(settings, database, volunteer, librarian):
    copies = _copies(settings, database, 6)
    patron = _adult(database)
    _lend(database, volunteer, patron, *copies[:5])
    _block_limit(settings)
    with pytest.raises(LoanRefused):
        _lend(database, librarian, patron, copies[5])  # sans motif
    result = _lend(database, librarian, patron, copies[5], reason_code="librarian_decision")
    row = _rows(
        settings,
        "SELECT reason_code, details, user_id FROM loan_events WHERE kind = 'override' AND loan_id = ?",
        result.loan_ids[0],
    )
    assert (
        row[0][0] == "librarian_decision"
        and json.loads(row[0][1]) == {"regles": ["R14"]}
        and row[0][2] == librarian.user_id
    )


def test_a_forbidden_rule_is_never_overridden(settings, database, volunteer, librarian):
    (copy,) = _copies(settings, database)
    _lend(database, volunteer, _adult(database), copy)
    with pytest.raises(LoanRefused) as caught:
        _lend(
            database, librarian, _adult(database, "Eve", "Roy", "e@example.org"), copy, reason_code="librarian_decision"
        )
    assert _codes(caught.value) == ["R1"]


def test_a_backdated_loan_requires_an_override(settings, database, volunteer, librarian):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    with pytest.raises(LoanRefused) as caught:
        _lend(database, volunteer, patron, copy, loaned_on="2026-11-02", reason_code="late_entry")
    assert _codes(caught.value) == ["R16"]
    result = _lend(database, librarian, patron, copy, loaned_on="2026-11-02", reason_code="late_entry")
    assert result.due_dates == {copy: "2026-11-30"}
    assert _events(settings, result.loan_ids[0]) == ["created", "override"]


def test_a_longer_due_date_requires_an_override_a_shorter_is_free(settings, database, volunteer):
    first, second = _copies(settings, database, 2)
    patron = _adult(database)
    with pytest.raises(LoanRefused) as caught:
        service.lend(database, LendRequest(patron, (LendItem(first, "2027-01-30"),), rules.MODE_SELF), volunteer)
    assert _codes(caught.value) == ["R17"]
    result = service.lend(database, LendRequest(patron, (LendItem(second, "2026-11-20"),), rules.MODE_SELF), volunteer)
    assert result.due_dates == {second: "2026-11-20"}


def test_a_future_loan_date_is_refused(settings, database, librarian):
    (copy,) = _copies(settings, database)
    with pytest.raises(BusinessError, match="futur"):
        _lend(database, librarian, _adult(database), copy, loaned_on="2026-11-10")


def test_a_reason_cannot_name_the_reader(settings, database, librarian):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    with pytest.raises(BusinessError, match="journal"):
        _lend(database, librarian, patron, copy, loaned_on="2026-11-02", reason_code="other", reason_text="pour Martin")


def test_overdues_and_expired_membership_warn_only(settings, database, volunteer, clock):
    first, second = _copies(settings, database, 2)
    patron = _adult(database)
    _execute(settings, "UPDATE patrons SET membership_expires_on = '2026-01-01' WHERE id = ?", patron)
    _lend(database, volunteer, patron, first)
    clock.advance(days=30)
    result = _lend(database, volunteer, patron, second)
    assert {"R18", "R19"} <= {item.code for item in result.warnings}


# --- Rendre (D10, PA-6) -----------------------------------------------------------------------------------------------
def test_return_frees_the_copy_and_says_where_it_goes(settings, database, volunteer, clock):
    (copy,) = _copies(settings, database)
    loan = _lend(database, volunteer, _adult(database), copy).loan_ids[0]
    _execute(
        settings,
        "INSERT INTO locations (id, public_id, kind, name, path, depth, sort_order, is_active, version, created_at,"
        " updated_at, grid_width, grid_height)"
        " VALUES (40, 'l40', 'shelf', 'Niveau 1', 'Salle > Étagère 1 > Niveau 1', 2, 1, 1, 1, 'x', 'x', 1, 1)",
    )
    _execute(settings, "UPDATE copies SET location_id = 40, shelf_position = 3 WHERE id = ?", copy)
    clock.advance(days=30)
    result = service.return_copy(database, copy, volunteer)
    assert (result.loan_id, result.days_overdue) == (loan, 2)
    assert (result.location_path, result.shelf_position) == ("Salle > Étagère 1 > Niveau 1", 3)
    assert _status(settings, loan)[0] is not None and _events(settings, loan) == ["created", "returned"]


def test_a_copy_not_on_loan_has_nothing_to_return(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    result = service.return_copy(database, copy, volunteer)
    assert result.verdict.code == "R25" and result.loan_id is None and _count(settings, "loan_operations") == 0


def test_a_suspended_reader_can_still_return(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    _lend(database, volunteer, patron, copy)
    _execute(settings, "UPDATE patrons SET status = 'suspended' WHERE id = ?", patron)
    assert service.return_copy(database, copy, volunteer).loan_id is not None


def test_a_returned_copy_can_leave_again(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    _lend(database, volunteer, patron, copy)
    service.return_copy(database, copy, volunteer)
    _lend(database, volunteer, patron, copy)
    assert _count(settings, "loans") == 2


# --- Renouveler (D7) --------------------------------------------------------------------------------------------------
def test_renew_up_to_the_limit_then_override(settings, database, volunteer, librarian):
    (copy,) = _copies(settings, database)
    loan = _lend(database, volunteer, _adult(database), copy).loan_ids[0]
    assert service.renew(database, loan, volunteer).due_on == "2027-01-04"
    with pytest.raises(LoanRefused) as caught:
        service.renew(database, loan, volunteer, reason_code="exceptional_duration")
    assert _codes(caught.value) == ["R15"]
    assert service.renew(database, loan, librarian, reason_code="exceptional_duration").due_on == "2027-02-01"
    assert _events(settings, loan) == ["created", "renewed", "renewed", "override"]


def test_renewing_an_overdue_loan_counts_from_today_and_warns(settings, database, volunteer, clock):
    (copy,) = _copies(settings, database)
    loan = _lend(database, volunteer, _adult(database), copy).loan_ids[0]
    clock.advance(days=40)
    result = service.renew(database, loan, volunteer)
    assert result.due_on == "2027-01-16" and [item.code for item in result.warnings] == ["R23"]


def test_a_finished_loan_does_not_renew(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    loan = _lend(database, volunteer, _adult(database), copy).loan_ids[0]
    service.return_copy(database, copy, volunteer)
    with pytest.raises(BusinessError, match="terminé"):
        service.renew(database, loan, volunteer)


def test_a_stale_version_is_refused_and_nothing_is_written(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    loan = _lend(database, volunteer, _adult(database), copy).loan_ids[0]
    service.renew(database, loan, volunteer)
    with pytest.raises(LoanRefused) as caught:
        service.return_copy(database, copy, volunteer, expected_version=1)
    assert _codes(caught.value) == ["R12"] and _status(settings, loan) == (None, None)


# --- Annuler (D9) -----------------------------------------------------------------------------------------------------
def _lent(settings, database, actor, count=1):
    copies = _copies(settings, database, count)
    return copies, _lend(database, actor, _adult(database), *copies)


def test_the_author_cancels_within_the_window(settings, database, volunteer, clock):
    _copies_ids, lent = _lent(settings, database, volunteer, 2)
    clock.advance(minutes=14)
    assert service.cancel_lend(database, lent.operation_id, volunteer) == 2
    assert _count(settings, "loans") == 2  # rien n'est supprimé
    for loan in lent.loan_ids:
        assert _status(settings, loan)[1] is not None and _events(settings, loan) == ["created", "cancelled"]


def test_a_cancelled_copy_can_leave_again(settings, database, volunteer):
    copies, lent = _lent(settings, database, volunteer)
    service.cancel_lend(database, lent.operation_id, volunteer)
    _lend(database, volunteer, _adult(database, "Eve", "Roy", "e@example.org"), *copies)


def test_the_author_is_refused_after_the_window(settings, database, volunteer, clock):
    _ids, lent = _lent(settings, database, volunteer)
    clock.advance(minutes=16)
    with pytest.raises(LoanRefused) as caught:
        service.cancel_lend(database, lent.operation_id, volunteer)
    assert _codes(caught.value) == ["D9"]


def test_another_volunteer_is_refused(settings, database, volunteer, other_volunteer):
    _ids, lent = _lent(settings, database, volunteer)
    with pytest.raises(LoanRefused) as caught:
        service.cancel_lend(database, lent.operation_id, other_volunteer)
    assert _codes(caught.value) == ["D9"]


def test_a_librarian_cancels_later_with_a_reason(settings, database, volunteer, librarian, clock):
    _ids, lent = _lent(settings, database, volunteer)
    clock.advance(hours=3)
    with pytest.raises(BusinessError, match="motif"):
        service.cancel_lend(database, lent.operation_id, librarian)
    service.cancel_lend(database, lent.operation_id, librarian, reason_code="scan_error")
    row = _rows(settings, "SELECT reason_code, user_id FROM loan_events WHERE kind = 'cancelled'")
    assert row == [("scan_error", librarian.user_id)]


def test_cancelling_twice_is_refused(settings, database, volunteer):
    _ids, lent = _lent(settings, database, volunteer)
    service.cancel_lend(database, lent.operation_id, volunteer)
    with pytest.raises(BusinessError, match="déjà"):
        service.cancel_lend(database, lent.operation_id, volunteer)


@pytest.mark.parametrize("dependent", ["returned", "renewed", "reminded", "relent"])
def test_a_dependent_operation_forbids_cancellation_for_everyone(settings, database, volunteer, librarian, dependent):
    copies, lent = _lent(settings, database, volunteer)
    loan = lent.loan_ids[0]
    if dependent in ("returned", "relent"):
        service.return_copy(database, copies[0], volunteer)
    if dependent == "relent":
        _lend(database, volunteer, _adult(database, "Eve", "Roy", "e@example.org"), *copies)
    if dependent == "renewed":
        service.renew(database, loan, volunteer)
    if dependent == "reminded":
        service.note_reminder(database, loan, "phone", "reached", librarian)
    with pytest.raises(LoanRefused) as caught:
        service.cancel_lend(database, lent.operation_id, librarian, reason_code="scan_error")
    assert _codes(caught.value) == ["R26"]


def test_a_return_is_cancelled_and_the_loan_runs_again(settings, database, volunteer, clock):
    copies, lent = _lent(settings, database, volunteer)
    service.return_copy(database, copies[0], volunteer)
    clock.advance(minutes=5)
    service.cancel_return(database, lent.loan_ids[0], volunteer)
    assert _status(settings, lent.loan_ids[0]) == (None, None)
    assert _events(settings, lent.loan_ids[0]) == ["created", "returned", "cancelled"]


def test_a_return_cannot_be_cancelled_once_the_copy_left_again(settings, database, volunteer, librarian):
    copies, lent = _lent(settings, database, volunteer)
    service.return_copy(database, copies[0], volunteer)
    _lend(database, volunteer, _adult(database, "Eve", "Roy", "e@example.org"), *copies)
    with pytest.raises(LoanRefused) as caught:
        service.cancel_return(database, lent.loan_ids[0], librarian, reason_code="scan_error")
    assert _codes(caught.value) == ["R26"]


def test_a_renewal_is_cancelled_and_the_due_date_restored(settings, database, volunteer):
    _copies_ids, lent = _lent(settings, database, volunteer)
    loan = lent.loan_ids[0]
    service.renew(database, loan, volunteer)
    service.cancel_renewal(database, loan, volunteer)
    assert _rows(settings, "SELECT due_on, renewals FROM loans") == [("2026-12-07", 0)]


# --- Corriger (D8, D9) : la trace reste -------------------------------------------------------------------------------
def test_shortening_is_free_lengthening_needs_an_override(settings, database, volunteer, librarian):
    _ids, lent = _lent(settings, database, volunteer)
    loan = lent.loan_ids[0]
    assert service.change_due(database, loan, "2026-11-20", volunteer) == "2026-11-20"
    with pytest.raises(LoanRefused) as caught:
        service.change_due(database, loan, "2026-12-30", volunteer, reason_code="wrong_due")
    assert _codes(caught.value) == ["R17"]
    assert service.change_due(database, loan, "2026-12-30", librarian, reason_code="wrong_due") == "2026-12-30"
    details = _rows(settings, "SELECT details FROM loan_events WHERE kind = 'due_changed' ORDER BY id")
    assert json.loads(details[1][0]) == {"avant": "2026-11-20", "apres": "2026-12-30"}


def test_correcting_a_return_date_keeps_the_trace(settings, database, volunteer, librarian, clock):
    copies, lent = _lent(settings, database, volunteer)
    clock.advance(days=3)
    service.return_copy(database, copies[0], volunteer)
    service.correct_return_date(database, lent.loan_ids[0], "2026-11-10", librarian, reason_code="wrong_return_date")
    assert _status(settings, lent.loan_ids[0])[0] == "2026-11-10T12:00:00Z"
    assert _events(settings, lent.loan_ids[0]) == ["created", "returned", "corrected"]


def test_a_return_date_cannot_precede_the_loan(settings, database, volunteer, librarian, clock):
    copies, lent = _lent(settings, database, volunteer)
    clock.advance(days=3)
    service.return_copy(database, copies[0], volunteer)
    with pytest.raises(BusinessError, match="pas possible"):
        service.correct_return_date(
            database, lent.loan_ids[0], "2026-11-01", librarian, reason_code="wrong_return_date"
        )


def test_a_wrong_reader_is_replaced_by_a_linked_loan(settings, database, volunteer, librarian):
    copies, lent = _lent(settings, database, volunteer)
    right = _adult(database, "Eve", "Roy", "e@example.org")
    request = LendRequest(right, (), rules.MODE_SELF, reason_code="wrong_reader")
    new = service.correct_borrower(database, lent.loan_ids[0], request, librarian)
    assert _status(settings, lent.loan_ids[0])[1] is not None
    assert _rows(settings, "SELECT patron_id, due_on FROM loans WHERE id = ?", new) == [(right, "2026-12-07")]
    assert _events(settings, lent.loan_ids[0]) == ["created", "corrected"]
    assert _rows(settings, "SELECT copy_id FROM loans WHERE returned_at IS NULL AND cancelled_at IS NULL") == [
        (copies[0],)
    ]


def test_a_correction_cannot_bypass_a_forbidden_rule(settings, database, volunteer, librarian):
    _ids, lent = _lent(settings, database, volunteer)
    child = _minor(database)
    request = LendRequest(child, (), rules.MODE_ALONE, reason_code="wrong_reader")
    with pytest.raises(LoanRefused) as caught:
        service.correct_borrower(database, lent.loan_ids[0], request, librarian)
    assert _codes(caught.value) == ["R7"]
    assert _status(settings, lent.loan_ids[0]) == (None, None)


def test_a_volunteer_cannot_correct(settings, database, volunteer):
    copies, lent = _lent(settings, database, volunteer)
    service.return_copy(database, copies[0], volunteer)
    with pytest.raises(BusinessError, match="rôle"):
        service.correct_return_date(
            database, lent.loan_ids[0], "2026-11-09", volunteer, reason_code="wrong_return_date"
        )


# --- Branchements L2 et L3 --------------------------------------------------------------------------------------------
@pytest.fixture
def on_loan(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    return copy, patron, _lend(database, volunteer, patron, copy)


@pytest.mark.parametrize("action", ["repair", "missing", "lost", "withdraw"])
def test_a_copy_on_loan_keeps_its_status(settings, database, on_loan, action):
    copy, _patron, _lent_result = on_loan
    with pytest.raises(BusinessError, match="en prêt"):
        holdings.change_status(database, copy, action, reason="usé", user_id=None)
    assert _rows(settings, "SELECT status FROM copies WHERE id = ?", copy) == [("available",)]


def test_a_copy_on_loan_cannot_be_shelved_or_unshelved(settings, database, on_loan):
    copy, _patron, _lent_result = on_loan
    with pytest.raises(BusinessError, match="garde sa place"):
        holdings.unshelve(database, [copy], user_id=None)
    _execute(
        settings,
        "INSERT INTO locations (id, public_id, kind, name, path, depth, sort_order, is_active, version, created_at,"
        " updated_at, grid_width, grid_height)"
        " VALUES (41, 'l41', 'shelf', 'N', 'N', 0, 1, 1, 1, 'x', 'x', 1, 1)",
    )
    with pytest.raises(BusinessError, match="garde sa place"):
        holdings.shelve(database, [copy], 41, user_id=None)


def test_a_copy_that_has_been_lent_can_no_longer_be_deleted(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    lent = _lend(database, volunteer, _adult(database), copy)
    service.cancel_lend(database, lent.operation_id, volunteer)
    _execute(settings, "UPDATE copies SET status = 'in_processing' WHERE id = ?", copy)
    with pytest.raises(BusinessError, match="historique"):
        catalogue.delete_copy(database, copy, None)


def test_a_reader_on_loan_cannot_be_archived(settings, database, on_loan, volunteer):
    copy, patron, _lent_result = on_loan
    with pytest.raises(BusinessError, match="archivage"):
        patrons.change_status(database, patron, "archive", None, None)
    service.return_copy(database, copy, volunteer)
    patrons.change_status(database, patron, "archive", None, None)


def test_a_cancelled_loan_no_longer_blocks_the_reader(settings, database, on_loan, volunteer):
    _copy, patron, lent = on_loan
    service.cancel_lend(database, lent.operation_id, volunteer)
    patrons.change_status(database, patron, "archive", None, None)


def _admin(settings):
    return make_user(settings, "chef", "admin")


def test_a_reader_on_loan_cannot_be_anonymized_then_the_link_is_erased(settings, database, volunteer):
    (copy,) = _copies(settings, database)
    child, parent = _minor(database), _adult(database)
    _link(settings, child, parent)
    loan = _lend(database, volunteer, child, copy, mode=rules.MODE_WITH_ADULT, guardian_id=parent).loan_ids[0]
    admin = _admin(settings)
    number = _rows(settings, "SELECT number FROM patrons WHERE id = ?", parent)[0][0]
    data = AnonymizeInput(typed_number=number, password=PASSWORD, reason="request")
    with pytest.raises(BusinessError, match="prêt"):
        patrons.anonymize(database, parent, data, admin)
    service.return_copy(database, copy, volunteer)
    patrons.anonymize(database, parent, data, admin)
    assert _rows(settings, "SELECT responsible_id, patron_id FROM loans WHERE id = ?", loan) == [(None, child)]


# --- Rappels et conservation (D13, D15) -------------------------------------------------------------------------------
def test_a_reminder_records_structured_values(settings, database, librarian, volunteer):
    _ids, lent = _lent(settings, database, volunteer)
    service.note_reminder(database, lent.loan_ids[0], "phone", "message_left", librarian)
    assert _rows(settings, "SELECT reminded_on, channel, result, user_id FROM loan_reminders") == [
        ("2026-11-09", "phone", "message_left", librarian.user_id)
    ]


@pytest.mark.parametrize(("channel", "result"), [("sms", "reached"), ("phone", "merci de rendre")])
def test_a_reminder_refuses_values_outside_the_lists(settings, database, librarian, volunteer, channel, result):
    _ids, lent = _lent(settings, database, volunteer)
    with pytest.raises(BusinessError):
        service.note_reminder(database, lent.loan_ids[0], channel, result, librarian)
    assert _count(settings, "loan_reminders") == 0


def test_a_volunteer_cannot_note_a_reminder(settings, database, volunteer):
    _ids, lent = _lent(settings, database, volunteer)
    with pytest.raises(BusinessError, match="rôle"):
        service.note_reminder(database, lent.loan_ids[0], "phone", "reached", volunteer)


def test_retention_is_only_a_simulation(settings, database, volunteer, clock):
    first, second, third = _copies(settings, database, 3)
    patron = _adult(database)
    _lend(database, volunteer, patron, first, second, third)
    service.return_copy(database, first, volunteer)
    service.return_copy(database, second, volunteer)
    _execute(settings, "UPDATE copies SET status = 'lost' WHERE id = ?", second)
    clock.advance(days=400)
    before = sqlite3.connect(settings.database_path)
    dump = "\n".join(before.iterdump())
    before.close()
    with database.session() as session:
        preview = repo.retention_preview(session, service.today())
    assert (preview.concerned, preview.kept_lost_copy, preview.kept_hold) == (1, 1, 0)
    after = sqlite3.connect(settings.database_path)
    assert "\n".join(after.iterdump()) == dump  # aucune écriture
    after.close()


def test_a_hold_keeps_the_loans_out_of_the_simulation(settings, database, volunteer, clock):
    (copy,) = _copies(settings, database)
    patron = _adult(database)
    _lend(database, volunteer, patron, copy)
    service.return_copy(database, copy, volunteer)
    _execute(
        settings,
        "INSERT INTO patron_holds (patron_id, reason, placed_at) VALUES (?, 'litige', '2026-11-09T00:00:00Z')",
        patron,
    )
    clock.advance(days=400)
    with database.session() as session:
        preview = repo.retention_preview(session, service.today())
    assert (preview.concerned, preview.kept_hold) == (0, 1)


# --- Journal --------------------------------------------------------------------------------------------------------
def test_the_journal_carries_no_title_nor_reader(settings, database, volunteer, librarian):
    copies, lent = _lent(settings, database, volunteer)
    service.renew(database, lent.loan_ids[0], volunteer)
    service.return_copy(database, copies[0], volunteer)
    entries = _rows(settings, "SELECT action, details FROM audit_log WHERE action LIKE 'loan.%'")
    assert {row[0] for row in entries} == {"loan.lend", "loan.renew", "loan.return"}
    blob = " ".join(row[1] or "" for row in entries)
    for forbidden in ("Martin", "Paul", "Lotus", "example.org", "P-0"):
        assert forbidden not in blob


def test_permissions_are_checked_by_the_service(settings, database, volunteer):
    copies, lent = _lent(settings, database, volunteer)
    nobody = Actor(None, frozenset())
    with pytest.raises(BusinessError, match="rôle"):
        _lend(database, nobody, 1, copies[0])
    with pytest.raises(BusinessError, match="rôle"):
        service.return_copy(database, copies[0], nobody)
    with pytest.raises(BusinessError, match="rôle"):
        service.renew(database, lent.loan_ids[0], nobody)
