# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

from types import SimpleNamespace

import pytest

from biblyo.core.security import ratelimit
from biblyo.core.security.ratelimit import FailureLimiter, client_ip


def _request(peer: str, forwarded: str | None = None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_blocks_after_limit_then_releases(monkeypatch):
    moment = [100.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: moment[0])
    limiter = FailureLimiter(2, 60)
    assert not limiter.blocked("a")
    limiter.record("a")
    assert not limiter.blocked("a")
    limiter.record("a")
    assert limiter.blocked("a")
    assert not limiter.blocked("b")
    moment[0] += 61
    assert not limiter.blocked("a")


def test_reset_clears_the_key():
    limiter = FailureLimiter(1, 60)
    limiter.record("a")
    assert limiter.blocked("a")
    limiter.reset("a")
    assert not limiter.blocked("a")


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 3)
    limiter = FailureLimiter(5, 60)
    for index in range(10):
        limiter.record(str(index))
    assert len(limiter._failures) <= 3


def test_forwarded_header_ignored_without_trusted_proxy():
    assert client_ip(_request("192.0.2.1", "198.51.100.9"), ()) == "192.0.2.1"
    assert client_ip(_request("192.0.2.1", "198.51.100.9"), ("10.0.0.1",)) == "192.0.2.1"


def test_forwarded_header_read_behind_declared_proxy():
    assert client_ip(_request("10.0.0.1", "198.51.100.9"), ("10.0.0.1",)) == "198.51.100.9"
    assert client_ip(_request("10.0.0.5", "198.51.100.9"), ("10.0.0.0/24",)) == "198.51.100.9"


def test_spoofed_leftmost_value_does_not_win():
    """Le client écrit ce qu'il veut à gauche : seule compte la dernière adresse non déclarée."""
    assert client_ip(_request("10.0.0.1", "203.0.113.66, 198.51.100.9"), ("10.0.0.1",)) == "198.51.100.9"


def test_malformed_values_do_not_break():
    assert client_ip(_request("10.0.0.1", "pas-une-adresse"), ("10.0.0.1", "aussi faux")) == "pas-une-adresse"
    assert client_ip(_request("pas-une-adresse"), ("10.0.0.1",)) == "pas-une-adresse"


@pytest.mark.parametrize("peer", ["10.0.0.1"])
def test_proxy_only_chain_falls_back_to_peer(peer):
    assert client_ip(_request(peer, "10.0.0.1"), ("10.0.0.1",)) == peer
