# SPDX-License-Identifier: EUPL-1.2
"""Service d'enrichissement sur un réseau simulé : recherche à niveaux, cache, application, annulation, invariants."""

from __future__ import annotations

import hashlib
import sqlite3

import httpx
import pytest
from sqlalchemy import select

from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.models import Edition, EditionIdentifier
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.enrichment import repository as repo
from biblyo.modules.enrichment import service
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.models import (
    EditionIsbnCandidate,
    EnrichmentDecision,
    EnrichmentProposal,
    FieldProvenance,
)
from biblyo.modules.enrichment.service import Acceptance, Criteria
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice
from tests.sources import read_bytes

PETIT_PRINCE = "9782070612758"
INVARIANT_TABLES = ("copies", "locations", "patrons", "loans", "retired_codes", "work_series", "series")


class Network:
    """Réseau simulé : une réponse enregistrée par morceau de requête, et le compte des appels."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, str, int]] = []  # (texte dans l'adresse, réponse enregistrée ou code, statut)
        self.calls: list[str] = []

    def when(self, needle: str, fixture: str, status: int = 200) -> Network:
        self.rules.append((needle, fixture, status))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for needle, fixture, status in self.rules:
            if needle in url:
                body = read_bytes(fixture) if "/" in fixture else fixture.encode()
                kind = "text/xml;charset=UTF-8" if status == 200 else "text/html"
                return httpx.Response(status, content=body, headers={"Content-Type": kind})
        default = "sudoc/sru-isbn-inconnu" if "sudoc" in url else "bnf/isbn-inconnu"
        return httpx.Response(200, content=read_bytes(default), headers={"Content-Type": "text/xml"})

    def count(self, host: str) -> int:
        return sum(1 for url in self.calls if host in url)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def make_hub(database: Database, network: Network) -> SourceHub:
    client = httpx.Client(transport=httpx.MockTransport(network.handler), follow_redirects=False, trust_env=False)
    transport = HttpTransport(registry.HOSTS, resolver=lambda host: ["194.199.8.183"], client=client)
    clock = Clock()
    return SourceHub(
        database, transport=transport, throttle=Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep
    )


def edition_of(database: Database, work_id: int) -> int:
    with database.session() as session:
        return session.scalar(select(Edition.id).where(Edition.work_id == work_id, Edition.is_primary.is_(True)))


def notice(database: Database, work_id: int):
    with database.session() as session:
        return catalogue_repo.load_notice(session, work_id)


def rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def invariants(settings) -> str:
    """Empreinte de tout ce que l'enrichissement ne doit jamais toucher."""
    digest = hashlib.sha256()
    for table in INVARIANT_TABLES:
        for row in rows(settings, f"SELECT * FROM {table} ORDER BY 1"):  # noqa: S608 : constante
            digest.update(repr(row).encode())
    return digest.hexdigest()


def petit_prince_network() -> Network:
    return (
        Network()
        .when(f"bib.isbn%20any%20%22{PETIT_PRINCE}", "bnf/isbn-exact-petit-prince")
        .when(f"isb%3D{PETIT_PRINCE}", "sudoc/sru-isbn-petit-prince")
    )


def first_proposal(outcome: service.SearchOutcome, source: str = "bnf") -> int:
    return next(pid for key, pid in outcome.proposals.items() if key.startswith(source + ":"))


# --- Recherche --------------------------------------------------------------------------------------------------------
def test_exact_isbn_search_queries_both_sources_and_stores_proposals(settings, database):
    work = make_notice(settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn=PETIT_PRINCE)
    network = petit_prince_network()
    outcome = service.search(database, make_hub(database, network), edition_id=edition_of(database, work), user_id=None)
    assert outcome.outcome == "exact" and outcome.levels == (1,)
    assert {s.code: s.status for s in outcome.states} == {"bnf": "found", "sudoc": "found"}
    assert network.count("catalogue.bnf.fr") == 1 and network.count("sudoc.abes.fr") == 1
    assert set(outcome.proposals) == {"bnf:ark:/12148/cb41023439w", "sudoc:117074047"}
    stored = rows(
        settings, "SELECT match_class, match_level, status, source_agency FROM enrichment_proposals ORDER BY id"
    )
    assert stored == [("exact", 1, "pending", "FR-751131015"), ("exact", 1, "pending", "Abes")]
    assert rows(settings, "SELECT outcome FROM enrichment_searches") == [("exact",)]
    assert "$5" not in rows(settings, "SELECT raw_record FROM enrichment_proposals WHERE source_code = 'sudoc'")[0][0]


