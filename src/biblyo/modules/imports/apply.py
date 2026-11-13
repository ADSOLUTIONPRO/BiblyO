# SPDX-License-Identifier: EUPL-1.2
"""Application d'un import simulé (lot L6, tâche T5 ; conceptions 20 et 21).

L'application n'écrit que le plan qu'une personne a lu. Dans l'ordre :

1. **un seul import à la fois** : un verrou de fichier, libéré par le système si le processus s'arrête ;
2. **le plan lu est le plan appliqué** : la simulation est refaite sur les fichiers déposés (leur empreinte est
   revérifiée), avec le profil et l'état actuel du catalogue ; son empreinte doit être exactement celle que la
   personne a confirmée. Un fichier changé, un profil changé ou un catalogue modifié entre-temps : refusé ;
3. **rien de bloquant** : ni ligne refusée, ni correspondance probable avec une notice existante, ni reconstruction
   de l’ancienne application, ni fichier déjà appliqué ;
4. **une sauvegarde vérifiée** de l'instance, prise avant la moindre écriture, dont le nom est gardé dans le lot ;
5. **une seule transaction** (`BEGIN IMMEDIATE`) : la simulation y est refaite une dernière fois, puis tout est écrit
   (séries, œuvres, éditions, exemplaires, lignes, liens, propositions) et **réconcilié** avec le plan avant la
   validation. Un écart, une erreur, une coupure : tout est annulé, rien n'est écrit, et l'échec est consigné à part.

Rien d'existant n'est modifié : une série, un contributeur, un éditeur ou une collection déjà au catalogue est
**rejoint** (lien marqué « existant »), jamais réécrit. L'index unique sur l'empreinte du fichier appliqué et
l'unicité des numéros d'inventaire garantissent dans la base elle-même qu'un second import ne duplique rien.

Enfin, l'application est **désactivée par défaut** sur toute instance (`imports.application_enabled`) : un
administrateur l'active sur le serveur (`biblyo import application --activer`). L'instance DEV permanente la garde
désactivée : le catalogue réel ne peut pas y être importé par erreur (D-L6-10).
"""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.config import Settings
from biblyo.core import audit
from biblyo.core.backup import BackupError, create_backup
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.isbn import parse_isbn
from biblyo.core.settings_store import get_setting
from biblyo.core.text import normalize, sort_name, title_sort
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    DocumentType,
    Edition,
    EditionIdentifier,
    Publisher,
    Series,
    Work,
    WorkContributor,
    WorkSeries,
)
from biblyo.modules.catalogue.rules import completeness
from biblyo.modules.enrichment.models import EditionIsbnCandidate
from biblyo.modules.imports import profile as profiles
from biblyo.modules.imports import simulate
from biblyo.modules.imports.models import ImportBatch, ImportLink, ImportProposal, ImportRow
from biblyo.modules.imports.reader import Workbook

SCHEMA_VERSION = "0012"
ENABLED_SETTING = "imports.application_enabled"
SOURCE_CODE = "import"  # fournisseur des ISBN proposés par un classeur importé
LOCK_NAME = ".application.lock"
COPY_STATUS = "in_processing"  # « En préparation » : non rangé dans biblyO tant qu'il n'a pas de place (D-L6-8)

_WEB_ADDRESS = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def safe_url(value: str) -> str:
    """Adresse lue dans un fichier : gardée comme lien seulement si c'est une adresse web (http, https). Une autre
    (« javascript: », « file: »…) n'est jamais un lien : son texte reste dans la note de la proposition."""
    text = (value or "").strip()
    return text if _WEB_ADDRESS.match(text) else ""


#: Sorte de proposition de la simulation → valeur de `import_proposals.field`.
PROPOSAL_FIELD = {
    "isbn": "isbn",
    "publication_year": "publication_year",
    "contributor": "contributor",
    "publisher": "publisher",
    "name_form": "name_form",
    "name_split": "name_split",
    "organization": "organization",
    "volume": "volume",
    "search_form": "search_form",
    "condition": "condition",
    "location": "other",  # place indiquée par le fichier : jamais un rangement (révision 0012 figée)
    "other": "other",
}


