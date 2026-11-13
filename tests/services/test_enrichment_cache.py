# SPDX-License-Identifier: EUPL-1.2
"""Cache des réponses : durées de D15, clés sans texte en clair, expiration, pannes jamais conservées."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from biblyo.core import clock
from biblyo.core.db import Database
from biblyo.modules.enrichment import cache

MOMENT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


@pytest.fixture
def frozen():
    state = {"now": MOMENT}
    clock.set_clock(lambda: state["now"])
    yield state
    clock.set_clock(None)


def test_keys_hide_the_searched_text():
    assert cache.key_for("isbn", "9782070612758") == "isbn:9782070612758"
    key = cache.key_for("search", "2", 'bib.title all "ecume des jours"')
    assert key.startswith("search:") and "ecume" not in key and len(key) == len("search:") + 32
    assert cache.key_for("search", "2", "a") != cache.key_for("search", "3", "a")


def test_found_lives_thirty_days_and_not_found_seven(database, frozen):
    with database.session(write=True) as session:
        cache.put(session, "bnf", "isbn:1", status="found", total=1, body="<r/>")
        cache.put(session, "bnf", "isbn:2", status="not_found", total=0, body="")
    frozen["now"] = MOMENT + timedelta(days=7, seconds=1)
    with database.session(write=True) as session:
        assert cache.get(session, "bnf", "isbn:1") is not None
        assert cache.get(session, "bnf", "isbn:2") is None
    frozen["now"] = MOMENT + timedelta(days=30, seconds=1)
    with database.session(write=True) as session:
        assert cache.get(session, "bnf", "isbn:1") is None


def test_an_entry_is_per_source_and_refreshable(database, frozen):
    with database.session(write=True) as session:
        cache.put(session, "bnf", "isbn:1", status="found", total=1, body="<a/>")
        cache.put(session, "sudoc", "isbn:1", status="not_found", total=0, body="")
    with database.session(write=True) as session:
        entry = cache.get(session, "bnf", "isbn:1")
        assert entry is not None and entry.body == "<a/>" and entry.fetched_at == "2026-11-11T12:00:00Z"
        assert cache.get(session, "sudoc", "isbn:1").status == "not_found"
        cache.put(session, "bnf", "isbn:1", status="found", total=2, body="<b/>")
    with database.session() as session:
        assert cache.get(session, "bnf", "isbn:1").body == "<b/>"


def test_failures_and_huge_bodies_are_never_cached(database, frozen):
    with database.session(write=True) as session:
        cache.put(session, "bnf", "isbn:1", status="unavailable", total=0, body="")
        cache.put(session, "bnf", "isbn:2", status="found", total=1, body="x" * (cache.MAX_BODY_BYTES + 1))
    with database.session() as session:
        assert cache.get(session, "bnf", "isbn:1") is None
        assert cache.get(session, "bnf", "isbn:2") is None


def test_purge_and_clear(database, frozen):
    with database.session(write=True) as session:
        cache.put(session, "bnf", "isbn:1", status="found", total=1, body="<a/>")
        cache.put(session, "bnf", "isbn:2", status="not_found", total=0, body="")
        cache.put(session, "sudoc", "isbn:3", status="found", total=1, body="<c/>")
    frozen["now"] = MOMENT + timedelta(days=8)
    with database.session(write=True) as session:
        assert cache.purge_expired(session) == 1
        assert cache.clear(session, "sudoc") == 1
        assert cache.clear(session) == 1
        assert cache.get(session, "bnf", "isbn:1") is None


def test_forget_one_entry(database, frozen):
    with database.session(write=True) as session:
        cache.put(session, "bnf", "isbn:1", status="found", total=1, body="<a/>")
        cache.forget(session, "bnf", "isbn:1")
        assert cache.get(session, "bnf", "isbn:1") is None
