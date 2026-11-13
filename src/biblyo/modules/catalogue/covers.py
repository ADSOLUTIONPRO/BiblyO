# SPDX-License-Identifier: EUPL-1.2
"""Couvertures : aperçu avant enregistrement, droit d'utilisation, historique (conception 17 § 3, 5 et 6).

Un fichier, une adresse ou une proposition d'une source donne d'abord un **aperçu** : l'image est contrôlée,
réencodée et rangée sous son empreinte (sans ligne `media_files`), et ce qui l'accompagne (édition, origine,
référence, droit d'utilisation) est noté à côté, sous un jeton aléatoire, pour la personne qui l'a envoyée. Rien
n'est rattaché avant la confirmation. Un aperçu abandonné expire après deux heures ; `biblyo medias nettoyer`
retire ses fichiers.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.db import Database
from biblyo.core.errors import FormErrors
from biblyo.core.i18n import _
from biblyo.core.media import StoredImage, store_image
from biblyo.modules.catalogue.models import CoverChange, MediaFile

STAGING_DIR = ".apercus"
STAGED_SECONDS = 2 * 3600
TOKEN = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

RIGHTS: dict[str, str] = {
    "club": _("Photo prise pour le Club"),
    "editeur": _("Image fournie ou autorisée par l’éditeur ou l’auteur"),
    "autre": _("Autre origine, droit d’utilisation vérifié"),
}
KIND_LABELS: dict[str, str] = {
    "upload": _("Fichier importé"),
    "url": _("Image d’une adresse web"),
    "source": _("Recherche de couverture"),
    "enrichment": _("Enrichissement"),
    "remove": _("Couverture supprimée"),
    "restore": _("Couverture précédente rétablie"),
    "revert": _("Enrichissement annulé"),
}
#: correspondance d'une image proposée avec l'édition de la fiche, en trois degrés (arbitrage du 12 novembre 2026) :
#: même édition établie, édition différente, édition indéterminée ; un ouvrage différent n'est jamais proposé. Le motif
#: suit les deux-points. Une édition différente ou indéterminée n'est jamais présentée comme sûre.
MATCH_LABELS: dict[str, tuple[str, str]] = {
    "same_edition": (_("Même édition établie : ISBN identique"), "ok"),
    "presumed": (_("Édition indéterminée : titre, auteur et éditeur concordent"), "due"),
    "undetermined": (_("Édition indéterminée : la fiche n’a pas d’ISBN"), "due"),
    "other_edition": (_("Édition différente : la couverture peut différer"), "late"),
    "work": (_("Édition indéterminée : image de l’œuvre"), "due"),
}
NEEDS_CONFIRMATION = frozenset({"presumed", "undetermined", "other_edition", "work"})
SOURCE_LABELS: dict[str, str] = {
    "bnf": _("BnF, service Couvertures"),
    "openlibrary": _("Open Library"),
}


@dataclass(frozen=True)
class Staged:
    """Un aperçu en attente de confirmation."""

    token: str
    user_id: int | None
    work_id: int
    edition_id: int
    sha256: str
    bytes: int
    width: int
    height: int
    kind: str  # upload, url, source
    source_code: str = ""
    reference: str = ""  # ARK, numéro d'image, adresse saisie
    origin_url: str = ""  # adresse de référence (notice publique, adresse finale)
    rights_note: str = ""  # droit d'utilisation déclaré
    license_note: str = ""  # conditions de la source
    match: str = ""  # correspondance présumée d'une proposition (cover_search.MATCH_LABELS)
    edition_text: str = ""  # édition que représente l'image, selon la source
    needs_rights: bool = False  # la source n'accorde aucun droit : à confirmer
    created: float = 0.0

    @property
    def stored(self) -> StoredImage:
        return StoredImage(self.sha256, self.bytes, self.width, self.height)

    @property
    def path(self) -> str:
        return f"/medias/{self.sha256}.webp"


#: une image trouvée chez une source tierce (Open Library) n'est jamais une photo du Club
SOURCE_RIGHTS: dict[str, str] = {key: label for key, label in RIGHTS.items() if key != "club"}


def rights_note(
    choice: str, detail: str, *, errors: dict[str, str], field: str = "droit", allowed: dict[str, str] | None = None
) -> str:
    """Le droit d'utilisation déclaré, tel qu'il est conservé. « Autre » exige une précision."""
    detail = " ".join(detail.split())[:300]
    if choice not in (allowed if allowed is not None else RIGHTS):
        errors[field] = _("Indiquez l’origine de l’image et votre droit de l’utiliser.")
        return ""
    if choice == "autre" and not detail:
        errors[f"{field}_detail"] = _("Précisez l’origine de l’image et le droit d’utilisation.")
        return ""
    return RIGHTS[choice] + (f" : {detail}" if detail else "")


def _folder(media_dir: Path) -> Path:
    folder = media_dir / STAGING_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stage(media_dir: Path, data: bytes, *, in_use: Callable[[str], bool] | None = None, **meta: object) -> Staged:
    """Contrôle et range l'image, note ce qui l'accompagne. Refuse (InvalidImage) ce qui n'est pas une image."""
    stored = store_image(data, media_dir)
    token = secrets.token_hex(16)
    staged = Staged(
        token=token,
        sha256=stored.sha256,
        bytes=stored.bytes,
        width=stored.width,
        height=stored.height,
        created=time.time(),
        **meta,  # type: ignore[arg-type]
    )
    target = _folder(media_dir) / f"{token}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(asdict(staged), handle, ensure_ascii=False)
    expire(media_dir, in_use=in_use)
    return staged