class ApplyRefused(BusinessError):
    """L'application est refusée avant toute écriture : le message dit pourquoi et quoi faire."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class ApplyFailed(BusinessError):
    """L'écriture a échoué : la transaction est annulée, rien n'est écrit, l'échec est consigné."""

    def __init__(self, message: str, batch_id: int | None) -> None:
        super().__init__(message)
        self.batch_id = batch_id


@dataclass
class Applied:
    batch_id: int
    public_id: str
    backup_name: str
    summary: dict[str, Any]


@dataclass
class ApplyRequest:
    """Ce que la personne a lu et confirmé."""

    workbook: Workbook
    sheet: str
    profile: profiles.Profile
    expected_plan: str
    user_id: int | None
    complement: Workbook | None = None
    complement_sheet: str = ""


#: Point d'arrêt des essais : appelé après chaque étape et chaque ligne écrite, il peut lever une exception pour
#: simuler une coupure en plein import.
Checkpoint = Callable[[str, int], None]


@contextmanager
def _exclusive(data_dir: Path) -> Iterator[None]:
    folder = data_dir / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK_NAME).open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ApplyRefused(_("Un import est déjà en cours d'application : attendez qu'il se termine.")) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _simulate(session: Session, request: ApplyRequest) -> simulate.Simulation:
    return simulate.simulate(
        request.workbook,
        request.sheet,
        request.profile,
        complement=request.complement,
        complement_sheet=request.complement_sheet,
        existing=simulate.snapshot(session),
    )


def _already_applied(session: Session, sha256: str) -> ImportBatch | None:
    return session.scalar(
        select(ImportBatch).where(ImportBatch.file_sha256 == sha256, ImportBatch.status.in_(("applying", "applied")))
    )


def enabled(session: Session) -> bool:
    """L'application des imports est-elle permise sur cette instance ? Non, tant qu'un administrateur ne l'a pas dit."""
    return get_setting(session, ENABLED_SETTING, False) is True


def _check(session: Session, request: ApplyRequest, sim: simulate.Simulation) -> None:
    """Tout ce qui interdit l'application, avant comme dans la transaction."""
    if not enabled(session):
        raise ApplyRefused(
            _("L'application des imports est désactivée sur cette instance. Un administrateur l'active sur le serveur "
              "(biblyo import application --activer), seulement là où l'import est décidé.")
        )  # fmt: skip
    previous = _already_applied(session, sim.file_sha256)
    if previous is not None:
        raise ApplyRefused(
            _("Ce fichier a déjà été appliqué (lot n° {id}, le {at}) : rien n'est importé deux fois.").format(
                id=previous.id, at=(previous.applied_at or previous.created_at)[:10]
            )
        )
    if sim.plan_sha256 != request.expected_plan:
        raise ApplyRefused(
            _("Le plan a changé depuis la simulation que vous avez lue : relancez la simulation et relisez-la."),
            [
                _("Empreinte confirmée : {a}").format(a=request.expected_plan[:16]),
                _("Empreinte actuelle : {b}").format(b=sim.plan_sha256[:16]),
                _("État du catalogue : {f}").format(f=sim.existing_fingerprint),
            ],
        )
    if sim.blockers:
        raise ApplyRefused(_("L'import ne peut pas être appliqué en l'état."), list(sim.blockers))
    if session.scalar(select(DocumentType.id).where(DocumentType.code == request.profile.document_type)) is None:
        raise ApplyRefused(_("Type de document inconnu : {code}.").format(code=request.profile.document_type))
    for value, collection_id in request.profile.collection_links.items():
        if session.get(Collection, collection_id) is None:
            raise ApplyRefused(
                _("La collection n° {id}, choisie pour « {v} », n'existe plus.").format(id=collection_id, v=value)
            )


