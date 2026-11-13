# SPDX-License-Identifier: EUPL-1.2
"""Fichiers déposés pour une simulation (lot L6, T3 et T4) : rangés à part, jamais dans la base.

Un dépôt vit dans `<données>/imports/.simulations/<jeton>/` : le fichier principal, le fichier complémentaire s'il y
en a un, et `depot.json` (propriétaire, noms, empreintes, feuille, correspondance en cours). Il n'est lisible que par
la personne qui l'a déposé, et disparaît au bout de 24 heures ou dès qu'elle l'abandonne. La simulation se recalcule
à partir de ces fichiers : rien d'autre n'est gardé, et la base n'est jamais écrite.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from biblyo.core.i18n import _
from biblyo.modules.imports import reader
from biblyo.modules.imports.profile import Profile

STAGING_DIR = Path("imports") / ".simulations"
TOKEN = re.compile(r"^[0-9a-f]{32}$")
LIFETIME_S = 24 * 3600
META = "depot.json"
_CACHE_SIZE = 4


@dataclass
class Deposit:
    token: str
    user_id: int | None
    created: float
    filename: str
    sha256: str
    size: int
    complement_filename: str = ""
    complement_sha256: str = ""
    sheet: str = ""
    complement_sheet: str = ""
    profile: str = "{}"  # correspondance en cours, JSON
    profile_name: str = ""
    sheets: list[str] = field(default_factory=list)

    @property
    def expires(self) -> float:
        return self.created + LIFETIME_S


def _folder(data_dir: Path, token: str) -> Path:
    return data_dir / STAGING_DIR / token


def create(
    data_dir: Path,
    main: tuple[bytes, reader.Workbook],
    complement: tuple[bytes, reader.Workbook] | None,
    *,
    user_id: int | None,
    profile: Profile,
    now: float | None = None,
) -> Deposit:
    expire(data_dir, now=now)
    token = secrets.token_hex(16)
    folder = _folder(data_dir, token)
    folder.mkdir(parents=True, mode=0o700)
    data, workbook = main
    (folder / "principal").write_bytes(data)
    deposit = Deposit(
        token,
        user_id,
        now if now is not None else time.time(),
        workbook.filename,
        workbook.sha256,
        workbook.size,
        sheet=data_sheet(workbook),
        sheets=[s.name for s in workbook.sheets],
        profile=profile.to_json(),
        profile_name=profile.name,
    )
    if complement is not None:
        other_data, other = complement
        (folder / "complementaire").write_bytes(other_data)
        deposit.complement_filename = other.filename
        deposit.complement_sha256 = other.sha256
        deposit.complement_sheet = data_sheet(other)
    save(data_dir, deposit)
    return deposit


def data_sheet(workbook: reader.Workbook) -> str:
    """La feuille de données : la première qui a des lignes et un en-tête, sinon la première."""
    for sheet in workbook.sheets:
        if sheet.rows and sheet.columns and not sheet.hidden:
            return sheet.name
    return workbook.sheets[0].name if workbook.sheets else ""


def save(data_dir: Path, deposit: Deposit) -> None:
    path = _folder(data_dir, deposit.token) / META
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(deposit), ensure_ascii=False))
    temporary.replace(path)


def load(data_dir: Path, token: str, user_id: int | None, *, now: float | None = None) -> Deposit | None:
    """Le dépôt, s'il existe, n'a pas expiré et appartient bien à cette personne."""
    if not TOKEN.match(token):
        return None
    path = _folder(data_dir, token) / META
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        deposit = Deposit(**data)
    except (OSError, ValueError, TypeError):
        return None
    if deposit.user_id != user_id or deposit.expires < (now if now is not None else time.time()):
        return None
    return deposit


def mine(data_dir: Path, user_id: int | None, *, now: float | None = None) -> list[Deposit]:
    folder = data_dir / STAGING_DIR
    if not folder.is_dir():
        return []
    found = [d for p in folder.iterdir() if (d := load(data_dir, p.name, user_id, now=now)) is not None]
    return sorted(found, key=lambda d: -d.created)


def workbooks(data_dir: Path, deposit: Deposit) -> tuple[reader.Workbook, reader.Workbook | None]:
    """Relit les fichiers déposés, avec les mêmes contrôles qu'au dépôt ; l'empreinte doit être la même."""
    folder = _folder(data_dir, deposit.token)
    main = _cached(folder / "principal", deposit.filename)
    if main.sha256 != deposit.sha256:
        raise reader.ReadError(_("Le fichier déposé a changé : déposez-le de nouveau."))
    other = None
    if deposit.complement_sha256:
        other = _cached(folder / "complementaire", deposit.complement_filename)
        if other.sha256 != deposit.complement_sha256:
            raise reader.ReadError(_("Le fichier complémentaire a changé : déposez-le de nouveau."))
    return main, other


_READ: OrderedDict[tuple[str, str], reader.Workbook] = OrderedDict()


def _cached(path: Path, filename: str) -> reader.Workbook:
    data = path.read_bytes()
    key = (hashlib.sha256(data).hexdigest(), filename)
    if key in _READ:
        _READ.move_to_end(key)
        return _READ[key]
    workbook = reader.read(data, filename)
    _READ[key] = workbook
    while len(_READ) > _CACHE_SIZE:
        _READ.popitem(last=False)
    return workbook


def discard(data_dir: Path, token: str) -> None:
    if TOKEN.match(token):
        shutil.rmtree(_folder(data_dir, token), ignore_errors=True)


def expire(data_dir: Path, *, now: float | None = None) -> int:
    """Retire les dépôts de plus de 24 heures ; rend leur nombre."""
    folder = data_dir / STAGING_DIR
    if not folder.is_dir():
        return 0
    limit = (now if now is not None else time.time()) - LIFETIME_S
    removed = 0
    for path in folder.iterdir():
        meta = path / META
        try:
            created = float(json.loads(meta.read_text()).get("created", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            created = path.stat().st_mtime
        if created < limit:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
