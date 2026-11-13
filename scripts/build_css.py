#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Construit `static/dist/app.css` avec le CLI autonome de Tailwind : version et empreinte figées, aucun npm.

Le binaire vit dans `.cache/tailwind/`, et son empreinte est vérifiée à chaque usage.

- En développement, il est téléchargé une fois s'il manque.
- Pour l'image Docker, il est déposé à l'avance (`--telecharger linux-arm64`) : avec `BIBLYO_CSS_HORS_LIGNE=1`, la
  construction ne télécharge jamais rien et s'arrête si le binaire manque ou si son empreinte diffère.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "tailwind"
VERSION = "v4.3.3"
OFFLINE = "BIBLYO_CSS_HORS_LIGNE"
#: sha256sums.txt de la version, relevé le 6 novembre 2026.
BINARIES = {
    "macos-arm64": ("tailwindcss-macos-arm64", "cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d"),
    "macos-x64": ("tailwindcss-macos-x64", "7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1"),
    "linux-arm64": ("tailwindcss-linux-arm64", "55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195"),
    "linux-x64": ("tailwindcss-linux-x64", "dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a"),
}
PLATFORMS = {
    ("Darwin", "arm64"): "macos-arm64",
    ("Darwin", "x86_64"): "macos-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Linux", "x86_64"): "linux-x64",
}
SOURCE = ROOT / "src" / "biblyo" / "static" / "src" / "app.css"
OUTPUT = ROOT / "src" / "biblyo" / "static" / "dist" / "app.css"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_platform() -> str:
    key = (platform.system(), platform.machine())
    if key not in PLATFORMS:
        raise SystemExit(f"Plateforme non prévue : {key}")
    return PLATFORMS[key]


def target_for(plateforme: str) -> Path:
    return CACHE / f"{VERSION}-{BINARIES[plateforme][0]}"


def download(plateforme: str) -> Path:
    """Télécharge le binaire dans un fichier temporaire, puis ne le met en place que si son empreinte est la bonne."""
    name, expected = BINARIES[plateforme]
    target = target_for(plateforme)
    CACHE.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{VERSION}/{name}"
    print(f"Téléchargement de {url}")
    handle, temporary = tempfile.mkstemp(dir=CACHE, prefix=".telechargement-")
    os.close(handle)
    partial = Path(temporary)
    try:
        urllib.request.urlretrieve(url, partial)
        if _digest(partial) != expected:
            raise SystemExit(f"Empreinte incorrecte pour {name} : fichier écarté, rien n'est exécuté.")
        partial.chmod(0o755)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def binary(plateforme: str | None = None) -> Path:
    plateforme = plateforme or current_platform()
    name, expected = BINARIES[plateforme]
    target = target_for(plateforme)
    if not target.exists():
        if os.environ.get(OFFLINE) == "1":
            raise SystemExit(
                f"{target.name} absent de .cache/tailwind : aucun téléchargement pendant cette construction. "
                f"Déposez-le d'abord avec « python3 scripts/build_css.py --telecharger {plateforme} »."
            )
        download(plateforme)
    if _digest(target) != expected:
        target.unlink()
        raise SystemExit(f"Empreinte incorrecte pour {name} : fichier supprimé, rien n'est exécuté.")
    target.chmod(0o755)
    return target


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--telecharger",
        choices=sorted(BINARIES),
        help="Dépose et vérifie le binaire de cette plateforme dans .cache/tailwind, sans construire.",
    )
    options = parser.parse_args(arguments)
    if options.telecharger:
        path = binary(options.telecharger)
        print(f"Binaire en place et vérifié : {path}")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [str(binary()), "--input", str(SOURCE), "--output", str(OUTPUT), "--minify"]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
