#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Mesure de performance de la circulation (UX-Circulation 2.0) sur le jeu synthétique de `perf_catalogue.py`.

100 000 notices, 120 000 exemplaires, 10 000 lecteurs et leurs liens familiaux, auxquels s'ajoutent 5 000 prêts en
cours (dont 1 500 en retard) et 20 000 prêts terminés. Base jetable, graine fixe, vrai code, vraies réponses HTTP.
Les cibles ont été fixées avant toute optimisation (docs/rapports/UX-CIRCULATION-AUDIT.md, § 8) : un dépassement est
publié, jamais masqué.

Usage : python scripts/perf_circulation.py [--notices 100000] [--graine 20261108] [--mesures 60] [--sortie f.json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("perf_catalogue", HERE / "perf_catalogue.py")
assert _spec is not None and _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

ACTIVE_LOANS = 5_000
OVERDUE_LOANS = 1_500
FINISHED_LOANS = 20_000
TODAY = date(2026, 11, 10)
TARGETS = {
    "catalogue": 150.0,
    "lecteurs": 60.0,
    "livres": 150.0,
    "comptoir": 120.0,
    "ajout": 120.0,
    "validation": 150.0,
    "liste": 120.0,
}


def add_loans(folder: Path, seed: int) -> dict[str, int]:
    """Des prêts inventés, écrits directement : chaque exemplaire prêté au plus une fois à la fois (R1)."""
    settings = base._settings(folder)
    rng = random.Random(seed + 1)  # noqa: S311 : jeu d'essai reproductible
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN")
    copies = [
        row[0] for row in connection.execute("SELECT id FROM copies WHERE status = 'available' AND is_loanable = 1")
    ]
    adults = [
        row[0] for row in connection.execute("SELECT id FROM patrons WHERE category = 'adult' AND status = 'active'")
    ]
    share = min(1.0, len(copies) * 0.6 / (ACTIVE_LOANS + FINISHED_LOANS))  # un petit jeu d'essai garde les proportions
    active_count, overdue_count = round(ACTIVE_LOANS * share), round(OVERDUE_LOANS * share)
    chosen = rng.sample(copies, active_count + round(FINISHED_LOANS * share))
    operations, loans = [], []
    moment = "2026-10-01T10:00:00Z"
    for index, copy_id in enumerate(chosen, start=1):
        active = index <= active_count
        late = index <= overdue_count
        due = TODAY - timedelta(days=rng.randint(1, 40)) if late else TODAY + timedelta(days=rng.randint(0, 28))
        if not active:
            due = TODAY - timedelta(days=rng.randint(30, 700))
        operations.append((index, "lend", moment, None))
        loans.append(
            (
                index, index, copy_id, rng.choice(adults), "self", moment, due.isoformat(),
                None if active else "2026-10-20T10:00:00Z", 0, 1,
            )
        )  # fmt: skip
    connection.executemany(
        "INSERT INTO loan_operations (id, kind, created_at, user_id) VALUES (?, ?, ?, ?)", operations
    )
    connection.executemany(
        "INSERT INTO loans (id, operation_id, copy_id, patron_id, borrow_mode, loaned_at, due_on, returned_at,"
        " renewals, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        loans,
    )
    connection.commit()
    connection.execute("ANALYZE")
    connection.close()
    return {
        "prets_en_cours": active_count,
        "dont_en_retard": overdue_count,
        "prets_termines": len(chosen) - active_count,
    }


def measure(folder: Path, count: int) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from biblyo.app import create_app
    from biblyo.core import clock
    from biblyo.core.clock import now_iso
    from biblyo.core.db import Database
    from biblyo.core.models import AuthSession, User
    from biblyo.core.security import passwords, sessions
    from biblyo.core.security.sessions import COOKIE_NAME
    from biblyo.modules.admin import repository as admin_repository
    from biblyo.modules.catalogue import listing
    from biblyo.modules.catalogue.listing import Criteria
    from biblyo.modules.circulation import counter, rules, service
    from biblyo.modules.circulation import repository as repo
    from biblyo.modules.circulation.counter import CounterState
    from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
    from biblyo.modules.holdings import availability, pages
    from biblyo.modules.holdings.models import Location
    from biblyo.modules.patrons.repository import EVERYTHING

    clock.set_clock(lambda: datetime(2026, 11, 10, 9, 0, tzinfo=UTC))
    settings = base._settings(folder)
    database = Database(settings.database_path)
    actor = Actor(None, frozenset({rules.LEND, rules.RENEW}))
    connection = sqlite3.connect(settings.database_path)
    free = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM copies WHERE status = 'available' AND is_loanable = 1"
            " AND id NOT IN (SELECT copy_id FROM loans WHERE returned_at IS NULL AND cancelled_at IS NULL) LIMIT 2000"
        )
    ]
    readers = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM patrons WHERE category = 'adult' AND status = 'active' ORDER BY id LIMIT 500"
        )
    ]
    names = [row[0] for row in connection.execute("SELECT last_name FROM patrons ORDER BY id LIMIT 200")]
    words = [row[0].split()[0] for row in connection.execute("SELECT title FROM works ORDER BY id LIMIT 200")]
    heavy = connection.execute(
        "SELECT l.parent_id FROM copies c JOIN locations l ON l.id = c.location_id"
        " GROUP BY l.parent_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    connection.close()
    results: list[dict[str, Any]] = []

    def run(name: str, family: str, target: float | None, operation: Any) -> None:
        results.append(base._summary(name, family, target, base._measure(count, operation)))

    def session_run(fn: Any) -> Any:
        def operation(index: int) -> None:
            with database.session() as session:
                fn(session, index)

        return operation

    run(
        "Catalogue : page de grille avec disponibilité",
        "service",
        TARGETS["catalogue"],
        session_run(lambda s, _i: listing.run(s, Criteria())),
    )
    run(
        "Catalogue : recherche d'un mot avec disponibilité",
        "service",
        TARGETS["catalogue"],
        session_run(lambda s, i: listing.run(s, Criteria(q=words[i % len(words)]))),
    )
    run(
        "Disponibilité seule : 24 œuvres",
        "decomposition",
        None,
        session_run(lambda s, i: availability.for_works(s, list(range(1 + i * 24, 25 + i * 24)))),
    )
    run(
        "Lecteurs : première page, sans saisie",
        "service",
        TARGETS["lecteurs"],
        session_run(lambda s, _i: counter.reader_page(s, "", 1, personal=False, visibility=EVERYTHING)),
    )
    run(
        "Lecteurs : recherche par nom",
        "service",
        TARGETS["lecteurs"],
        session_run(
            lambda s, i: counter.reader_page(s, names[i % len(names)], 1, personal=False, visibility=EVERYTHING)
        ),
    )
    run(
        "Lecteurs : page 40",
        "service",
        TARGETS["lecteurs"],
        session_run(lambda s, _i: counter.reader_page(s, "", 40, personal=False, visibility=EVERYTHING)),
    )
    run(
        "Livres : première page, sans saisie",
        "service",
        TARGETS["livres"],
        session_run(lambda s, _i: counter.book_page(s, "", 1, [])),
    )
    run(
        "Livres : recherche d'un mot",
        "service",
        TARGETS["livres"],
        session_run(lambda s, i: counter.book_page(s, words[i % len(words)], 1, [])),
    )

    def desk(index: int) -> CounterState:
        start = (index * 3) % (len(free) - 3)
        return CounterState(patron_id=readers[index % len(readers)], mode="self", copy_ids=free[start : start + 3])

    run(
        "Comptoir : lecteur et 3 livres",
        "service",
        TARGETS["comptoir"],
        session_run(lambda s, i: counter.build(s, desk(i), actor, EVERYTHING)),
    )

    def add(session: Any, index: int) -> None:
        state = desk(index)
        extra = state.copy_ids.pop()
        counter.Scanner(session, EVERYTHING, state).apply(rules.Candidate(rules.KIND_COPY, extra))
        counter.build(session, state, actor, EVERYTHING)

    run("Comptoir : ajout d'un exemplaire", "service", TARGETS["ajout"], session_run(add))

    lent: list[int] = []

    def lend(index: int) -> None:
        state = desk(index + 500)
        request = LendRequest(state.patron_id or 0, tuple(LendItem(item) for item in state.copy_ids), "self")
        service.lend(database, request, actor)
        lent.extend(state.copy_ids)

    run("Validation d'un prêt de 3 livres", "service", TARGETS["validation"], lend)
    for copy_id in lent:  # le jeu revient à son état : les mesures suivantes ne voient pas ces prêts
        service.return_copy(database, copy_id, actor)
    run(
        "Liste des prêts en cours, page de 50",
        "service",
        TARGETS["liste"],
        session_run(lambda s, _i: repo.loan_list(s, "cours", service.today(), EVERYTHING, page=1, size=50)),
    )
    run(
        "Liste des prêts en retard, page de 50",
        "service",
        TARGETS["liste"],
        session_run(lambda s, _i: repo.loan_list(s, "retards", service.today(), EVERYTHING, page=1, size=50)),
    )
    run(
        "Liste des prêts : recherche par nom",
        "service",
        TARGETS["liste"],
        session_run(
            lambda s, i: repo.loan_list(
                s, "cours", service.today(), EVERYTHING, page=1, size=50, query=names[i % len(names)], by_reader=True
            )
        ),
    )
    run(
        "Étagère : meuble le plus chargé, avec les dos sortis",
        "service",
        None,
        session_run(lambda s, _i: pages.unit_view(s, s.get(Location, heavy))),
    )

    moment = now_iso()
    with database.session(write=True) as session:
        user = User(
            username="mesure",
            display_name="Mesure",
            password_hash=passwords.hash_password("Mesure-42-Perf"),
            is_active=True,
            must_change_password=False,
            created_at=moment,
            updated_at=moment,
        )
        user.roles = list(admin_repository.roles_by_codes(session, ["admin"]))
        session.add(user)
        session.flush()
        raw = sessions.open_session(session, user.id, settings, "127.0.0.1", "Mesure")
        csrf = session.scalars(select(AuthSession.csrf_token).where(AuthSession.user_id == user.id)).one()
    application = create_app(settings)
    client = TestClient(application, base_url="http://127.0.0.1")
    client.cookies.set(COOKIE_NAME, raw)
    client.headers["X-CSRF-Token"] = csrf

    def get(path: Any) -> Any:
        def operation(index: int) -> None:
            response = client.get(path(index))
            assert response.status_code == 200, response.status_code

        return operation

    def post(path: str, data: Any) -> Any:
        def operation(index: int) -> None:
            response = client.post(path, data=data(index), headers={"HX-Request": "true"})
            assert response.status_code == 200, response.status_code

        return operation

    run("HTTP GET /catalogue (grille)", "http", TARGETS["catalogue"], get(lambda _i: "/catalogue"))
    run("HTTP GET /catalogue?view=list", "http", TARGETS["catalogue"], get(lambda _i: "/catalogue?view=list"))
    run("HTTP GET /comptoir (listes d'emblée)", "http", None, get(lambda _i: "/comptoir"))
    run(
        "HTTP POST /comptoir/lecteurs (frappe)",
        "http",
        TARGETS["lecteurs"],
        post("/comptoir/lecteurs", lambda i: {"lecteur_q": names[i % len(names)]}),
    )
    run(
        "HTTP POST /comptoir/livres (frappe)",
        "http",
        TARGETS["livres"],
        post("/comptoir/livres", lambda i: {"livre_q": words[i % len(words)]}),
    )
    run(
        "HTTP POST /comptoir/etat (lecteur et 3 livres)",
        "http",
        TARGETS["comptoir"],
        post(
            "/comptoir/etat",
            lambda i: {"lecteur": str(desk(i).patron_id), "ex": [str(item) for item in desk(i).copy_ids]},
        ),
    )
    run("HTTP GET /prets", "http", TARGETS["liste"], get(lambda _i: "/prets"))
    run("HTTP GET /emplacements/<meuble chargé>", "http", None, get(lambda _i: f"/emplacements/{heavy}"))
    database.dispose()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notices", type=int, default=100_000)
    parser.add_argument("--graine", type=int, default=20261108)
    parser.add_argument("--mesures", type=int, default=60)
    parser.add_argument("--sortie", type=Path)
    arguments = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="biblyo-perf-circ-"))
    try:
        started = time.perf_counter()
        dataset = base.generate(folder, arguments.notices, arguments.graine)
        base.step_reindex(folder)
        loans = add_loans(folder, arguments.graine)
        generation = round(time.perf_counter() - started, 1)
        scenarios = measure(folder, arguments.mesures)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    report = {
        "environnement": base._environment(),
        "graine": arguments.graine,
        "jeu": {**{k: v for k, v in dataset.items() if k not in ("vocabulaire", "editeurs")}, **loans},
        "generation_s": generation,
        "mesures": {"scenarios": scenarios},
    }
    if arguments.sortie:
        arguments.sortie.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["jeu"], ensure_ascii=False))
    print(base._table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
