# SPDX-License-Identifier: EUPL-1.2
"""Espacement par source, mise de côté après trois échecs, paliers, Retry-After, remise à zéro."""

from __future__ import annotations

import pytest

from biblyo.modules.enrichment.throttle import MAX_WAIT, Sidelined, Throttle


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


def test_calls_are_spaced_by_the_interval(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    throttle.acquire("bnf", 1.0)
    throttle.acquire("bnf", 1.0)
    throttle.acquire("bnf", 1.0)
    assert fake.slept == [1.0, 1.0]


def test_sources_do_not_wait_for_each_other(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    throttle.acquire("bnf", 5.0)
    throttle.acquire("sudoc", 2.0)
    assert fake.slept == []


def test_a_long_gap_needs_no_wait(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    throttle.acquire("bnf", 1.0)
    fake.now += 10
    throttle.acquire("bnf", 1.0)
    assert fake.slept == []


def test_the_wait_is_bounded(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    throttle.acquire("bnf", 500.0)
    throttle.acquire("bnf", 500.0)
    assert fake.slept == [MAX_WAIT]


def test_three_failures_sideline_the_source_with_growing_steps(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 300.0]
    with pytest.raises(Sidelined):
        throttle.acquire("bnf", 1.0)
    assert 0 < throttle.sidelined_for("bnf") <= 300
    fake.now += 301
    throttle.acquire("bnf", 1.0)  # de nouveau disponible
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 600.0]
    fake.now += 601
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 1800.0]
    fake.now += 1801
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 1800.0]  # dernier palier, sans fin


def test_a_success_resets_failures_and_steps(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    throttle.failed("bnf")
    throttle.failed("bnf")
    throttle.succeeded("bnf")
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 300.0]
    fake.now += 301
    throttle.succeeded("bnf")
    assert [throttle.failed("bnf") for _ in range(3)] == [0.0, 0.0, 300.0]


def test_retry_after_sidelines_at_once_within_bounds(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    assert throttle.failed("bnf", retry_after=45.0) == 45.0
    assert throttle.sidelined_for("bnf") == 45.0
    throttle.reset("bnf")
    assert throttle.failed("bnf", retry_after=99999.0) == 1800.0
    throttle.reset("bnf")
    assert throttle.failed("bnf", retry_after=0.0) == 1.0


def test_sidelined_does_not_block_other_sources(fake):
    throttle = Throttle(clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        throttle.failed("sudoc")
    throttle.acquire("bnf", 1.0)
    assert throttle.sidelined_for("bnf") == 0.0
