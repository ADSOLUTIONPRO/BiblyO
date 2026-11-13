# SPDX-License-Identifier: EUPL-1.2
"""Mention de titulaire : cohérente partout, et exigée pour publier."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_titulaire", ROOT / "scripts" / "check_titulaire.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _copy(tmp_path: Path) -> Path:
    for name in ("NOTICE", "README.md", "LICENSE"):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "src" / "biblyo").mkdir(parents=True)
    shutil.copy(ROOT / "src" / "biblyo" / "__init__.py", tmp_path / "src" / "biblyo" / "__init__.py")
    return tmp_path


def _replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text("utf-8").replace(old, new), "utf-8")


def test_the_repository_is_consistent():
    assert lint.check() == []


def test_publication_requires_a_named_holder(tmp_path):
    root = _copy(tmp_path)
    if lint.PLACEHOLDER in lint.declared(root):
        assert any("publication refusée" in p for p in lint.check(root, publication=True))
        for name in ("NOTICE", "README.md", "src/biblyo/__init__.py"):
            _replace(root / name, lint.PLACEHOLDER, "Camille Exemple")
    assert lint.check(root, publication=True) == []


def test_a_diverging_mention_is_refused(tmp_path):
    root = _copy(tmp_path)
    _replace(root / "README.md", "Copyright © 2026", "Copyright © 2025")
    assert any("README.md" in p for p in lint.check(root))


def test_the_licence_line_must_follow(tmp_path):
    root = _copy(tmp_path)
    _replace(root / "NOTICE", "Licensed under the EUPL-1.2", "Sous licence libre")
    assert any("NOTICE" in p and "doit suivre" in p for p in lint.check(root))