def load(media_dir: Path, token: str, user_id: int | None) -> Staged | None:
    """L'aperçu de ce jeton, pour cette personne, s'il n'a pas expiré."""
    if not TOKEN.match(token):
        return None
    path = media_dir / STAGING_DIR / f"{token}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        staged = Staged(**raw)
    except (OSError, ValueError, TypeError):
        return None
    if staged.user_id != user_id or time.time() - staged.created > STAGED_SECONDS:
        return None
    return staged


def _digest_of(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("sha256") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def release(media_dir: Path, digests: set[str], in_use: Callable[[str], bool]) -> int:
    """Supprime les fichiers d'image d'aperçus abandonnés ou expirés, sauf ceux qu'une couverture enregistrée ou un
    autre aperçu en cours désigne encore. Une image externe non confirmée n'est pas conservée (arbitrage du
    12 novembre 2026)."""
    folder = media_dir / STAGING_DIR
    noted = {_digest_of(path) for path in folder.glob("*.json")} if folder.is_dir() else set()
    removed = 0
    for digest in digests:
        if not SHA256.match(digest) or digest in noted or in_use(digest):
            continue
        for name in (f"{digest}.webp", f"{digest}-v.webp"):
            path = media_dir / name
            if path.exists():
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def discard(media_dir: Path, token: str, *, in_use: Callable[[str], bool] | None = None) -> None:
    """Abandon ou enregistrement d'un aperçu : sa note disparaît ; abandonné, son image aussi (`in_use` donné)."""
    if not TOKEN.match(token):
        return
    path = media_dir / STAGING_DIR / f"{token}.json"
    digest = _digest_of(path)
    path.unlink(missing_ok=True)
    if in_use is not None and digest:
        release(media_dir, {digest}, in_use)


def expire(media_dir: Path, *, now: float | None = None, in_use: Callable[[str], bool] | None = None) -> int:
    """Retire les notes d'aperçus expirés ; avec `in_use`, leurs images inutilisées aussi."""
    folder = media_dir / STAGING_DIR
    if not folder.is_dir():
        return 0
    moment = time.time() if now is None else now
    removed = 0
    digests: set[str] = set()
    for path in folder.glob("*.json"):
        try:
            if moment - path.stat().st_mtime > STAGED_SECONDS:
                digests.add(_digest_of(path))
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    if in_use is not None:
        release(media_dir, digests - {""}, in_use)
    return removed


def in_use_checker(database: Database) -> Callable[[str], bool]:
    """Vrai si une image de cette empreinte est enregistrée (`media_files`) : couverture actuelle ou historique."""

    def in_use(digest: str) -> bool:
        with database.session() as session:
            return session.scalar(select(MediaFile.id).where(MediaFile.sha256 == digest).limit(1)) is not None

    return in_use


# --- Historique -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class HistoryEntry:
    id: int
    at: str
    kind: str
    kind_label: str
    source_label: str
    reference: str
    rights_note: str
    user_name: str
    image: str  # vignette de l'image posée, vide pour un retrait ou une image purgée
    previous_image: str
    undone_at: str
    restorable: bool


def history(session: Session, edition_id: int, current_media_id: int | None, *, limit: int = 20) -> list[HistoryEntry]:
    from biblyo.core.models import User

    rows = list(
        session.scalars(
            select(CoverChange).where(CoverChange.edition_id == edition_id).order_by(CoverChange.id.desc()).limit(limit)
        )
    )
    media_ids = {i for r in rows for i in (r.media_id, r.previous_media_id) if i is not None}
    digests = (
        dict(session.execute(select(MediaFile.id, MediaFile.sha256).where(MediaFile.id.in_(media_ids))).all())
        if media_ids
        else {}
    )
    user_ids = {r.user_id for r in rows if r.user_id is not None}
    names = (
        dict(session.execute(select(User.id, User.display_name).where(User.id.in_(user_ids))).all()) if user_ids else {}
    )
    entries: list[HistoryEntry] = []
    for index, row in enumerate(rows):
        restorable = (
            index == 0
            and row.undone_at is None
            and row.kind not in ("restore", "revert", "enrichment")
            and row.media_id == current_media_id
            and not (row.kind == "remove" and row.previous_media_id is None)
        )
        entries.append(
            HistoryEntry(
                id=row.id,
                at=row.created_at,
                kind=row.kind,
                kind_label=KIND_LABELS.get(row.kind, row.kind),
                source_label=SOURCE_LABELS.get(row.source_code or "", row.source_code or ""),
                reference=row.reference or "",
                rights_note=row.rights_note or "",
                user_name=names.get(row.user_id, "") if row.user_id else "",
                image=f"/medias/{digests[row.media_id]}-v.webp" if row.media_id in digests else "",
                previous_image=f"/medias/{digests[row.previous_media_id]}-v.webp"
                if row.previous_media_id in digests
                else "",
                undone_at=row.undone_at or "",
                restorable=restorable,
            )
        )
    return entries


def form_errors(errors: dict[str, str]) -> None:
    if errors:
        raise FormErrors(errors)
