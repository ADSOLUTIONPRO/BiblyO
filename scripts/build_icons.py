#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Construit `static/icons/sprite.svg` à partir des icônes Lucide, sans en modifier aucun tracé.

La correspondance nom biblyO → icône Lucide, la version et l'empreinte sha512 du paquet npm sont dans
`static/icons/sources.json`. Le paquet est téléchargé (ou lu depuis `--paquet <fichier.tgz>`), vérifié à son empreinte,
puis chaque symbole reprend tel quel le contenu du fichier SVG de Lucide.

Usage : `build_icons.py` (écrit le sprite) ou `build_icons.py --check` (échoue si le sprite diffère de sa
reconstruction).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "src" / "biblyo" / "static" / "icons"
SOURCES = ICONS / "sources.json"
SPRITE = ICONS / "sprite.svg"
HEADER = (
    "<!-- Icônes Lucide {version} (https://lucide.dev), licence ISC ; celles dérivées de Feather Icons sous licence"
    " MIT. Textes : licenses/lucide-LICENSE.txt. Tracés non modifiés. Généré par scripts/build_icons.py"
    " depuis sources.json : ne pas modifier à la main. -->"
)
_SVG = re.compile(r"<svg\b[^>]*>(.*)</svg>", re.S)


def verify(data: bytes, integrity: str) -> None:
    algorithm, expected = integrity.split("-", 1)
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        raise SystemExit(f"Paquet Lucide : empreinte {algorithm} inattendue")


def inner(svg: str) -> str:
    """Les éléments du fichier Lucide, à l'identique, sans les blancs de mise en page entre balises."""
    body = _SVG.search(svg)
    if body is None:
        raise ValueError("fichier SVG illisible")
    return "".join(line.strip() for line in body.group(1).splitlines() if line.strip())


def build(package: bytes, sources: dict) -> str:
    verify(package, sources["integrity"])
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {
            Path(member.name).stem: archive.extractfile(member).read().decode("utf-8")
            for member in archive.getmembers()
            if member.isfile() and member.name.startswith("package/icons/") and member.name.endswith(".svg")
        }
    symbols = []
    for name, lucide in sorted(sources["icons"].items()):
        if lucide not in files:
            raise SystemExit(f"{name} : icône Lucide « {lucide} » absente du paquet {sources['version']}")
        symbols.append(f'  <symbol id="{name}" viewBox="0 0 24 24">{inner(files[lucide])}</symbol>')
    header = HEADER.format(version=sources["version"])
    return '<svg xmlns="http://www.w3.org/2000/svg">\n' + header + "\n" + "\n".join(symbols) + "\n</svg>\n"


def package_bytes(sources: dict, argv: list[str]) -> bytes:
    if "--paquet" in argv:
        return Path(argv[argv.index("--paquet") + 1]).read_bytes()
    with urllib.request.urlopen(sources["tarball"], timeout=60) as response:  # adresse fixe du registre npm
        return response.read()


def main(argv: list[str]) -> int:
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    sprite = build(package_bytes(sources, argv), sources)
    if "--check" in argv:
        if SPRITE.read_text(encoding="utf-8") != sprite:
            print(f"{SPRITE.relative_to(ROOT)} : diffère de sa reconstruction depuis Lucide {sources['version']}")
            return 1
        print(f"Icônes : {len(sources['icons'])} symboles, identiques à Lucide {sources['version']}.")
        return 0
    SPRITE.write_text(sprite, encoding="utf-8")
    print(f"{SPRITE.relative_to(ROOT)} : {len(sources['icons'])} symboles depuis Lucide {sources['version']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
