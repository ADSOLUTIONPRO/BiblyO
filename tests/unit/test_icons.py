# SPDX-License-Identifier: EUPL-1.2
"""Icônes : le sprite est la reconstruction déclarée depuis Lucide, et chaque icône appelée existe."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ICONS = ROOT / "src" / "biblyo" / "static" / "icons"
SOURCES = json.loads((ICONS / "sources.json").read_text(encoding="utf-8"))
SPRITE = (ICONS / "sprite.svg").read_text(encoding="utf-8")
_spec = importlib.util.spec_from_file_location("build_icons", ROOT / "scripts" / "build_icons.py")
build_icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_icons)

SYMBOLS = dict(re.findall(r'<symbol id="([^"]+)" viewBox="0 0 24 24">(.*?)</symbol>', SPRITE))


def test_sprite_declares_its_origin_and_licence():
    assert f"Icônes Lucide {SOURCES['version']}" in SPRITE
    assert "licenses/lucide-LICENSE.txt" in SPRITE
    licence = (ICONS / "licenses" / "lucide-LICENSE.txt").read_text(encoding="utf-8")
    assert licence.startswith("ISC License") and "Copyright (c) 2013-present Cole Bemis" in licence


def test_sprite_has_exactly_the_declared_symbols():
    assert sorted(SYMBOLS) == sorted(SOURCES["icons"])
    assert SPRITE.count("<symbol") == len(SOURCES["icons"])


def test_every_icon_called_by_a_template_exists():
    called = set()
    for path in (ROOT / "src" / "biblyo").rglob("*"):
        if path.suffix in (".html", ".py"):
            text = path.read_text(encoding="utf-8")
            called |= set(re.findall(r"""\bicon\(\s*["']([a-z]+)["']""", text))
            called |= set(re.findall(r"""icon_button\([^)]*?,\s*["']([a-z]+)["']""", text))
    assert called, "aucun appel d'icône trouvé : le motif de recherche est à revoir"
    assert called <= set(SYMBOLS), sorted(called - set(SYMBOLS))


def _package(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"package/icons/{name}.svg")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _integrity(data: bytes) -> str:
    import base64
    import hashlib

    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_build_keeps_lucide_markup_verbatim():
    svg = (
        '<!-- @license -->\n<svg\n  class="lucide"\n  viewBox="0 0 24 24"\n>\n'
        '  <path d="M1 2h3" />\n  <circle cx="12" cy="12" r="3" />\n</svg>\n'
    )
    package = _package({"thing": svg})
    sprite = build_icons.build(package, {"version": "9.9.9", "integrity": _integrity(package), "icons": {"a": "thing"}})
    assert '<symbol id="a" viewBox="0 0 24 24"><path d="M1 2h3" /><circle cx="12" cy="12" r="3" /></symbol>' in sprite


def test_build_refuses_a_package_with_another_digest():
    package = _package({"thing": '<svg><path d="M1 2" /></svg>'})
    with pytest.raises(SystemExit, match="empreinte"):
        build_icons.build(package, {"version": "9.9.9", "integrity": _integrity(b"autre"), "icons": {"a": "thing"}})


def test_build_refuses_an_unknown_lucide_icon():
    package = _package({"thing": '<svg><path d="M1 2" /></svg>'})
    with pytest.raises(SystemExit, match="absente"):
        build_icons.build(package, {"version": "9.9.9", "integrity": _integrity(package), "icons": {"a": "other"}})
