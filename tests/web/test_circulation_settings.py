# SPDX-License-Identifier: EUPL-1.2
"""Règles de prêt, seuils et simulation de conservation (D3, D4, D12, D13)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _form(settings, **changes):
    data = {"mode_plafond": "avertir", "seuil_retards": "", "fermeture": "", "conservation_mois": "12"}
    for rule_id, days, renewal, renewals, limit in _rows(
        settings, "SELECT id, loan_days, renewal_days, max_renewals, max_active_loans FROM loan_rules"
    ):
        data |= {
            f"regle_{rule_id}_duree": str(days),
            f"regle_{rule_id}_prolongation": str(renewal),
            f"regle_{rule_id}_renouvellements": str(renewals),
            f"regle_{rule_id}_plafond": "" if limit is None else str(limit),
        }
    return data | changes


def _adult_rule(settings):
    return _rows(settings, "SELECT id FROM loan_rules WHERE patron_category = 'adult' AND document_type_id IS NULL")[0][
        0
    ]


@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 403), ("viewer", 403)])
def test_settings_access(client_as, role, status):
    assert client_as(role).get("/prets/regles").status_code == status


def test_the_page_shows_the_default_rules_and_the_simulation(client_as):
    html = client_as("librarian").get("/prets/regles").text
    for text in (
        "Adulte",
        "Mineur",
        "Structure",
        "Simulation",
        "Hypothèse, sans effet",
        "aucune commande d’effacement",
    ):
        assert text in html
    assert 'value="28"' in html and 'value="21"' in html and 'value="42"' in html


def test_saving_changes_rules_and_settings_and_logs_names_only(settings, client_as):
    rule = _adult_rule(settings)
    data = _form(settings, mode_plafond="bloquer", seuil_retards="3", fermeture="0", conservation_mois="18")
    data[f"regle_{rule}_duree"] = "30"
    response = client_as("librarian").post("/prets/regles", data=data)
    assert response.status_code == 303
    assert _rows(settings, "SELECT loan_days FROM loan_rules WHERE id = ?", rule) == [(30,)]
    stored = dict(_rows(settings, "SELECT key, value FROM settings WHERE key LIKE 'circulation.%'"))
    assert {key: json.loads(value) for key, value in stored.items()} == {
        "circulation.limit_mode": "bloquer",
        "circulation.overdue_block": 3,
        "circulation.closed_weekday": 0,
        "circulation.retention_months": 18,
    }
    entry = json.loads(_rows(settings, "SELECT details FROM audit_log WHERE action = 'circulation.settings'")[0][0])
    assert set(entry["champs"]) == {
        f"regle_{rule}",
        "circulation.limit_mode",
        "circulation.overdue_block",
        "circulation.closed_weekday",
        "circulation.retention_months",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"duree": "0"},
        {"duree": "400"},
        {"renouvellements": "-1"},
        {"plafond": "0"},
        {"duree": "dix"},
        {"mode": "x"},
        {"seuil": "0"},
        {"fermeture": "9"},
        {"mois": "0"},
    ],
)
def test_a_wrong_value_saves_nothing(settings, client_as, change):
    rule = _adult_rule(settings)
    data = _form(settings)
    key, value = next(iter(change.items()))
    field = {"mode": "mode_plafond", "seuil": "seuil_retards", "fermeture": "fermeture", "mois": "conservation_mois"}
    data[field.get(key, f"regle_{rule}_{key}")] = value
    data[f"regle_{rule}_prolongation"] = "29" if key != "prolongation" else data[f"regle_{rule}_prolongation"]
    response = client_as("librarian").post("/prets/regles", data=data)
    assert response.status_code == 400
    assert _rows(settings, "SELECT renewal_days FROM loan_rules WHERE id = ?", rule) == [(28,)]
    assert _rows(settings, "SELECT COUNT(*) FROM audit_log WHERE action = 'circulation.settings'") == [(0,)]


def test_an_empty_limit_means_no_limit(settings, client_as):
    rule = _adult_rule(settings)
    data = _form(settings)
    data[f"regle_{rule}_plafond"] = ""
    assert client_as("librarian").post("/prets/regles", data=data).status_code == 303
    assert _rows(settings, "SELECT max_active_loans FROM loan_rules WHERE id = ?", rule) == [(None,)]


def test_a_rule_for_a_document_type_wins_and_can_be_removed(settings, database, client_as):
    browser = client_as("librarian")
    kind = _rows(settings, "SELECT id FROM document_types WHERE code = 'bd'")[0][0]
    data = {"type": str(kind), "categorie": "adult", "duree": "7", "prolongation": "7", "renouvellements": "0"}
    assert browser.post("/prets/regles/ajouter", data=data).status_code == 303
    assert browser.post("/prets/regles/ajouter", data=data).status_code == 400  # même portée
    work = make_notice(settings, "Une BD")
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
    sqlite3.connect(settings.database_path).execute("UPDATE copies SET status = 'available'").connection.commit()
    person = PersonInput(first_name="Ana", last_name="Roy", category="adult")
    patron = patrons.register(database, Registration(person=person), None)
    lent = service.lend(
        database, LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, frozenset({rules.LEND}))
    )
    assert lent.due_dates[copy_id] == (service.today().fromordinal(service.today().toordinal() + 7)).isoformat()
    added = _rows(settings, "SELECT id FROM loan_rules WHERE document_type_id = ?", kind)[0][0]
    assert browser.post("/prets/regles/supprimer", data={"supprimer": str(added)}).status_code == 303
    base = _adult_rule(settings)
    assert browser.post("/prets/regles/supprimer", data={"supprimer": str(base)}).status_code == 400


def test_the_simulation_writes_nothing_from_the_page(settings, client_as):
    browser = client_as("librarian")  # le compte et sa session existent avant la photographie de la base
    browser.get("/prets/regles")
    before = "\n".join(sqlite3.connect(settings.database_path).iterdump())
    assert browser.get("/prets/regles").status_code == 200
    assert "\n".join(sqlite3.connect(settings.database_path).iterdump()) == before


def test_the_patron_export_carries_the_loans(settings, database, client_as):
    work = make_notice(settings, "Exporté")
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
    sqlite3.connect(settings.database_path).execute("UPDATE copies SET status = 'available'").connection.commit()
    person = PersonInput(first_name="Ana", last_name="Roy", category="adult")
    patron = patrons.register(database, Registration(person=person), None)
    service.lend(
        database, LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, frozenset({rules.LEND}))
    )
    data = client_as("admin").get(f"/lecteurs/{patron}/export.json").json()
    assert [item["ouvrage"] for item in data["prets"]] == ["Exporté"] and data["prets_dont_il_repond"] == []
    assert "Exporté" in client_as("admin").get(f"/lecteurs/{patron}/export").text
