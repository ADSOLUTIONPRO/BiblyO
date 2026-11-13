# SPDX-License-Identifier: EUPL-1.2
"""Contrôle de reprise : passe sur le dépôt, échoue sur une copie, même renommée, accepte une suite examinée."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_provenance", ROOT / "scripts" / "check_provenance.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_provenance"] = gate  # dataclass résout ses annotations par le module
_spec.loader.exec_module(gate)

# Code inventé pour l'essai : il tient lieu du logiciel antérieur.
ORIGINAL = """
def settle_account(ledger, member, amount, when):
    entry = ledger.lookup(member)
    if entry is None or entry.closed:
        raise LookupError("unknown member")
    balance = entry.balance - amount
    for fee in entry.pending_fees(when):
        if fee.due <= when and not fee.waived:
            balance -= fee.value * (1 + fee.rate) ** max(0, (when - fee.due).days // 30)
    while balance < 0 and entry.credit:
        balance += entry.credit.pop()
    entry.history.append((when, amount, balance))
    return balance if balance >= 0 else -balance
"""
RENAMED = (
    ORIGINAL.replace("settle_account", "solder")
    .replace("ledger", "registre")
    .replace("member", "lecteur")
    .replace("entry", "fiche")
    .replace("balance", "solde")
    .replace("fee", "frais")
    .replace("unknown member", "lecteur inconnu")
)
UNRELATED = """
def format_call_number(prefix, author, volume=None):
    parts = [prefix.upper(), author[:3].upper()]
    if volume:
        parts.append(f"T{volume:02d}")
    return " ".join(parts)
"""


@pytest.fixture
def setup(tmp_path):
    old = tmp_path / "ancien"
    old.mkdir()
    (old / "accounts.py").write_text(ORIGINAL, encoding="utf-8")
    reference = tmp_path / "reference.bin"
    gate.write_reference([f"essai={old}"], reference)
    reviewed = tmp_path / "examines.json"
    reviewed.write_text(json.dumps({"examinees": []}), encoding="utf-8")
    project = tmp_path / "projet"
    (project / "src").mkdir(parents=True)
    return project, reference, reviewed


def test_the_repository_passes():
    problems, _ = gate.check()
    assert problems == []


def test_the_reference_holds_only_fingerprints():
    header, sets = gate.read_reference()
    assert header["empreinte"] == "blake2b-40"
    assert {s["nom"] for s in header["sources"]} == {"amont-39edc43", "v0.73.0", "modules-locaux"}
    assert all(len(values) == header["modes"][mode]["nombre"] for mode, values in sets.items())


def test_unrelated_code_passes(setup):
    project, reference, reviewed = setup
    (project / "src" / "cote.py").write_text(UNRELATED, encoding="utf-8")
    assert gate.check(project, reference, reviewed)[0] == []


def test_a_verbatim_copy_is_refused(setup):
    project, reference, reviewed = setup
    (project / "src" / "copie.py").write_text(UNRELATED + ORIGINAL, encoding="utf-8")
    problems, _ = gate.check(project, reference, reviewed)
    assert any("src/copie.py" in p and "brute" in p for p in problems)


def test_a_renamed_copy_is_refused(setup):
    project, reference, reviewed = setup
    (project / "src" / "renomme.py").write_text(RENAMED, encoding="utf-8")
    problems, _ = gate.check(project, reference, reviewed)
    assert any("src/renomme.py" in p and "logique" in p for p in problems)
    assert not any("brute" in p for p in problems)


def test_a_reviewed_run_is_accepted(setup):
    project, reference, reviewed = setup
    (project / "src" / "renomme.py").write_text(RENAMED, encoding="utf-8")
    _, runs = gate.check(project, reference, reviewed)
    flagged = [r for r in runs if r.length >= gate.LIMITS[r.mode]]
    entries = [{"fichier": r.path, "empreinte": r.key, "motif": "essai"} for r in flagged]
    reviewed.write_text(json.dumps({"examinees": entries}), encoding="utf-8")
    assert gate.check(project, reference, reviewed)[0] == []


def test_a_truncated_reference_is_refused(setup, tmp_path):
    _, reference, _ = setup
    damaged = tmp_path / "abimee.bin"
    damaged.write_bytes(reference.read_bytes()[:-3])
    with pytest.raises(ValueError, match="tronquée"):
        gate.read_reference(damaged)
