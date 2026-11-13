# SPDX-License-Identifier: EUPL-1.2
"""Message affiché après une redirection. Le cookie ne porte qu'un code : aucun texte ne peut y être injecté."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from biblyo.config import Settings
from biblyo.core.i18n import _

COOKIE_NAME = "biblyo_flash"

MESSAGES: dict[str, tuple[str, str]] = {
    "profile_updated": ("ok", _("Votre profil a été mis à jour.")),
    "password_changed": ("ok", _("Votre mot de passe a été changé. Vos autres sessions ont été fermées.")),
    "session_revoked": ("ok", _("La session a été fermée.")),
    "user_updated": ("ok", _("Le compte a été mis à jour.")),
    "installed": ("ok", _("Installation terminée. Bienvenue dans biblyO.")),
    "notice_created": ("ok", _("La notice a été créée.")),
    "notice_updated": ("ok", _("La notice a été mise à jour.")),
    "notice_archived": ("ok", _("La notice a été archivée.")),
    "notice_restored": ("ok", _("La notice a été réactivée.")),
    "notice_deleted": ("ok", _("La notice a été supprimée.")),
    "edition_added": ("ok", _("L’édition a été ajoutée.")),
    "edition_primary": ("ok", _("C’est désormais l’édition principale.")),
    "edition_archived": ("ok", _("L’édition a été archivée.")),
    "edition_restored": ("ok", _("L’édition a été réactivée.")),
    "edition_deleted": ("ok", _("L’édition a été supprimée.")),
    "copies_added": ("ok", _("Les exemplaires ont été ajoutés.")),
    "copy_deleted": ("ok", _("L’exemplaire a été supprimé. Ses codes ne seront jamais réattribués.")),
    "place_created": ("ok", _("L’emplacement a été créé.")),
    "import_deposited": ("ok", _("Fichier lu. Vérifiez la correspondance des colonnes, puis lancez la simulation.")),
    "import_mapping_saved": ("ok", _("La correspondance a été enregistrée pour ce dépôt.")),
    "import_profile_saved": ("ok", _("Le profil de correspondance a été enregistré.")),
    "import_profile_deleted": ("ok", _("Le profil de correspondance a été supprimé.")),
    "import_discarded": ("ok", _("Le dépôt a été abandonné : ses fichiers sont supprimés.")),
    "import_applied": ("ok", _("Import appliqué : tout le plan est écrit, en une transaction, après sauvegarde.")),
    "place_updated": ("ok", _("L’emplacement a été mis à jour.")),
    "place_deleted": ("ok", _("L’emplacement a été supprimé.")),
    "shelf_sorted": ("ok", _("L’étagère a été triée.")),
    "group_moved": ("ok", _("Les exemplaires ont été rangés. Leurs cotes ne changent pas.")),
    "settings_identity_saved": ("ok", _("L’identité de la bibliothèque a été enregistrée.")),
    "settings_logo_saved": ("ok", _("Le logo a été enregistré.")),
    "settings_logo_removed": ("ok", _("Le logo a été retiré : les initiales du nom sont affichées.")),
    "settings_backup_created": ("ok", _("Sauvegarde créée et vérifiée.")),
    "presence_confirmed": ("ok", _("Présence vérifiée : ces exemplaires sont « En place ».")),
    "barcodes_assigned": ("ok", _("Codes-barres attribués. Aucun inventaire n’a changé, aucune étiquette imprimée.")),
    "group_moved_over": ("late", _("Exemplaires rangés, mais cet emplacement dépasse maintenant sa capacité.")),
    "scan_ok": ("ok", _("Exemplaire rangé à la fin de l’étagère.")),
    "scan_over": ("late", _("Exemplaire rangé, mais cet emplacement dépasse maintenant sa capacité.")),
    "scan_same": ("ok", _("Déjà sur cette étagère : rien n’a bougé.")),
    "scan_undone": ("ok", _("Le rangement a été annulé.")),
    "copy_updated": ("ok", _("La fiche de l’exemplaire a été mise à jour.")),
    "loan_cancelled": ("ok", _("L’opération est annulée. Sa trace reste dans l’historique du prêt.")),
    "loan_corrected": ("ok", _("La correction est enregistrée. La trace de l’erreur reste dans l’historique.")),
    "loan_due_changed": ("ok", _("L’échéance a été modifiée.")),
    "loan_returned": ("ok", _("Le retour est enregistré. L’historique du prêt est conservé.")),
    "loan_renewed": ("ok", _("Le prêt est renouvelé : la nouvelle échéance est affichée.")),
    "reminder_noted": ("ok", _("Le rappel est noté. biblyO n’a rien envoyé.")),
    "loan_rules_saved": ("ok", _("Les règles de prêt sont enregistrées.")),
    "copy_status": ("ok", _("Le statut de l’exemplaire a été mis à jour.")),
    "copy_moved": ("ok", _("L’exemplaire a été rangé. Sa cote ne change pas.")),
    "copy_moved_over": ("late", _("L’exemplaire a été rangé, mais cet emplacement dépasse maintenant sa capacité.")),
    "copy_unchanged": ("ok", _("Rien n’a changé : l’exemplaire est déjà à cette place.")),
    "enrichment_applied": (
        "ok",
        _("La fiche a été complétée. La provenance de chaque champ est affichée dans l’onglet Enrichissement."),
    ),
    "enrichment_partial": ("ok", _("Les champs cochés ont été appliqués ; les autres sont restés tels quels.")),
    "enrichment_nothing": ("late", _("Aucun champ n’a été appliqué.")),
    "enrichment_stale": (
        "late",
        _("La fiche a changé depuis la recherche : la proposition est périmée. Relancez la recherche."),
    ),
    "enrichment_done": ("ok", _("Cette proposition a déjà été traitée.")),
    "enrichment_limited": ("late", _("Trop de recherches en une minute : patientez un instant.")),
    "enrichment_cover_failed": (
        "late",
        _("Les autres champs cochés ont été appliqués ; la couverture n’a pas pu être récupérée. Voir l’historique."),
    ),
    "enrichment_reverted": ("ok", _("L’enrichissement est annulé : les valeurs d’avant sont rétablies.")),
    "enrichment_reverted_partial": (
        "late",
        _("Annulation partielle : les champs modifiés depuis et non cochés gardent leur valeur actuelle."),
    ),
    "isbn_confirmed": ("ok", _("L’ISBN est confirmé : il entre dans la recherche et le contrôle des doublons.")),
    "isbn_rejected": ("ok", _("L’ISBN est rejeté : il ne sera plus proposé pour cette édition.")),
    "sources_saved": ("ok", _("Les réglages des sources sont enregistrés.")),
    "sources_cache_cleared": ("ok", _("Le cache de cette source est vidé.")),
    "enrichment_empty": ("late", _("Indiquez au moins un ISBN, un titre, un auteur ou une série.")),
    "enrichment_failed": ("late", _("La recherche n’a pas pu être lancée : cette édition n’existe plus.")),
    "group_unchanged": ("ok", _("Rien n’a changé : ces exemplaires sont déjà à cette place.")),
    "copy_unshelved": ("ok", _("L’exemplaire n’a plus de place : il figure dans les « Non rangés ».")),
    "cover_saved": ("ok", _("La couverture a été enregistrée.")),
    "cover_removed": ("ok", _("La couverture a été retirée.")),
    "cover_restored": ("ok", _("La couverture précédente est rétablie.")),
    "cover_discarded": ("ok", _("Aperçu abandonné : la couverture n’a pas changé.")),
    "series_created": ("ok", _("La série a été créée.")),
    "series_updated": ("ok", _("La série a été mise à jour.")),
    "series_deleted": ("ok", _("La série a été supprimée.")),
    "volumes_confirmed": ("ok", _("Les tomes ont été confirmés.")),
    "volume_unconfirmed": ("ok", _("Le tome n’est plus indiqué comme confirmé.")),
    "search_saved": ("ok", _("La recherche a été enregistrée.")),
    "search_deleted": ("ok", _("La recherche a été retirée.")),
    "patron_registered": ("ok", _("Le lecteur a été inscrit.")),
    "patron_updated": ("ok", _("La fiche du lecteur a été mise à jour.")),
    "patron_unchanged": ("ok", _("Rien n’a changé sur cette fiche.")),
    "patron_contacts": ("ok", _("Les coordonnées ont été mises à jour.")),
    "patron_renewed": ("ok", _("L’adhésion a été renouvelée.")),
    "patron_majority": (
        "ok",
        _("La majorité est confirmée. Les liens de responsabilité restent à revoir sur la fiche."),
    ),
    "card_replaced": ("ok", _("La nouvelle carte est en service. L’ancien numéro ne sera jamais redonné.")),
    "card_ended": ("ok", _("La carte n’est plus en service. Son numéro ne sera jamais redonné.")),
    "family_registered": ("ok", _("La famille a été inscrite.")),
    "structure_created": ("ok", _("La structure a été créée. Ajoutez-lui ses référents.")),
    "household_updated": ("ok", _("Le foyer a été mis à jour.")),
    "household_contacts": ("ok", _("Les coordonnées partagées ont été mises à jour.")),
    "household_archived": ("ok", _("Le foyer a été archivé.")),
    "household_restored": ("ok", _("Le foyer a été réactivé.")),
    "member_added": ("ok", _("Le lecteur a été rattaché au foyer. Aucun droit n’en découle.")),
    "member_updated": ("ok", _("Le rôle dans le foyer a été mis à jour.")),
    "member_removed": ("ok", _("Le lien avec le foyer a été retiré. La fiche du lecteur reste intacte.")),
    "guardian_added": ("ok", _("Le responsable a été ajouté, avec les droits cochés.")),
    "guardian_updated": ("ok", _("Les droits du responsable ont été mis à jour.")),
    "guardian_removed": ("ok", _("Le lien de responsabilité a été retiré.")),
    "patron_suspended": ("ok", _("Le lecteur est suspendu.")),
    "patron_archived": ("ok", _("Le lecteur est archivé. Sa fiche et ses liens sont conservés.")),
    "patron_reactivated": ("ok", _("Le lecteur est de nouveau actif.")),
    "hold_placed": (
        "ok",
        _("La mise en conservation est enregistrée : l’anonymisation est bloquée tant qu’elle court."),
    ),
    "hold_lifted": ("ok", _("La mise en conservation est levée.")),
    "patron_anonymized": (
        "ok",
        _("Lecteur anonymisé. Les sauvegardes antérieures contiennent encore ces données jusqu’à leur suppression."),
    ),
    "taxonomy_updated": ("ok", _("La liste a été mise à jour.")),
    "taxonomy_merged": ("ok", _("La fusion a été effectuée. Aucune association n’a été perdue.")),
}


def set_flash(response: Response, code: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME, code, max_age=60, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
    )


def read_flash(request: Request) -> tuple[str, str] | None:
    return MESSAGES.get(request.cookies.get(COOKIE_NAME, ""))


def clear_if_read(request: Request, response: Response, settings: Settings) -> None:
    if COOKIE_NAME in request.cookies:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
