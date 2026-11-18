# SPDX-License-Identifier: EUPL-1.2
"""Assistant « Configurer ma douchette » : ce que la douchette a réellement envoyé, et ce qu'il faut régler.

Une douchette USB ou Bluetooth se comporte comme un clavier (HID) : aucun pilote, aucune permission du navigateur. Ce
qu'on peut mesurer depuis la page : le texte reçu, la touche finale (Entrée, Tab ou rien) et la vitesse de frappe. Les
réglages se font avec les codes de configuration du manuel de **votre** modèle : deux douchettes de la même marque ne
se règlent pas forcément pareil, rien ici ne le suppose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from biblyo.core.barcodes import ean13_check_digit
from biblyo.core.i18n import _
from biblyo.core.identifiers import clean, from_qwerty
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.labels.layout import TEST_CODE128, TEST_EAN13, TEST_UNKNOWN
from biblyo.modules.scan import resolver

READY = "pret"
NO_SUFFIX = "suffixe"
UNREADABLE = "non_reconnu"
UNKNOWN = "inconnu"
KNOWN = "reconnu"
TITLES: dict[str, str] = {
    READY: _("Douchette prête"),
    NO_SUFFIX: _("Suffixe non détecté"),
    UNREADABLE: _("Code non reconnu"),
    UNKNOWN: _("Code inconnu"),
    KNOWN: _("Code reconnu"),
}
BADGES = {READY: "ok", KNOWN: "ok", NO_SUFFIX: "due", UNREADABLE: "late", UNKNOWN: "due"}
SUFFIXES = {"enter": _("Entrée"), "tab": _("Tabulation")}
SLOW_TYPING_MS = 80  # au-delà, entre deux caractères, c'est une main sur un clavier, pas une douchette


#: La liste de mise en service d'une douchette (arbitrage du 18 novembre 2026), dans l'ordre où on la parcourt.
CHECKS: dict[str, str] = {
    "code128": _("Code 128 d’essai (« BIBLYO-TEST ») lu sans erreur"),
    "isbn": _("ISBN-13 lu sans erreur (le code d’essai, ou le dos d’un livre)"),
    "interne": _("Étiquette d’un livre (code interne) : la bonne fiche s’ouvre"),
    "inventaire": _("Numéro d’inventaire : la bonne fiche s’ouvre (lu ou tapé)"),
    "inconnu": _("Code inconnu (« BIBLYO-INCONNU ») : biblyO le dit, sans rien ouvrir"),
    "serie": _("Trois lectures réussies de suite, sans attendre entre elles"),
    "suffixe": _("Touche finale Entrée ou Tab reçue après chaque code"),
    "clavier": _("Clavier : les chiffres et le tiret arrivent tels quels (AZERTY bien réglé)"),
}
SERIES_TARGET = 3
SERIES_WINDOW = 20  # secondes au plus entre deux lectures d'une même série


@dataclass
class Diagnosis:
    status: str
    received: str
    details: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    identification: resolver.Identification | None = None
    passed: frozenset[str] = frozenset()  # points de la liste que cette lecture établit
    layout_problem: bool = False

    @property
    def title(self) -> str:
        return TITLES[self.status]

    @property
    def badge(self) -> str:
        return BADGES[self.status]


def _suffix_line(suffix: str) -> str:
    if suffix in SUFFIXES:
        return _("Touche finale reçue : {key}. biblyO valide le code tout seul.").format(key=SUFFIXES[suffix])
    if suffix == "none":
        return _("Aucune touche finale reçue après le code.")
    return _("Touche finale non mesurée (JavaScript désactivé).")


NO_SUFFIX_FIX = _(
    "Dans le manuel de la douchette, scannez le code de configuration « Suffixe : Entrée » (parfois « Add CR » ou "
    "« Enter »). « Tabulation » convient aussi."
)
QWERTY_FIX = _(
    "Dans le manuel de la douchette, scannez le code « Clavier français » ou « AZERTY » (parfois « Keyboard "
    "language : French »). Une douchette Bluetooth peut aussi demander le même réglage côté ordinateur."
)


def diagnose(session: Session, raw: str, suffix: str, interval_ms: int, auth: AuthContext) -> Diagnosis:
    received = raw.replace("\r", "").replace("\n", "").replace("\t", "")
    text = clean(raw)
    found = Diagnosis(UNREADABLE, received)
    if not text:
        found.details.append(_("Rien n’a été reçu. Le champ avait-il le curseur au moment du scan ?"))
        return found
    tests = {TEST_CODE128: _("Code 128"), TEST_EAN13: _("EAN-13")}
    slow = interval_ms > SLOW_TYPING_MS
    if text in tests:
        found.status = NO_SUFFIX if suffix == "none" else READY
        found.passed = frozenset({"code128" if text == TEST_CODE128 else "isbn"})
        found.details.append(_("{kind} lu sans erreur : « {text} ».").format(kind=tests[text], text=text))
        found.details.append(_suffix_line(suffix))
        if suffix == "none":
            found.fixes.append(NO_SUFFIX_FIX)
        if any(character.islower() for character in received):
            found.details.append(
                _(
                    "Les lettres arrivent en minuscules : le verrouillage des majuscules est peut-être "
                    "actif. biblyO les accepte quand même."
                )
            )
        if slow:
            found.details.append(_("Les caractères sont arrivés lentement : était-ce une saisie au clavier ?"))
        return found
    repaired = from_qwerty(raw)
    if repaired in tests:
        found.details.append(
            _(
                "La douchette a écrit « {got} » au lieu de « {want} » : elle est réglée en clavier américain (QWERTY), "
                "alors que l’ordinateur est en français (AZERTY)."
            ).format(got=received, want=repaired)
        )
        found.fixes.append(QWERTY_FIX)
        found.layout_problem = True
        return found
    digits = text.replace("-", "")
    if digits.isdigit() and len(digits) == 12 and digits + ean13_check_digit(digits) == TEST_EAN13:
        found.details.append(_("Le dernier chiffre (la clé de contrôle) de l’EAN-13 n’a pas été transmis."))
        found.fixes.append(_("Dans le manuel, activez « Transmettre le chiffre de contrôle » pour l’EAN-13."))
        return found
    for test in tests:
        if test in text and text != test:
            found.details.append(
                _(
                    "Le code est entouré d’autres caractères : « {got} ». La douchette ajoute un préfixe ou un "
                    "identifiant de symbologie."
                ).format(got=received)
            )
            found.fixes.append(_("Dans le manuel, désactivez le préfixe et l’identifiant de symbologie (« AIM ID »)."))
            return found
    identification = resolver.identify(session, raw, auth)
    found.identification = identification
    if identification.kind in (resolver.KIND_COPY, resolver.KIND_READER, resolver.KIND_AMBIGUOUS) or (
        identification.kind == resolver.KIND_ISBN and identification.editions
    ):
        found.status = NO_SUFFIX if suffix == "none" else KNOWN
        found.details.append(_suffix_line(suffix))
        if suffix == "none":
            found.fixes.append(NO_SUFFIX_FIX)
        copy = identification.copy
        if copy is not None and identification.kind == resolver.KIND_COPY:
            found.passed = frozenset({"interne" if copy.found_by == "barcode" else "inventaire"})
        elif identification.shape.isbn is not None:
            found.passed = frozenset({"isbn"})
        return found
    if identification.keyboard and identification.keyboard_found:
        found.details.append(
            _("« {got} » ressemble à « {want} », un code connu : la douchette écrit en clavier américain.").format(
                got=received, want=identification.keyboard
            )
        )
        found.fixes.append(QWERTY_FIX)
        found.layout_problem = True
        return found
    if identification.shape.reconstruction:
        found.status = UNKNOWN
        found.details.append(
            _("« {got} » est une étiquette de l’ancienne reconstruction (avec un tiret) : elle ne désigne aucun livre. "
              "La douchette l’a bien lue.").format(got=received)
        )
        found.details.append(_suffix_line(suffix))
        return found
    if identification.shape.internal or identification.shape.is_book_number:
        found.status = UNKNOWN
        found.passed = frozenset({"inconnu"} if text == TEST_UNKNOWN or identification.shape.internal else {"isbn"})
        if identification.kind == resolver.KIND_ISBN:
            found.details.append(_("ISBN valide, mais aucun livre du catalogue ne le porte."))
        else:
            found.details.append(_("Lu correctement, mais aucun exemplaire, ISBN ou lecteur ne porte ce code."))
        found.details.append(_suffix_line(suffix))
        return found
    found.details.append(_("« {got} » ne ressemble à aucun code de biblyO ni à un ISBN.").format(got=received))
    found.fixes.append(_("Essayez d’abord les deux codes d’essai de cette page ou de la feuille test."))
    return found


@dataclass(frozen=True)
class Progress:
    """Où en est la liste de mise en service sur ce poste. Elle voyage dans le formulaire : rien n'est gardé."""

    done: frozenset[str]
    series: int
    last: int  # instant (secondes) de la dernière lecture réussie de la série

    @property
    def complete(self) -> bool:
        return self.done >= frozenset(CHECKS)


def progress(previous: Progress, result: Diagnosis, suffix: str, now: int) -> Progress:
    """La liste après cette lecture. Une lecture propre (reçue entière, avec sa touche finale) prolonge la série."""
    done = set(previous.done) | set(result.passed)
    clean_read = result.status in (READY, KNOWN, UNKNOWN) and suffix in SUFFIXES
    if suffix in SUFFIXES and clean_read:
        done.add("suffixe")
    if result.layout_problem:
        done.discard("clavier")
    elif {"code128", "isbn"} <= done:
        done.add("clavier")
    series = previous.series + 1 if clean_read and now - previous.last <= SERIES_WINDOW else (1 if clean_read else 0)
    if series >= SERIES_TARGET:
        done.add("serie")
    return Progress(frozenset(name for name in done if name in CHECKS), series, now if clean_read else previous.last)
