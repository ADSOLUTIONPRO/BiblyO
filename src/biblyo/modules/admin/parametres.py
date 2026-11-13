# SPDX-License-Identifier: EUPL-1.2
"""Paramètres de l'instance (lot L6, finalisation avant production) : un tableau de bord **opérationnel**.

Ce qui se règle ici : l'identité de la bibliothèque (nom, coordonnées, logo), les sauvegardes (créer, vérifier). Ce
qui se règle ailleurs est **contrôlé** ici, avec son état réel et le lien qui y mène : comptes et rôles, règles de prêt,
catégories de lecteurs, sources (BnF, Sudoc, Open Library et son adresse de contact), droits sur les notices et les
couvertures, emplacements, mise en rayon, profils d'import, exports, informations techniques. Rien n'est seulement
affiché : chaque ligne lit la base ou le disque au moment où la page s'ouvre.

Le logo est réencodé (format, taille et dimensions vérifiés, métadonnées retirées) et gardé **dans la base** : il
part avec chaque sauvegarde, et le nettoyage des médias, qui supprime les images qu'aucune édition n'utilise, ne peut
pas l'effacer.
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from biblyo import __version__
from biblyo.config import Settings
from biblyo.core import audit
from biblyo.core.backup import list_backups
from biblyo.core.db import Database
from biblyo.core.errors import FormErrors
from biblyo.core.i18n import _
from biblyo.core.media import preview_webp
from biblyo.core.schema_state import read_revision
from biblyo.core.settings_store import delete_setting, get_setting, set_setting

NAME, EMAIL, PHONE, ADDRESS = "library.name", "library.contact_email", "library.phone", "library.address"
LOGO, LOGO_SHA = "library.logo", "library.logo_sha"
LOGO_SIDE = 512
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    phone: str
    address: str
    logo_sha: str


def identity(session: Session) -> Identity:
    return Identity(
        str(get_setting(session, NAME, "") or ""),
        str(get_setting(session, EMAIL, "") or ""),
        str(get_setting(session, PHONE, "") or ""),
        str(get_setting(session, ADDRESS, "") or ""),
        str(get_setting(session, LOGO_SHA, "") or ""),
    )


def save_identity(database: Database, values: dict[str, str], user_id: int | None) -> None:
    name = " ".join(values.get("nom", "").split())
    email = values.get("courriel", "").strip()
    phone = " ".join(values.get("telephone", "").split())
    address = values.get("adresse", "").strip()
    errors: dict[str, str] = {}
    if not 2 <= len(name) <= 120:
        errors["nom"] = _("Le nom de la bibliothèque compte de 2 à 120 caractères.")
    if email and (len(email) > 254 or not _EMAIL.match(email)):
        errors["courriel"] = _("Adresse de courriel invalide.")
    if len(phone) > 40:
        errors["telephone"] = _("Le numéro compte 40 caractères au plus.")
    if len(address) > 300:
        errors["adresse"] = _("L’adresse compte 300 caractères au plus.")
    if errors:
        raise FormErrors(errors)
    with database.session(write=True) as session:
        changed = []
        for key, value in ((NAME, name), (EMAIL, email), (PHONE, phone), (ADDRESS, address)):
            if (get_setting(session, key, "") or "") != value:
                changed.append(key.split(".", 1)[1])
                set_setting(session, key, value, user_id)
        if changed:
            audit.record(session, "settings.identity", "settings", None, user_id=user_id, details={"champs": changed})


def save_logo(database: Database, data: bytes, user_id: int | None) -> str:
    """Contrôle et réencode l'image (WebP, 512 px au plus), puis la garde dans la base. Rend son empreinte."""
    webp = preview_webp(data, LOGO_SIDE)
    digest = hashlib.sha256(webp).hexdigest()
    with database.session(write=True) as session:
        set_setting(session, LOGO, base64.b64encode(webp).decode("ascii"), user_id)
        set_setting(session, LOGO_SHA, digest, user_id)
        audit.record(session, "settings.logo", "settings", None, user_id=user_id, details={"empreinte": digest[:16]})
    return digest


def remove_logo(database: Database, user_id: int | None) -> None:
    with database.session(write=True) as session:
        delete_setting(session, LOGO)
        delete_setting(session, LOGO_SHA)
        audit.record(session, "settings.logo_removed", "settings", None, user_id=user_id)