def test_a_failing_source_is_reported_not_mistaken_for_no_result(settings, database):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    network = (
        Network().when("catalogue.bnf.fr", "panne", 503).when(f"isb%3D{PETIT_PRINCE}", "sudoc/sru-isbn-petit-prince")
    )
    outcome = service.search(database, make_hub(database, network), edition_id=edition_of(database, work), user_id=None)
    states = {s.code: s for s in outcome.states}
    assert states["bnf"].status == "unavailable" and states["bnf"].retried
    assert states["sudoc"].status == "found"
    assert outcome.outcome == "exact" and not outcome.complete


def test_nothing_found_is_none_when_complete_and_incomplete_otherwise(settings, database):
    work = make_notice(settings, "Un titre inconnu", isbn="9782999999992")
    outcome = service.search(
        database, make_hub(database, Network()), edition_id=edition_of(database, work), user_id=None
    )
    assert outcome.outcome == "none" and outcome.complete
    down = Network().when("catalogue.bnf.fr", "panne", 503)
    hub = make_hub(database, down)
    outcome = service.search(database, hub, edition_id=edition_of(database, work), user_id=None, refresh=True)
    assert outcome.outcome == "incomplete"  # « aucun résultat » du cache n'est pas servi : la recherche est actualisée


def test_without_isbn_levels_follow_the_criteria_and_sudoc_waits_for_the_bnf(settings, database):
    """D3 : la deuxième source n'est appelée qu'à défaut d'une correspondance exacte ou forte de la première."""
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2007"
    )
    network = Network().when("bib.title%20all%20%22le%20petit%20prince", "bnf/isbn-exact-petit-prince")
    outcome = service.search(database, make_hub(database, network), edition_id=edition_of(database, work), user_id=None)
    assert outcome.outcome == "strong" and outcome.levels == (2,)
    assert network.count("sudoc") == 0
    # Recette sur DEV : le Sudoc ne disparaît pas de l'écran, il est dit « non interrogé » et rien ne manque
    assert {s.code: s.status for s in outcome.states} == {"bnf": "found", "sudoc": "not_needed"}
    assert outcome.complete


def test_the_second_search_comes_from_the_cache(settings, database):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    network = petit_prince_network()
    hub = make_hub(database, network)
    service.search(database, hub, edition_id=edition_of(database, work), user_id=None)
    calls = len(network.calls)
    outcome = service.search(database, hub, edition_id=edition_of(database, work), user_id=None)
    assert len(network.calls) == calls
    assert all(s.from_cache_at for s in outcome.states)
    refreshed = service.search(database, hub, edition_id=edition_of(database, work), user_id=None, refresh=True)
    assert len(network.calls) == calls + 2 and not any(s.from_cache_at for s in refreshed.states)


def test_a_disabled_source_is_never_called(settings, database):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE)
    rows(settings, "SELECT 1")
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE metadata_sources SET is_enabled = 0 WHERE code = 'sudoc'")
    connection.commit()
    connection.close()
    network = petit_prince_network()
    outcome = service.search(database, make_hub(database, network), edition_id=edition_of(database, work), user_id=None)
    assert network.count("sudoc") == 0
    assert {s.code: s.status for s in outcome.states} == {"bnf": "found", "sudoc": "disabled"}
    assert outcome.complete


def test_explicit_criteria_override_the_fiche(settings, database):
    work = make_notice(settings, "Autre titre")
    network = petit_prince_network()
    outcome = service.search(
        database,
        make_hub(database, network),
        edition_id=edition_of(database, work),
        criteria=Criteria(isbn=PETIT_PRINCE),
        user_id=None,
    )
    assert outcome.levels == (1,) and any(
        a.candidate.external_id == "ark:/12148/cb41023439w" for a in outcome.assessments
    )
    assert rows(settings, "SELECT criteria FROM enrichment_searches")[0][0].startswith('{"isbn": "9782070612758"')