def apply(
    database: Database, settings: Settings, request: ApplyRequest, *, checkpoint: Checkpoint | None = None
) -> Applied:
    """Applique le plan confirmé. Refuse (`ApplyRefused`) ou échoue (`ApplyFailed`) sans rien écrire au catalogue."""
    with _exclusive(settings.data_dir):
        with database.session() as session:
            _check(session, request, _simulate(session, request))
            session.rollback()
        try:
            backup = create_backup(settings, "avant-import")
        except BackupError as exc:
            raise ApplyRefused(_("Sauvegarde impossible : rien n'est écrit. {e}").format(e=exc)) from exc
        try:
            with database.session(write=True) as session:
                sim = _simulate(session, request)
                _check(session, request, sim)
                writer = _Writer(session, sim, request, backup.name, checkpoint)
                batch = writer.run()
                return Applied(batch.id, batch.public_id or "", backup.name, json.loads(batch.summary or "{}"))
        except ApplyRefused:
            raise
        except Exception as exc:  # toute autre erreur : la transaction est annulée, l'échec est consigné à part
            batch_id = _record_failure(database, request, backup.name, exc)
            raise ApplyFailed(
                _("L'import a échoué et a été entièrement annulé : rien n'a été écrit au catalogue ({e}).").format(
                    e=type(exc).__name__
                ),
                batch_id,
            ) from exc


def _record_failure(database: Database, request: ApplyRequest, backup_name: str, exc: BaseException) -> int | None:
    try:
        with database.session(write=True) as session:
            moment = now_iso()
            batch = ImportBatch(
                target="catalogue",
                filename=request.workbook.filename,
                file_sha256=request.workbook.sha256,
                file_bytes=request.workbook.size,
                sheet=request.sheet,
                mapping=request.profile.to_json(),
                schema_version=SCHEMA_VERSION,
                plan_sha256=request.expected_plan,
                status="failed",
                summary=json.dumps({"erreur": type(exc).__name__, "message": str(exc)[:500]}, ensure_ascii=False),
                created_by=request.user_id,
                created_at=moment,
                public_id=new_public_id(),
                complement_filename=request.complement.filename if request.complement else None,
                complement_sha256=request.complement.sha256 if request.complement else None,
                backup_name=backup_name,
            )
            session.add(batch)
            session.flush()
            audit.record(
                session, "import.failed", "import_batch", batch.id, user_id=request.user_id,
                details={"erreur": type(exc).__name__},
            )  # fmt: skip
            return batch.id
    except Exception:
        return None


@dataclass
class _Created:
    """Ce qui a été écrit, pour la réconciliation : référence du plan → identifiant, et s'il a été créé."""

    ids: dict[str, int] = field(default_factory=dict)
    created: set[str] = field(default_factory=set)

    def put(self, ref: str, entity_id: int, *, new: bool) -> None:
        self.ids[ref] = entity_id
        if new:
            self.created.add(ref)