def logo_bytes(session: Session) -> tuple[bytes, str] | None:
    encoded = get_setting(session, LOGO, None)
    if not encoded:
        return None
    return base64.b64decode(str(encoded)), str(get_setting(session, LOGO_SHA, ""))


# --- Contrôle des paramètres -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    label: str
    state: str  # ok, todo, info
    detail: str
    href: str = ""


@dataclass
class Overview:
    checks: list[Check] = field(default_factory=list)
    roles: list[tuple[str, list[bool]]] = field(default_factory=list)
    rights: list[tuple[str, str]] = field(default_factory=list)
    backups: list[dict[str, Any]] = field(default_factory=list)
    technical: list[tuple[str, str]] = field(default_factory=list)


#: Droits que l'exploitation doit connaître, dans l'ordre du tableau des rôles.
KEY_RIGHTS: tuple[tuple[str, str], ...] = (
    ("catalogue.edit", _("Modifier les notices")),
    ("enrichment.review", _("Accepter métadonnées et couvertures")),
    ("holdings.edit", _("Ranger, modifier les exemplaires")),
    ("holdings.withdraw", _("Retirer du fonds")),
    ("locations.manage", _("Gérer les emplacements")),
    ("circulation.lend", _("Prêter et rendre")),
    ("circulation.settings", _("Régler les prêts")),
    ("patrons.view_personal", _("Voir les coordonnées des lecteurs")),
    ("imports.simulate", _("Simuler un import, exporter")),
    ("imports.apply", _("Appliquer un import")),
    ("admin.users", _("Gérer les comptes")),
    ("admin.settings", _("Régler l’instance")),
    ("admin.backups", _("Sauvegarder")),
)
ROLE_LABELS = {
    "admin": _("Administration"),
    "librarian": _("Bibliothécaire"),
    "volunteer": _("Bénévole"),
    "viewer": _("Consultation"),
}


def _count(session: Session, sql: str) -> int:
    return int(session.execute(text(sql)).scalar() or 0)