# --- Application ------------------------------------------------------------------------------------------------------
def searched(settings, database, **notice_fields):
    work = make_notice(settings, notice_fields.pop("title", "Le Petit Prince"), isbn=PETIT_PRINCE, **notice_fields)
    edition = edition_of(database, work)
    outcome = service.search(database, make_hub(database, petit_prince_network()), edition_id=edition, user_id=None)
    return work, edition, outcome


def test_only_the_accepted_fields_are_applied(settings, database):
    """V15 : validation de certains champs seulement ; les autres sont des refus tracés."""
    work, _edition, outcome = searched(settings, database)
    before = invariants(settings)
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"publisher", "year"})),
        user_id=None,
    )
    assert set(result.applied) == {"publisher", "year"}
    assert {"pages", "collection", "contributors"} <= set(result.rejected)
    view = notice(database, work)
    assert view.primary.publisher.label == "Gallimard" and view.primary.year == 2007
    assert view.primary.pages is None and view.contributors == () and view.summary is None
    assert rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", first_proposal(outcome)) == [
        ("partial",)
    ]
    decisions = rows(settings, "SELECT field, decision, action FROM enrichment_decisions ORDER BY field")
    assert ("publisher", "accepted", "fill") in decisions and ("pages", "rejected", None) in decisions
    provenance = rows(
        settings, "SELECT field, source_code, external_id, license_note FROM field_provenance ORDER BY field"
    )
    assert provenance == [
        ("publisher", "bnf", "ark:/12148/cb41023439w", service.LICENCES["bnf"]),
        ("year", "bnf", "ark:/12148/cb41023439w", service.LICENCES["bnf"]),
    ]
    assert invariants(settings) == before  # V20


def test_a_filled_field_is_never_replaced_without_its_own_box(settings, database):
    """V16 : aucun écrasement silencieux."""
    work, edition, outcome = searched(settings, database, publisher="Galimard")
    proposal = first_proposal(outcome)
    service.apply(database, proposal_id=proposal, acceptance=Acceptance(fields=frozenset({"year"})), user_id=None)
    assert notice(database, work).primary.publisher.label == "Galimard"
    _, edition, outcome = searched(settings, database, title="Le Petit Prince bis", publisher="Galimard")
    work_bis = notice(database, edition_of_work(database, edition)).id
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"publisher"})),
        user_id=None,
    )
    assert result.applied == ("publisher",)
    assert notice(database, work_bis).primary.publisher.label == "Gallimard"
    decision = rows(
        settings,
        "SELECT action, old_value, new_value FROM enrichment_decisions"
        " WHERE field = 'publisher' AND decision = 'accepted'",
    )
    assert decision == [("replace", '"Galimard"', '"Gallimard"')]


def edition_of_work(database: Database, edition_id: int) -> int:
    with database.session() as session:
        return session.get(Edition, edition_id).work_id


def test_a_third_party_summary_is_not_copied(settings, database):
    work, _edition, outcome = searched(settings, database)
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"summary"})),
        user_id=None,
    )
    assert result.applied == () and ("summary", "champ non applicable") in result.skipped
    assert notice(database, work).summary is None


def test_contributors_are_added_with_the_chosen_role_without_duplicate_agents(settings, database):
    make_notice(settings, "Vol de nuit", authors=("Antoine de Saint-Exupéry",))
    work, _edition, outcome = searched(settings, database)
    before = rows(settings, "SELECT COUNT(*) FROM agents")[0][0]
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(
            fields=frozenset({"contributors"}), contributors=(("Antoine de Saint-Exupéry", "author"),)
        ),
        user_id=None,
    )
    assert result.applied == ("contributors",)
    view = notice(database, work)
    assert [(c.name, c.role) for c in view.contributors] == [("Antoine de Saint-Exupéry", "author")]
    assert rows(settings, "SELECT COUNT(*) FROM agents")[0][0] == before  # l'auteur existant est réutilisé


def test_an_unknown_role_or_unproposed_person_is_skipped(settings, database):
    work, _edition, outcome = searched(settings, database)
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(
            fields=frozenset({"contributors"}),
            contributors=(("Antoine de Saint-Exupéry", "pilote"), ("Inconnu", "author")),
        ),
        user_id=None,
    )
    assert result.applied == () and len(result.skipped) >= 2
    assert notice(database, work).contributors == ()


