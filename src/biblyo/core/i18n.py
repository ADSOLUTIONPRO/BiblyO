# SPDX-License-Identifier: EUPL-1.2
"""Interface française native : les textes sont écrits en français et marqués pour gettext.

Le français est la langue source : sans catalogue, `gettext` rend le texte tel quel. Le marquage permet
d'extraire les chaînes (`make i18n`) et d'ajouter une autre langue plus tard sans toucher aux gabarits.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_datetime
from babel.numbers import format_decimal

from biblyo.core.clock import from_iso

LOCALE = "fr_FR"
TIMEZONE = ZoneInfo("Europe/Paris")
NBSP = " "
NNBSP = " "


def gettext(message: str) -> str:
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    # Règle française : 0 et 1 sont au singulier.
    return singular if n <= 1 else plural


_ = gettext


def typo(text: str) -> str:
    """Espaces insécables de la typographie française devant ; : ! ? et dans les guillemets."""
    text = re.sub(r"\s+([;!?])", NNBSP + r"\1", text)
    text = re.sub(r"\s+:", NBSP + ":", text)
    text = re.sub(r"«\s*", "«" + NBSP, text)
    return re.sub(r"\s*»", NBSP + "»", text)


def date_long(value: date | datetime | None) -> str:
    """« 6 novembre 2026 »."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return format_datetime(value, "d MMMM y", tzinfo=TIMEZONE, locale=LOCALE)
    return format_date(value, "d MMMM y", locale=LOCALE)


def date_with_weekday(value: date | datetime) -> str:
    """« Vendredi 6 novembre »."""
    if isinstance(value, datetime):
        text = format_datetime(value, "EEEE d MMMM", tzinfo=TIMEZONE, locale=LOCALE)
    else:
        text = format_date(value, "EEEE d MMMM", locale=LOCALE)
    return text[:1].upper() + text[1:]


def datetime_short(value: datetime | str | None) -> str:
    """« 6 nov. 2026 à 14:03 ». Accepte aussi la forme ISO stockée en base."""
    if value is None:
        return "—"
    if isinstance(value, str):
        value = from_iso(value)
    day = format_datetime(value, "d MMM y", tzinfo=TIMEZONE, locale=LOCALE)
    hour = format_datetime(value, "HH:mm", tzinfo=TIMEZONE, locale=LOCALE)
    return f"{day} à {hour}"


def number(value: int | float) -> str:
    """« 12 345 » avec l'espace fine insécable française."""
    return format_decimal(value, locale=LOCALE)