class _Writer:
    def __init__(
        self,
        session: Session,
        sim: simulate.Simulation,
        request: ApplyRequest,
        backup_name: str,
        checkpoint: Checkpoint | None,
    ) -> None:
        self.session = session
        self.sim = sim
        self.request = request
        self.profile = request.profile
        self.backup_name = backup_name
        self.checkpoint = checkpoint or (lambda _stage, _n: None)
        self.moment = now_iso()
        self.user_id = request.user_id
        self.publishers = _Created()
        self.agents = _Created()
        self.series = _Created()
        self.works = _Created()
        self.books = _Created()
        self.collections: dict[tuple[int | None, str], tuple[int, bool]] = {}
        self.candidates: dict[tuple[int, str], int] = {}
        self.counts: dict[str, int] = {}
        self.firsts: dict[str, set[str]] = {
            kind: set() for kind in ("work", "edition", "series", "agent", "publisher", "collection")
        }

    def _first(self, kind: str, ref: str, created: _Created) -> bool:
        """Vrai pour la première ligne qui rejoint une entité créée par cet import : c'est elle qui l'a créée."""
        if ref in self.firsts[kind]:
            return False
        self.firsts[kind].add(ref)
        return ref in created.created

    # --- Étapes -------------------------------------------------------------------------------------------------------
    def run(self) -> ImportBatch:
        sim = self.sim
        batch = ImportBatch(
            target="catalogue",
            profile_id=None,
            filename=sim.filename,
            file_sha256=sim.file_sha256,
            file_bytes=self.request.workbook.size,
            sheet=sim.sheet,
            mapping=self.profile.to_json(),
            schema_version=SCHEMA_VERSION,
            plan_sha256=sim.plan_sha256,
            status="applying",
            created_by=self.user_id,
            applied_by=self.user_id,
            created_at=self.moment,
            simulated_at=self.moment,
            public_id=new_public_id(),
            complement_filename=sim.complement_filename or None,
            complement_sha256=sim.complement_sha256 or None,
            backup_name=self.backup_name,
        )
        self.session.add(batch)
        self.session.flush()  # l'index unique partiel refuse ici un second lot « en cours » du même fichier
        self.batch = batch
        self.document_type_id = self.session.scalar(
            select(DocumentType.id).where(DocumentType.code == self.profile.document_type)
        )
        self._publishers()
        self.checkpoint("publishers", len(self.publishers.ids))
        self._agents()
        self.checkpoint("agents", len(self.agents.ids))
        self._series()
        self.checkpoint("series", len(self.series.ids))
        self._works()
        self.checkpoint("works", len(self.works.ids))
        self._editions()
        self.checkpoint("editions", len(self.books.ids))
        self._rows()
        self._derived()
        reconciliation = self._reconcile()
        failed = [line for line in reconciliation if not line["ok"]]
        if failed:
            raise RuntimeError("Réconciliation en écart : " + " ; ".join(line["label"] for line in failed))
        batch.status = "applied"
        batch.applied_at = now_iso()
        batch.summary = json.dumps(self._summary(reconciliation), ensure_ascii=False, sort_keys=True)
        audit.record(
            self.session,
            "import.applied",
            "import_batch",
            batch.id,
            user_id=self.user_id,
            details={
                "lignes": sim.totals["rows_read"],
                "exemplaires": self.counts.get("copies", 0),
                "livres": len(self.books.created),
                "oeuvres": len(self.works.created),
                "empreinte_fichier": sim.file_sha256[:16],
                "empreinte_plan": sim.plan_sha256[:16],
                "sauvegarde": self.backup_name,
            },
        )
        self.checkpoint("commit", 0)
        return batch

    def _publishers(self) -> None:
        for key, plan in self.sim.publishers.items():
            if plan.existing_id is not None:
                self.publishers.put(key, plan.existing_id, new=False)
                continue
            found = self.session.scalar(select(Publisher.id).where(Publisher.name_norm == key))
            if found is not None:
                self.publishers.put(key, found, new=False)
                continue
            record = Publisher(name=plan.label, name_norm=key, created_at=self.moment)
            self.session.add(record)
            self.session.flush()
            self.publishers.put(key, record.id, new=True)

    def _agents(self) -> None:
        for written, plan in self.sim.agents.items():
            if plan.existing_id is not None:
                self.agents.put(written, plan.existing_id, new=False)
                continue
            # D-L6-13 : le nom reste tel qu'écrit ; une organisation probable reste une personne, proposée
            record = Agent(
                kind="person",
                display_name=written,
                sort_name=sort_name(written),
                name_norm=normalize(written),
                created_at=self.moment,
            )
            self.session.add(record)
            self.session.flush()
            self.agents.put(written, record.id, new=True)

    def _series(self) -> None:
        for ref, plan in self.sim.series.items():
            if plan.existing_id is not None:
                self.series.put(ref, plan.existing_id, new=False)
                continue
            title = simulate.natural_title(plan.title)  # D-L6-11 : affiché dans l'ordre naturel
            record = Series(
                public_id=new_public_id(),
                title=title,
                title_sort=title_sort(title),
                title_norm=normalize(title),
                next_call_sequence=1,
                status="unknown",
                created_at=self.moment,
                updated_at=self.moment,
            )
            self.session.add(record)
            self.session.flush()
            self.series.put(ref, record.id, new=True)

    def _works(self) -> None:
        by_number = {row.number: row for row in self.sim.rows}
        for ref, plan in self.sim.works.items():
            if plan.existing_id is not None:  # bloqué par la simulation : jamais ici
                raise RuntimeError(f"Œuvre {ref} : correspondance existante non tranchée.")
            title = simulate.natural_title(plan.title)
            record = Work(
                public_id=new_public_id(),
                document_type_id=self.document_type_id,
                title=title,
                title_sort=title_sort(title),
                title_norm=normalize(title),
                status="active",
                completeness=0,
                version=1,
                created_at=self.moment,
                updated_at=self.moment,
                created_by=self.user_id,
                updated_by=self.user_id,
            )
            self.session.add(record)
            self.session.flush()
            self.works.put(ref, record.id, new=True)
            if plan.series:
                label = (
                    plan.volume if plan.volume_number is None or plan.volume != _number(plan.volume_number) else None
                )
                self.session.add(
                    WorkSeries(
                        work_id=record.id,
                        series_id=self.series.ids[plan.series],
                        volume_number=plan.volume_number,
                        volume_label=label or None,
                        call_sequence=None,  # la cote du Club reste celle de l'exemplaire (D-L6-6, prudent)
                        is_primary=True,
                    )
                )
            # Tous les noms des lignes de l'œuvre, dans l'ordre de première apparition : aucun n'est perdu.
            seen: set[tuple[int, str]] = set()
            for row_number in plan.rows:
                for contributor in by_number[row_number].fields["contributors"]:
                    pair = (self.agents.ids[contributor.written], contributor.role)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    self.session.add(
                        WorkContributor(work_id=record.id, agent_id=pair[0], role=pair[1], position=len(seen) - 1)
                    )
        self.session.flush()

    def _collection(self, publisher_id: int | None, label: str) -> tuple[int, bool]:
        key = (publisher_id, normalize(label))
        if key in self.collections:
            return self.collections[key][0], False
        match = Collection.publisher_id == publisher_id if publisher_id else Collection.publisher_id.is_(None)
        found = self.session.scalar(select(Collection.id).where(Collection.name_norm == key[1], match))
        if found is not None:
            self.collections[key] = (found, False)
            return found, False
        record = Collection(publisher_id=publisher_id, name=label, name_norm=key[1], created_at=self.moment)
        self.session.add(record)
        self.session.flush()
        self.collections[key] = (record.id, True)
        return record.id, True

    def _editions(self) -> None:
        by_number = {row.number: row for row in self.sim.rows}
        primary_done: set[str] = set()
        self.edition_collection: dict[str, tuple[int, bool] | None] = {}
        for ref, plan in self.sim.books.items():
            publisher_id = self.publishers.ids.get(plan.publisher_key) if plan.publisher else None
            statement = plan.collection if plan.collection_kind == "mention" else None
            notes = plan.collection if plan.collection_kind == "note" else None
            collection: tuple[int, bool] | None = None
            if plan.collection and plan.collection_kind == "collection":
                linked = plan.collection_link
                collection = (int(linked), False) if linked else self._collection(publisher_id, plan.collection_label)
            self.edition_collection[ref] = collection
            members = [by_number[n] for n in plan.rows]
            year = next((m.fields["year"] for m in members if m.fields.get("year")), "")
            record = Edition(
                public_id=new_public_id(),
                work_id=self.works.ids[plan.work],
                is_primary=plan.work not in primary_done,
                publisher_id=publisher_id,
                collection_id=collection[0] if collection else None,
                edition_statement=statement,
                notes=notes,
                publication_year=int(year) if year else None,
                language=self.profile.language,
                status="active",
                origin="import",
                version=1,
                created_at=self.moment,
                updated_at=self.moment,
                created_by=self.user_id,
                updated_by=self.user_id,
            )
            primary_done.add(plan.work)
            self.session.add(record)
            self.session.flush()
            self.books.put(ref, record.id, new=True)
            established = next((m.fields["isbn"] for m in members if m.fields.get("isbn")), "")
            parsed = parse_isbn(established) if established else None
            if parsed is not None:  # un ISBN établi par le fichier (colonne « ISBN »), jamais un ISBN proposé
                self.session.add(EditionIdentifier(edition_id=record.id, scheme="isbn13", value=parsed.isbn13))
                if parsed.isbn10:
                    self.session.add(EditionIdentifier(edition_id=record.id, scheme="isbn10", value=parsed.isbn10))
        self.session.flush()

    def _candidate(self, edition_id: int, row: simulate.RowPlan, proposal: simulate.Proposal) -> int | None:
        parsed = parse_isbn(proposal.value)
        if parsed is None:
            return None  # un ISBN à la clé fausse reste une proposition d'import, jamais un candidat
        key = (edition_id, parsed.isbn13)
        if key in self.candidates:
            return self.candidates[key]
        record = EditionIsbnCandidate(
            edition_id=edition_id,
            isbn13=parsed.isbn13,
            status="proposed",
            source_code=SOURCE_CODE,
            external_id=f"{self.sim.filename} › {self.sim.sheet} › {row.number}",
            external_url=safe_url(proposal.url),
            score=0,
            agreeing="[]",
            conflicting="[]",
            created_at=self.moment,
        )
        self.session.add(record)
        self.session.flush()
        self.candidates[key] = record.id
        return record.id

    def _rows(self) -> None:
        firsts = self.firsts
        copies = 0
        for index, row in enumerate(self.sim.rows):
            fields = row.fields
            book_ref = row.book
            edition_id = self.books.ids[book_ref]
            barcode = fields.get("barcode") or None
            copy = Copy(
                public_id=new_public_id(),
                edition_id=edition_id,
                barcode=barcode,
                inventory_number=row.inventory,
                call_number=fields.get("call_number") or None,
                status=COPY_STATUS,
                is_loanable=True,
                condition=fields.get("condition") or None,
                condition_note=fields.get("condition_note") or None,
                acquisition_note=fields.get("acquisition") or None,
                notes=fields.get("copy_note") or None,
                created_at=self.moment,
                updated_at=self.moment,
                created_by=self.user_id,
                updated_by=self.user_id,
                version=1,
            )
            self.session.add(copy)
            self.session.flush()
            copies += 1
            record = ImportRow(
                batch_id=self.batch.id,
                sheet=self.sim.sheet,
                row_number=row.number,
                raw=json.dumps(row.raw, ensure_ascii=False),
                normalized=json.dumps(self._stored(row, copy), ensure_ascii=False, sort_keys=True),
                action="create",
                severity={"info": "ok"}.get(row.severity, row.severity),
                messages=json.dumps(
                    [
                        {"code": m.code, "text": m.text, "column": m.column, "severity": m.severity}
                        for m in row.messages
                    ],
                    ensure_ascii=False,
                ),
            )
            self.session.add(record)
            self.session.flush()
            links: list[tuple[str, int, bool]] = [("copy", copy.id, True)]
            first = self._first
            links.append(("edition", edition_id, first("edition", book_ref, self.books)))
            links.append(("work", self.works.ids[row.work], first("work", row.work, self.works)))
            if row.series:
                links.append(("series", self.series.ids[row.series], first("series", row.series, self.series)))
            for contributor in fields["contributors"]:
                agent_id = self.agents.ids[contributor.written]
                if not any(t == "agent" and i == agent_id for t, i, _c in links):
                    links.append(("agent", agent_id, first("agent", contributor.written, self.agents)))
            if fields.get("publisher"):
                key = normalize(fields["publisher"])
                links.append(("publisher", self.publishers.ids[key], first("publisher", key, self.publishers)))
            collection = self.edition_collection.get(book_ref)
            if collection is not None:
                ckey = str(collection[0])
                created_now = collection[1] and ckey not in firsts["collection"]
                firsts["collection"].add(ckey)
                links.append(("collection", collection[0], created_now))
            for proposal in row.proposals:
                if proposal.kind == "isbn":
                    candidate = self._candidate(edition_id, row, proposal)
                    if candidate is not None and not any(
                        t == "isbn_candidate" and i == candidate for t, i, _c in links
                    ):
                        links.append(("isbn_candidate", candidate, True))
                item = ImportProposal(
                    edition_id=edition_id,
                    row_id=record.id,
                    field=PROPOSAL_FIELD.get(proposal.kind, "other"),
                    value=proposal.value,
                    source_url=safe_url(proposal.url) or None,
                    note=" · ".join(
                        p
                        for p in (
                            proposal.origin,
                            proposal.column,
                            proposal.note,
                            proposal.url if proposal.url and not safe_url(proposal.url) else "",
                        )
                        if p
                    )
                    or None,
                    status="proposed",
                    created_at=self.moment,
                )
                self.session.add(item)
                self.session.flush()
                links.append(("proposal", item.id, True))
            for kind, entity_id, created in links:
                self.session.add(
                    ImportLink(row_id=record.id, entity_type=kind, entity_id=entity_id, created=1 if created else 0)
                )
            self.session.flush()
            self.checkpoint("row", index + 1)
        self.counts["copies"] = copies

    def _stored(self, row: simulate.RowPlan, copy: Copy) -> dict[str, Any]:
        """Ce que l'import a écrit pour chaque colonne : l'export (T7) restitue la valeur d'origine tant que la valeur
        de biblyO est encore celle-ci, et la valeur modifiée dès qu'une personne l'a changée."""
        work_plan = self.sim.works[row.work]
        book_plan = self.sim.books[row.book]
        work_id = self.works.ids[row.work]
        edition_id = self.books.ids[row.book]
        series_title = simulate.natural_title(self.sim.series[row.series].title) if row.series else None
        if row.series and self.sim.series[row.series].existing_id is not None:
            series_title = self.session.scalar(select(Series.title).where(Series.id == self.series.ids[row.series]))
        collection = self.edition_collection.get(row.book)
        publisher_id = self.publishers.ids.get(book_plan.publisher_key) if book_plan.publisher else None
        contributors = [
            {"column": c.position, "written": c.written, "agent_id": self.agents.ids[c.written], "role": c.role}
            for c in row.fields["contributors"]
        ]
        return {
            "source_id": row.source_id,
            "series": {"id": self.series.ids.get(row.series), "title": series_title} if row.series else None,
            "work": {"id": work_id, "title": simulate.natural_title(work_plan.title)},
            "volume": {"number": work_plan.volume_number, "label": work_plan.volume} if row.series else None,
            "edition": {
                "id": edition_id,
                "publisher_id": publisher_id,
                "publisher": self.session.scalar(select(Publisher.name).where(Publisher.id == publisher_id))
                if publisher_id
                else None,
                "collection_kind": book_plan.collection_kind,
                "collection_id": collection[0] if collection else None,
                "collection": book_plan.collection,
                "collection_name": self.session.scalar(select(Collection.name).where(Collection.id == collection[0]))
                if collection
                else None,
                "year": row.fields.get("year") or None,
                "isbn": row.fields.get("isbn") or None,
            },
            "copy": {
                "id": copy.id,
                "public_id": copy.public_id,
                "inventory": copy.inventory_number,
                "barcode": copy.barcode,
                "call_number": copy.call_number,
                "condition": copy.condition,
                "condition_note": copy.condition_note,
                "acquisition_note": copy.acquisition_note,
                "notes": copy.notes,
            },
            "contributors": contributors,
            "complement": {"row": row.complement_row, "values": row.complement_raw} if row.complement_row else None,
        }

    def _derived(self) -> None:
        """Complétude et index de recherche, dans la même transaction que les œuvres."""
        work_ids = sorted(self.works.ids.values())
        for work_id in work_ids:
            work = self.session.get(Work, work_id)
            assert work is not None
            work.completeness, _missing = completeness(catalogue_repo.completeness_facts(self.session, work))
        self.session.flush()
        search.index_works(self.session, work_ids)

    # --- Réconciliation -----------------------------------------------------------------------------------------------
    def _count(self, kind: str, *, created: bool | None = None) -> int:
        query = (
            select(func.count(func.distinct(ImportLink.entity_id)))
            .join(ImportRow, ImportRow.id == ImportLink.row_id)
            .where(ImportRow.batch_id == self.batch.id, ImportLink.entity_type == kind)
        )
        if created is not None:
            query = query.where(ImportLink.created == (1 if created else 0))
        return int(self.session.scalar(query) or 0)

    def _reconcile(self) -> list[dict[str, Any]]:
        sim = self.sim
        t = sim.totals
        rows = int(self.session.scalar(select(func.count()).where(ImportRow.batch_id == self.batch.id)) or 0)
        copy_ids = list(
            self.session.scalars(
                select(ImportLink.entity_id)
                .join(ImportRow, ImportRow.id == ImportLink.row_id)
                .where(ImportRow.batch_id == self.batch.id, ImportLink.entity_type == "copy")
            )
        )
        inventories = sorted(
            self.session.scalars(select(Copy.inventory_number).where(Copy.id.in_(copy_ids))) if copy_ids else []
        )
        expected_inventories = sorted(r.inventory for r in sim.rows)
        labels = {label for _i, _l, _h, label in sim.sheet_columns}
        raw_complete = all(
            set(json.loads(raw)) == labels
            for raw in self.session.scalars(select(ImportRow.raw).where(ImportRow.batch_id == self.batch.id))
        )
        per_edition = dict(
            self.session.execute(
                select(Copy.edition_id, func.count()).where(Copy.id.in_(copy_ids)).group_by(Copy.edition_id)
            ).all()
        ) if copy_ids else {}  # fmt: skip
        expected_per_edition = {self.books.ids[ref]: len(book.rows) for ref, book in sim.books.items()}
        new_series = sum(1 for s in sim.series.values() if s.existing_id is None)
        new_agents = sum(1 for a in sim.agents.values() if a.existing_id is None)
        proposals = int(
            self.session.scalar(
                select(func.count())
                .select_from(ImportProposal)
                .join(ImportRow, ImportRow.id == ImportProposal.row_id)
                .where(ImportRow.batch_id == self.batch.id)
            )
            or 0
        )
        expected_proposals = sum(len(r.proposals) for r in sim.rows)

        def line(label: str, expected: Any, found: Any) -> dict[str, Any]:
            return {"label": label, "expected": expected, "found": found, "ok": expected == found}

        return [
            line(_("Lignes du fichier gardées"), t["rows_read"], rows),
            line(_("Exemplaires créés, un par ligne"), t["copies"], len(copy_ids)),
            line(_("Numéros d'inventaire, à l'identique"), expected_inventories, inventories),
            line(_("Livres (éditions) créés"), t["books"], self._count("edition", created=True)),
            line(_("Œuvres créées"), t["works"], self._count("work", created=True)),
            line(_("Séries créées"), new_series, self._count("series", created=True)),
            line(_("Contributeurs créés"), new_agents, self._count("agent", created=True)),
            line(_("Exemplaires par livre, conformes au plan"), expected_per_edition, per_edition),
            line(_("Propositions gardées, aucune appliquée"), expected_proposals, proposals),
            line(_("Chaque ligne garde toutes ses colonnes"), True, raw_complete),
        ]

    def _summary(self, reconciliation: list[dict[str, Any]]) -> dict[str, Any]:
        sim = self.sim
        # La liste des inventaires peut être longue : la réconciliation la vérifie, le résumé n'en garde que le compte.
        compact = [
            {**line, "expected": len(line["expected"]), "found": len(line["found"])}
            if isinstance(line["expected"], (list, dict))
            else line
            for line in reconciliation
        ]
        return {
            "plan": sim.plan_sha256,
            "file": {"name": sim.filename, "sha256": sim.file_sha256, "sheet": sim.sheet, "header_row": sim.header_row},
            "complement": {
                "name": sim.complement_filename,
                "sha256": sim.complement_sha256,
                "sheet": sim.complement_sheet,
                "header_row": sim.complement_header_row,
            }
            if sim.complement_filename
            else None,
            "columns": [list(c) for c in sim.sheet_columns],
            "fields": {
                label: self.profile.columns.get(label, profiles.KEEP) for _i, _l, _h, label in sim.sheet_columns
            },
            "complement_columns": [list(c) for c in sim.complement_columns],
            "totals": sim.totals,
            "created": {
                "series": len(self.series.created),
                "works": len(self.works.created),
                "editions": len(self.books.created),
                "copies": self.counts.get("copies", 0),
                "agents": len(self.agents.created),
                "publishers": len(self.publishers.created),
                "collections": sum(1 for _id, new in self.collections.values() if new),
                "isbn_candidates": len(self.candidates),
            },
            "joined": {
                "series": len(self.series.ids) - len(self.series.created),
                "agents": len(self.agents.ids) - len(self.agents.created),
                "publishers": len(self.publishers.ids) - len(self.publishers.created),
                "collections": sum(1 for _id, new in self.collections.values() if not new),
            },
            "reconciliation": compact,
            "differences": [asdict(d) for d in sim.differences],
            "shared_call_numbers": len(sim.shared_call_numbers),
            "backup": self.backup_name,
            "copy_status": COPY_STATUS,
            "barcode_mode": self.profile.barcode,
        }


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