def test_keywords_and_known_genres_from_the_sudoc(settings, database):
    make_notice(settings, "Un conte", genres="Contes")
    work, _edition, outcome = searched(settings, database)
    proposal = first_proposal(outcome, "sudoc")
    result = service.apply(
        database, proposal_id=proposal, acceptance=Acceptance(fields=frozenset({"keywords", "genres"})), user_id=None
    )
    assert set(result.applied) == {"keywords", "genres"}
    view = notice(database, work)
    assert "Écoles" in view.keywords and view.genres == ("Contes",)
    assert rows(settings, "SELECT COUNT(*) FROM tags WHERE kind = 'genre'")[0][0] == 1  # aucun genre inventé


def test_isbn_is_confirmed_only_when_the_edition_is_established(settings, database):
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2007"
    )
    edition = edition_of(database, work)
    network = Network().when("bib.title%20all%20%22le%20petit%20prince", "bnf/isbn-exact-petit-prince")
    outcome = service.search(database, make_hub(database, network), edition_id=edition, user_id=None)
    assert outcome.outcome == "strong"
    proposal = first_proposal(outcome)
    result = service.apply(
        database, proposal_id=proposal, acceptance=Acceptance(fields=frozenset({"isbn", "pages"})), user_id=None
    )
    # forte avec même éditeur et même année : l'édition est établie, l'ISBN est confirmé
    assert result.isbn_status == "confirmed" and "isbn" in result.applied
    assert notice(database, work).primary.isbn13 == (PETIT_PRINCE,)
    assert rows(settings, "SELECT status FROM edition_isbn_candidates") == [("confirmed",)]


def test_isbn_stays_proposed_when_the_edition_is_not_established(settings, database):
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2015"
    )
    edition = edition_of(database, work)
    network = Network().when("bib.title%20all%20%22le%20petit%20prince", "bnf/isbn-exact-petit-prince")
    outcome = service.search(database, make_hub(database, network), edition_id=edition, user_id=None)
    proposal = next(iter(outcome.proposals.values()))
    result = service.apply(
        database, proposal_id=proposal, acceptance=Acceptance(fields=frozenset({"isbn"})), user_id=None
    )
    assert result.isbn_status == "proposed" and "isbn" not in result.applied
    assert notice(database, work).primary.isbn13 == ()
    assert rows(settings, "SELECT status, isbn13 FROM edition_isbn_candidates") == [("proposed", PETIT_PRINCE)]
    assert rows(settings, "SELECT COUNT(*) FROM edition_identifiers WHERE value = ?", PETIT_PRINCE) == [(0,)]
    # « J'ai vérifié cet ISBN sur le livre » : confirmé
    outcome = service.search(database, make_hub(database, network), edition_id=edition, user_id=None)
    proposal = next(iter(outcome.proposals.values()))
    result = service.apply(
        database,
        proposal_id=proposal,
        acceptance=Acceptance(fields=frozenset({"isbn"}), confirm_isbn=True),
        user_id=None,
    )
    assert result.isbn_status == "confirmed"
    assert notice(database, work).primary.isbn13 == (PETIT_PRINCE,)
    assert rows(settings, "SELECT status FROM edition_isbn_candidates") == [("confirmed",)]


def test_an_unchecked_isbn_is_recorded_as_rejected(settings, database):
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2015"
    )
    network = Network().when("bib.title%20all%20%22le%20petit%20prince", "bnf/isbn-exact-petit-prince")
    outcome = service.search(database, make_hub(database, network), edition_id=edition_of(database, work), user_id=None)
    result = service.apply(
        database,
        proposal_id=next(iter(outcome.proposals.values())),
        acceptance=Acceptance(fields=frozenset({"pages"})),
        user_id=None,
    )
    assert result.isbn_status == "rejected"
    assert rows(settings, "SELECT status FROM edition_isbn_candidates") == [("rejected",)]


def test_a_modified_fiche_makes_the_proposal_stale(settings, database):
    """V17 : conflit de modification concurrente."""
    work, _edition, outcome = searched(settings, database)
    view = notice(database, work)
    catalogue.update_content(
        database, work, catalogue.ContentInput(summary="Résumé saisi entre-temps"), view.version, None
    )
    with pytest.raises(ConflictError):
        service.apply(
            database,
            proposal_id=first_proposal(outcome),
            acceptance=Acceptance(fields=frozenset({"publisher"})),
            user_id=None,
        )
    assert rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", first_proposal(outcome)) == [
        ("stale",)
    ]
    assert notice(database, work).primary.publisher is None


