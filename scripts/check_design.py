#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Design system : contrastes AA vérifiés sur les jetons, aucune couleur en dur hors de `tokens.css`."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES = ROOT / "src" / "biblyo" / "static" / "src"
TOKENS = STYLES / "tokens.css"
HARD_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch)\(")

TEXT = 4.5  # WCAG AA, texte courant
GRAPHIC = 3.0  # WCAG AA, composants d'interface et icônes

_MODULES = ("cat", "ser", "she", "loan", "rdr", "enr", "inv", "io", "sta", "ok", "late", "due", "info")
#: (premier plan, fond, seuil)
PAIRS: list[tuple[str, str, float]] = [
    ("text", "bg", TEXT),
    ("text", "surface", TEXT),
    ("text", "surface-2", TEXT),
    ("muted", "bg", TEXT),
    ("muted", "surface", TEXT),
    ("muted", "surface-2", TEXT),
    ("due-ink", "surface", TEXT),
    ("faint", "bg", TEXT),
    ("faint", "surface", TEXT),
    ("accent", "bg", TEXT),
    ("accent", "surface", TEXT),
    ("on-accent", "accent", TEXT),
    ("on-accent", "accent-hover", TEXT),
    ("accent-ink", "accent-soft", TEXT),
    ("on-late", "late", TEXT),
    ("late-ink", "bg", TEXT),
    ("late-ink", "surface", TEXT),
    ("on-avatar", "avatar", TEXT),
    *(("on-avatar", f"ini-{index}", TEXT) for index in range(6)),
    ("on-coral", "brand-coral", TEXT),
    ("on-brand", "brand-ink", TEXT),
    ("side-text", "side-bg", TEXT),
    ("side-text", "side-hover", TEXT),
    ("side-text", "side-on", TEXT),
    ("side-muted", "side-bg", TEXT),
    ("side-strong", "side-on", TEXT),
    ("line-strong", "surface", GRAPHIC),
    ("line-strong", "bg", GRAPHIC),
    ("brand-coral", "bg", GRAPHIC),
    ("brand-coral", "surface", GRAPHIC),
    ("brand-coral", "side-bg", GRAPHIC),
    *((f"{module}-ink", f"{module}-soft", TEXT) for module in _MODULES),
    *((module, f"{module}-soft", GRAPHIC) for module in _MODULES),
    *((module, "surface", GRAPHIC) for module in _MODULES),
    *(("on-spine", f"cv-{index}", TEXT) for index in range(9)),
    # étagères : en-tête du meuble, étiquette des niveaux, dos en préparation, dépassement, places en plus
    ("on-wood", "wood", TEXT),
    ("she-ink", "wood-back", TEXT),
    ("she-ink", "wood-back-2", TEXT),
    ("text", "wood-back", TEXT),
    ("text", "wood-back-2", TEXT),
    ("late-ink", "wood-back", TEXT),
    ("hit", "wood-back", GRAPHIC),
    ("hit", "wood-back-2", GRAPHIC),
    ("accent", "wood-back", GRAPHIC),
    ("spine-edge", "wood-back", GRAPHIC),
    ("she", "surface-3", GRAPHIC),
    ("late", "surface-3", GRAPHIC),
    *((f"side-{m}", "side-bg", GRAPHIC) for m in ("cat", "ser", "she", "loan", "rdr", "enr", "inv", "io", "sta")),
]


def parse_tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """Valeurs hexadécimales des jetons, thème clair puis surcharges du thème sombre."""
    declaration = re.compile(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b")
    light = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    dark = re.search(r'\[data-theme="dark"\]\s*\{(.*?)\n\}', css, re.S)
    light_values = dict(declaration.findall(light.group(1))) if light else {}
    dark_values = {**light_values, **(dict(declaration.findall(dark.group(1))) if dark else {})}
    return light_values, dark_values


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def check_contrasts(css: str) -> list[str]:
    problems = []
    for theme, values in zip(("clair", "sombre"), parse_tokens(css), strict=True):
        for foreground, background, minimum in PAIRS:
            if foreground not in values or background not in values:
                problems.append(f"thème {theme} : jeton absent ({foreground} ou {background})")
                continue
            ratio = contrast(values[foreground], values[background])
            if ratio < minimum:
                problems.append(f"thème {theme} : {foreground} sur {background} = {ratio:.2f} (minimum {minimum})")
    return problems


def check_hard_colors(styles: Path = STYLES) -> list[str]:
    problems = []
    for path in sorted(styles.glob("*.css")):
        if path.name == TOKENS.name:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HARD_COLOR.search(line):
                problems.append(f"{path.name}:{number}: couleur en dur")
    return problems


def check() -> list[str]:
    return [*check_contrasts(TOKENS.read_text(encoding="utf-8")), *check_hard_colors()]


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"Design : {len(PAIRS)} paires de contraste conformes dans les deux thèmes, aucune couleur en dur.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