def overview(session: Session, settings: Settings) -> Overview:
    from biblyo.modules.enrichment.sources import contact_of
    from biblyo.modules.imports.apply import enabled as import_enabled
    from biblyo.modules.patrons.rules import CATEGORIES

    result = Overview()
    me = identity(session)
    checks = result.checks
    checks.append(Check(_("Nom de la bibliothèque"), "ok" if me.name else "todo", me.name or _("À renseigner")))
    checks.append(
        Check(
            _("Logo et identité visuelle"),
            "ok" if me.logo_sha else "info",
            _("Logo enregistré") if me.logo_sha else _("Aucun logo : les initiales du nom sont affichées"),
        )
    )
    checks.append(
        Check(
            _("Coordonnées de la bibliothèque"),
            "ok" if me.email else "todo",
            ", ".join(v for v in (me.email, me.phone) if v) or _("Courriel à renseigner"),
        )
    )
    users = dict(
        session.execute(
            text(
                "SELECT r.code, COUNT(u.id) FROM roles r LEFT JOIN user_roles ur ON ur.role_id = r.id"
                " LEFT JOIN users u ON u.id = ur.user_id AND u.is_active = 1 GROUP BY r.code"
            )
        ).all()
    )
    checks.append(
        Check(
            _("Comptes actifs"),
            "ok" if users.get("admin") else "todo",
            ", ".join(f"{ROLE_LABELS.get(code, code)} : {count}" for code, count in sorted(users.items())),
            "/admin/utilisateurs",
        )
    )
    rules = _count(session, "SELECT COUNT(*) FROM loan_rules")
    checks.append(
        Check(
            _("Règles de prêt, renouvellement et retard"),
            "ok" if rules else "todo",
            _("{n} règle(s) : durée, renouvellements, plafond par catégorie ; blocage pour retard réglable").format(
                n=rules
            ),
            "/prets/regles",
        )
    )
    checks.append(
        Check(
            _("Catégories de lecteurs"),
            "info",
            ", ".join(str(label) for label in CATEGORIES.values()) + _(" ; foyers et structures"),
            "/lecteurs",
        )
    )
    sources = dict(session.execute(text("SELECT code, is_enabled FROM metadata_sources")).all())
    contact = contact_of(session)
    source_text = ", ".join(
        f"{label} : {_('active') if sources.get(code) and (code != 'openlibrary' or contact) else _('inactive')}"
        for code, label in (("bnf", "BnF"), ("sudoc", "Sudoc"), ("openlibrary", "Open Library"))
    )
    checks.append(
        Check(
            _("Sources bibliographiques"),
            "ok" if sources.get("bnf") else "todo",
            source_text + ("" if contact else _(" ; adresse de contact non renseignée : Open Library reste inactive")),
            "/admin/sources",
        )
    )
    places = dict(session.execute(text("SELECT kind, COUNT(*) FROM locations WHERE is_active = 1 GROUP BY kind")).all())
    shelves = places.get("shelf", 0) + places.get("bin", 0)
    checks.append(
        Check(
            _("Sites, salles, meubles et étagères"),
            "ok" if shelves else "todo",
            _("{r} salle(s), {f} meuble(s), {s} étagère(s) ou bac(s)").format(
                r=places.get("room", 0), f=places.get("furniture", 0), s=shelves
            ),
            "/emplacements",
        )
    )
    waiting = _count(session, "SELECT COUNT(*) FROM copies WHERE status = 'in_processing'")
    checks.append(
        Check(
            _("Mise en rayon"),
            "info",
            _("« En place » seulement après un rangement vérifié (scan sur l’étagère, case cochée, ou présence "
              "confirmée) ; {n} exemplaire(s) en préparation").format(n=waiting),
            "/emplacements/non-ranges?etat=en_preparation",
        )
    )  # fmt: skip
    profiles = _count(session, "SELECT COUNT(*) FROM import_profiles")
    checks.append(
        Check(
            _("Profils d’import"),
            "info",
            _("{n} profil(s) enregistré(s) ; application des imports : {s}").format(
                n=profiles, s=_("activée") if import_enabled(session) else _("désactivée (biblyo import application)")
            ),
            "/imports",
        )
    )
    checks.append(
        Check(
            _("Exports"),
            "ok",
            _("Catalogue complet en .xlsx ou en .csv ; format d’origine de chaque lot importé"),
            "/imports",
        )
    )
    archives = list_backups(settings)
    checks.append(
        Check(
            _("Sauvegardes et restauration"),
            "ok" if archives else "todo",
            _("{n} sauvegarde(s) dans {d} ; restauration : biblyo backup restore, service arrêté").format(
                n=len(archives), d=settings.backup_dir
            ),
        )
    )
    granted: dict[str, set[str]] = {}
    for code, permission in session.execute(
        text("SELECT r.code, rp.permission FROM roles r JOIN role_permissions rp ON rp.role_id = r.id")
    ):
        granted.setdefault(code, set()).add(permission)
    result.rights = list(KEY_RIGHTS)
    for code in ("admin", "librarian", "volunteer", "viewer"):
        perms = granted.get(code, set())
        result.roles.append(
            (str(ROLE_LABELS[code]), ["*" in perms or permission in perms for permission, _label in KEY_RIGHTS])
        )
    for path in archives[:10]:
        stat = path.stat()
        result.backups.append({"name": path.name, "bytes": stat.st_size, "at": int(stat.st_mtime)})
    revision, _state = read_revision(settings.database_path)
    database_bytes = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    result.technical = [
        (_("Version"), __version__),
        (_("Commit de l’image"), os.environ.get("BIBLYO_COMMIT", "") or _("non renseigné (hors conteneur)")),
        (_("Révision du schéma"), str(revision or "—")),
        (_("Environnement"), settings.env),
        (_("Architecture"), platform.machine()),
        (_("Python"), sys.version.split()[0]),
        (_("Dossier des données"), str(settings.data_dir)),
        (_("Dossier des sauvegardes"), str(settings.backup_dir)),
        (_("Taille de la base"), _("{n} Mio").format(n=round(database_bytes / 1048576, 1))),
        (_("Adresse publique"), settings.base_url or "—"),
    ]
    return result


def backup_path(settings: Settings, name: str) -> Path | None:
    """Une archive de la liste, désignée par son nom seul : aucun chemin saisi n'est suivi."""
    return next((path for path in list_backups(settings) if path.name == name), None)