def test_a_proposal_is_applied_once(settings, database):
    _work, _edition, outcome = searched(settings, database)
    service.apply(
        database, proposal_id=first_proposal(outcome), acceptance=Acceptance(fields=frozenset({"year"})), user_id=None
    )
    with pytest.raises(Exception, match="déjà été traitée"):
        service.apply(
            database,
            proposal_id=first_proposal(outcome),
            acceptance=Acceptance(fields=frozenset({"pages"})),
            user_id=None,
        )


def test_preview_lists_the_differences(settings, database):
    _work, edition, outcome = searched(settings, database)
    target, candidate, assessment, diffs = service.preview(database, proposal_id=first_proposal(outcome))
    assert target.edition.id == edition and candidate.external_id == "ark:/12148/cb41023439w"
    assert assessment.isbn_equal and {d.field for d in diffs} >= {"publisher", "year", "pages", "summary", "cover"}


# --- Annulation -------------------------------------------------------------------------------------------------------
def test_revert_restores_the_old_values(settings, database):
    """V18 : annulation sans perte d'une modification postérieure."""
    work, _edition, outcome = searched(settings, database, publisher="Galimard")
    before = invariants(settings)
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"publisher", "year", "pages"})),
        user_id=None,
    )
    reverted = service.revert(database, operation_id=result.operation_id, user_id=None)
    assert reverted.restored == ("pages", "publisher", "year") and reverted.conflicts == ()
    view = notice(database, work)
    assert (view.primary.publisher.label, view.primary.year, view.primary.pages) == ("Galimard", None, None)
    assert rows(settings, "SELECT COUNT(*) FROM field_provenance") == [(0,)]
    assert rows(settings, "SELECT status FROM enrichment_proposals WHERE id = ?", first_proposal(outcome)) == [
        ("reverted",)
    ]
    assert rows(
        settings, "SELECT COUNT(*) FROM enrichment_decisions WHERE reverted_at IS NULL AND decision = 'accepted'"
    ) == [(0,)]
    assert invariants(settings) == before
    assert service.revert(database, operation_id=result.operation_id, user_id=None).nothing_to_do


def test_revert_never_overwrites_a_later_change_unless_forced(settings, database):
    work, edition, outcome = searched(settings, database)
    result = service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"publisher", "year"})),
        user_id=None,
    )
    view = notice(database, work)
    catalogue.update_edition(
        database,
        edition,
        catalogue.EditionInput(isbn=PETIT_PRINCE, publisher="Gallimard jeunesse", year="2007"),
        view.primary.version,
        None,
    )
    reverted = service.revert(database, operation_id=result.operation_id, user_id=None)
    assert reverted.restored == ("year",)
    assert [c[0] for c in reverted.conflicts] == ["publisher"]
    assert reverted.conflicts[0][1:] == ("Gallimard", "Gallimard jeunesse", "")
    view = notice(database, work)
    assert view.primary.publisher.label == "Gallimard jeunesse" and view.primary.year is None
    forced = service.revert(database, operation_id=result.operation_id, user_id=None, force=frozenset({"publisher"}))
    assert forced.restored == ("publisher",)
    assert notice(database, work).primary.publisher is None


def test_revert_of_a_confirmed_isbn_puts_it_back_to_proposed(settings, database):
    work = make_notice(
        settings, "Le petit prince", authors=("Antoine de Saint-Exupéry",), publisher="Gallimard", year="2007"
    )
    edition = edition_of(database, work)
    network = Network().when("bib.title%20all%20%22le%20petit%20prince", "bnf/isbn-exact-petit-prince")
    outcome = service.search(database, make_hub(database, network), edition_id=edition, user_id=None)
    result = service.apply(
        database, proposal_id=first_proposal(outcome), acceptance=Acceptance(fields=frozenset({"isbn"})), user_id=None
    )
    assert result.isbn_status == "confirmed"
    service.revert(database, operation_id=result.operation_id, user_id=None)
    assert notice(database, work).primary.isbn13 == ()
    assert rows(settings, "SELECT status FROM edition_isbn_candidates") == [("proposed",)]


