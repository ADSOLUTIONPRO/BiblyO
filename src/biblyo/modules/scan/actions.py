# SPDX-License-Identifier: EUPL-1.2
"""Ce qu'on peut faire d'un livre scanné : seulement les actions permises au compte **et** utiles dans l'état réel de
l'exemplaire. Chaque action mène à l'écran existant qui l'accomplit, avec ses contrôles, ses transactions et son
journal : le scanner ne contourne rien, il ouvre la bonne porte."""

from __future__ import annotations

from dataclasses import dataclass

from biblyo.core.i18n import _
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.scan.resolver import CopyView

MODES: dict[str, str] = {
    "recherche": _("Recherche"),
    "comptoir": _("Comptoir de prêt"),
    "retours": _("Retours"),
    "inventaire": _("Inventaire"),
    "rangement": _("Rangement"),
    "enrichissement": _("Enrichissement"),
}
MODE_PATHS: dict[str, str] = {
    "recherche": "/scanner",
    "comptoir": "/comptoir",
    "retours": "/comptoir/retours",
    "inventaire": "/scanner/inventaire",
    "rangement": "/emplacements/scanner",
    "enrichissement": "/scanner?mode=enrichissement",
}
MODE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "recherche": ("catalogue.view",),
    "comptoir": ("circulation.lend", "patrons.view"),
    "retours": ("circulation.lend",),
    "inventaire": ("catalogue.view",),
    "rangement": ("holdings.edit",),
    "enrichissement": ("enrichment.search",),
}
#: L'action mise en avant par chaque mode, quand elle est permise et possible.
PRIMARY: dict[str, str] = {
    "comptoir": "lend",
    "retours": "return",
    "inventaire": "presence",
    "rangement": "move",
    "enrichissement": "enrich",
}


@dataclass(frozen=True)
class Action:
    code: str
    label: str
    href: str
    icon: str
    primary: bool = False


def modes_for(auth: AuthContext) -> list[tuple[str, str, str]]:
    """Les modes de travail ouverts à ce compte : code, libellé, adresse."""
    return [
        (code, label, MODE_PATHS[code])
        for code, label in MODES.items()
        if all(auth.can(permission) for permission in MODE_PERMISSIONS[code])
    ]


def copy_actions(view: CopyView, auth: AuthContext, mode: str = "recherche") -> list[Action]:
    actions: list[Action] = []
    on_loan = view.loan_id is not None
    shelvable = view.status not in ("lost", "withdrawn", "missing")
    if auth.can("circulation.lend") and auth.can("patrons.view") and view.state and view.state.is_loanable_now:
        actions.append(Action("lend", _("Prêter"), f"/comptoir?exemplaire={view.id}&livre={view.work_id}", "out"))
    if on_loan and auth.can("circulation.lend"):
        target = f"/comptoir/livre/{view.work_id}#retour-{view.id}" if auth.can("patrons.view") else "/comptoir/retours"
        actions.append(Action("return", _("Enregistrer le retour"), target, "ret"))
    actions.append(Action("open", _("Ouvrir la fiche"), f"/catalogue/notices/{view.work_id}", "book"))
    if auth.can("holdings.edit"):
        actions.append(Action("edit", _("Modifier l’exemplaire"), f"/exemplaires/{view.id}#titre-fiche", "edit"))
    else:
        actions.append(Action("copy", _("Fiche de l’exemplaire"), f"/exemplaires/{view.id}", "copy"))
    if auth.can("enrichment.search"):
        actions.append(
            Action("enrich", _("Enrichir les informations"), f"/catalogue/notices/{view.work_id}/enrichir", "enrich")
        )
    if auth.can("holdings.edit") and not on_loan:
        if view.status == "in_processing" and view.location_id is not None:
            actions.append(
                Action("presence", _("Vérifier sa présence"), f"/emplacements/verifier?exemplaire={view.id}", "check")
            )
        actions.append(Action("status", _("Changer le statut"), f"/exemplaires/{view.id}#titre-statut", "gear"))
    if auth.can("holdings.edit") and shelvable:
        label = _("Ranger") if view.location_id is None else _("Déplacer")
        actions.append(Action("move", label, f"/exemplaires/{view.id}/deplacer", "shelf"))
    if view.barcode and auth.can("labels.print"):
        actions.append(
            Action(
                "label", _("Imprimer son étiquette"), f"/etiquettes/imprimer?exemplaire={view.id}&origine=copy", "print"
            )
        )
    elif not view.barcode and auth.can("holdings.edit"):
        actions.append(Action("label", _("Générer son code-barres"), f"/exemplaires/{view.id}#titre-code", "tag"))
    wanted = PRIMARY.get(mode) or ("lend" if any(a.code == "lend" for a in actions) else "return")
    chosen = next((action.code for action in actions if action.code == wanted), "open")
    return [Action(a.code, a.label, a.href, a.icon, a.code == chosen) for a in actions]
