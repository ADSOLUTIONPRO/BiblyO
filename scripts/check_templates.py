#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Gabarits compatibles avec la CSP stricte : rien en ligne, rien d'externe, composants Alpine déclarés.

Aussi : expressions Alpine simples, car le build CSP n'évalue pas de code arbitraire.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "biblyo"

JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
SCRIPT_INLINE = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>", re.I)
STYLE_BLOCK = re.compile(r"<style\b", re.I)
STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*["']""", re.I)
EVENT_ATTR = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.I)
HX_ON = re.compile(r"""\shx-on[:-]""", re.I)
EXTERNAL = re.compile(r"""\b(?:src|href|action|poster|data)\s*=\s*["'](?:https?:)?//""", re.I)
ALPINE_DATA_USE = re.compile(r"""x-data\s*=\s*["']([^"']*)["']""")
ALPINE_DATA_DECL = re.compile(r"""Alpine\.data\(\s*["'](\w+)["']""")
ALPINE_EXPR = re.compile(r"""\s(?:x-show|x-text|x-model|x-if|:[\w-]+|@[\w.:-]+)\s*=\s*["']([^"']*)["']""")
SIMPLE_EXPRESSION = re.compile(r"^[A-Za-z_$][\w$]*$")
JS_FORBIDDEN = re.compile(r"\beval\s*\(|new\s+Function\s*\(|\.innerHTML\s*=|document\.write\s*\(")


def declared_components(src: Path = SRC) -> set[str]:
    names: set[str] = set()
    for path in (src / "static" / "js").rglob("*.js"):
        names.update(ALPINE_DATA_DECL.findall(path.read_text(encoding="utf-8")))
    return names


def _templates(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*.html") if "templates" in p.parts)


def check_template(path: Path, components: set[str]) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    # Les balises Jinja sont retirées : `{% if x %}` n'est pas un attribut.
    html = JINJA.sub("", raw)
    problems = []
    for pattern, label in (
        (SCRIPT_INLINE, "script en ligne"),
        (STYLE_BLOCK, "bloc de style"),
        (STYLE_ATTR, "attribut style"),
        (EVENT_ATTR, "gestionnaire d'événement en ligne"),
        (HX_ON, "hx-on"),
        (EXTERNAL, "ressource externe"),
    ):
        if pattern.search(html):
            problems.append(f"{path}: {label}")
    for name in ALPINE_DATA_USE.findall(html):
        if name not in components:
            problems.append(f"{path}: composant Alpine non déclaré « {name} »")
    for expression in ALPINE_EXPR.findall(html):
        if not SIMPLE_EXPRESSION.match(expression):
            problems.append(f"{path}: expression Alpine trop complexe « {expression} »")
    return problems


def check_scripts(src: Path = SRC) -> list[str]:
    problems = []
    for path in sorted((src / "static" / "js").rglob("*.js")):
        if JS_FORBIDDEN.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path}: eval, Function, innerHTML ou document.write")
    return problems


def check(src: Path = SRC) -> list[str]:
    components = declared_components(src) if (src / "static" / "js").exists() else set()
    problems: list[str] = []
    for path in _templates(src):
        problems.extend(check_template(path, components))
    if (src / "static" / "js").exists():
        problems.extend(check_scripts(src))
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("Gabarits : aucun script ni style en ligne, aucune ressource externe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