def test_provenance_marks_a_field_changed_by_hand(settings, database):
    work, edition, outcome = searched(settings, database)
    service.apply(
        database,
        proposal_id=first_proposal(outcome),
        acceptance=Acceptance(fields=frozenset({"publisher", "year"})),
        user_id=None,
    )
    with database.session() as session:
        target = repo.load_target(session, edition)
        assert target is not None
        marks = {p.field: p.changed_since for p in repo.provenance(session, target.values, work, edition)}
    assert marks == {"publisher": False, "year": False}
    view = notice(database, work)
    catalogue.update_edition(
        database,
        edition,
        catalogue.EditionInput(isbn=PETIT_PRINCE, publisher="Autre", year="2007"),
        view.primary.version,
        None,
    )
    with database.session() as session:
        target = repo.load_target(session, edition)
        assert target is not None
        marks = {p.field: p.changed_since for p in repo.provenance(session, target.values, work, edition)}
    assert marks == {"publisher": True, "year": False}


def test_history_lists_searches_proposals_and_decisions(settings, database):
    _work, edition, outcome = searched(settings, database)
    service.apply(
        database, proposal_id=first_proposal(outcome), acceptance=Acceptance(fields=frozenset({"year"})), user_id=None
    )
    with database.session() as session:
        views = repo.history(session, edition)
    assert len(views) == 1 and views[0].outcome == "exact" and len(views[0].proposals) == 2
    applied = next(p for p in views[0].proposals if p.source_code == "bnf")
    assert applied.status == "partial"
    assert {(d.field, d.decision) for d in applied.decisions} >= {("year", "accepted"), ("publisher", "rejected")}


def test_audit_entries_are_written(settings, database):
    _work, _edition, outcome = searched(settings, database)
    result = service.apply(
        database, proposal_id=first_proposal(outcome), acceptance=Acceptance(fields=frozenset({"year"})), user_id=None
    )
    service.revert(database, operation_id=result.operation_id, user_id=None)
    actions = [
        r[0] for r in rows(settings, "SELECT action FROM audit_log WHERE action LIKE 'enrichment.%' ORDER BY id")
    ]
    assert actions == ["enrichment.search", "enrichment.apply", "enrichment.revert"]


def test_tables_untouched_by_enrichment_hold_the_copies_series_and_call_numbers(settings, database):
    work = make_notice(
        settings, "Astérix le Gaulois", authors=("René Goscinny",), series="Astérix", volume="1", isbn="9782012101333"
    )
    edition = edition_of(database, work)
    catalogue.add_copies(database, edition, 2, "", None)
    network = Network().when("bib.isbn%20any%20%229782012101333", "bnf/bd-asterix-unimarc")
    before = invariants(settings)
    outcome = service.search(database, make_hub(database, network), edition_id=edition, user_id=None)
    proposal = first_proposal(outcome)
    service.apply(
        database,
        proposal_id=proposal,
        acceptance=Acceptance(fields=frozenset({"publisher", "year", "pages", "statement"})),
        user_id=None,
    )
    assert invariants(settings) == before
    view = notice(database, work)
    assert view.series.title == "Astérix" and view.series.call_sequence == 1 and view.series.volume_text == "1"
    assert len(view.copies) == 2
    assert all(isinstance(r[0], str) for r in rows(settings, "SELECT call_number FROM copies"))
    stale = rows(settings, "SELECT COUNT(*) FROM enrichment_proposals WHERE status = 'pending'")[0][0]
    assert stale == 0  # un seul candidat : appliqué
    with database.session() as session:
        assert (
            session.scalar(
                select(EditionIdentifier.value).where(
                    EditionIdentifier.edition_id == edition, EditionIdentifier.scheme == "isbn13"
                )
            )
            == "9782012101333"
        )
        assert session.scalar(select(EnrichmentProposal.id)) is not None
        assert session.scalar(select(EnrichmentDecision.id)) is not None
        assert session.scalar(select(FieldProvenance.field)) is not None
        assert session.scalar(select(EditionIsbnCandidate.id)) is None  # ISBN déjà présent : rien à proposer
