# SPDX-License-Identifier: EUPL-1.2
"""Feuille de style : hors ligne, la construction n'a rien à télécharger ; un binaire faux n'est jamais exécuté."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def build_css(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_css", ROOT / "scripts" / "build_css.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE", tmp_path / "tailwind")
    content = b"binaire de test"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setitem(module.BINARIES, "linux-arm64", ("tailwindcss-linux-arm64", digest))
    module.test_content = content
    return module


def _forbid_network(monkeypatch, module):
    def refuse(*_arguments, **_options):
        raise AssertionError("téléchargement interdit")

    monkeypatch.setattr(module.urllib.request, "urlretrieve", refuse)


def test_offline_build_never_downloads(build_css, monkeypatch):
    monkeypatch.setenv("BIBLYO_CSS_HORS_LIGNE", "1")
    _forbid_network(monkeypatch, build_css)
    with pytest.raises(SystemExit, match="aucun téléchargement"):
        build_css.binary("linux-arm64")


def test_offline_build_uses_the_verified_local_binary(build_css, monkeypatch):
    monkeypatch.setenv("BIBLYO_CSS_HORS_LIGNE", "1")
    _forbid_network(monkeypatch, build_css)
    target = build_css.target_for("linux-arm64")
    target.parent.mkdir(parents=True)
    target.write_bytes(build_css.test_content)
    assert build_css.binary("linux-arm64") == target


def test_a_tampered_binary_is_deleted_and_never_run(build_css, monkeypatch):
    monkeypatch.setenv("BIBLYO_CSS_HORS_LIGNE", "1")
    target = build_css.target_for("linux-arm64")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"autre chose")
    with pytest.raises(SystemExit, match="Empreinte incorrecte"):
        build_css.binary("linux-arm64")
    assert not target.exists()


def test_a_bad_download_leaves_nothing_behind(build_css, monkeypatch):
    monkeypatch.delenv("BIBLYO_CSS_HORS_LIGNE", raising=False)
    monkeypatch.setattr(build_css.urllib.request, "urlretrieve", lambda _url, path: Path(path).write_bytes(b"faux"))
    with pytest.raises(SystemExit, match="Empreinte incorrecte"):
        build_css.download("linux-arm64")
    assert list(build_css.CACHE.iterdir()) == []


def test_an_interrupted_download_leaves_nothing_behind(build_css, monkeypatch):
    def broken(_url, path):
        Path(path).write_bytes(b"debut")
        raise OSError("HTTP Error 504: Gateway Timeout")

    monkeypatch.setattr(build_css.urllib.request, "urlretrieve", broken)
    with pytest.raises(OSError):
        build_css.download("linux-arm64")
    assert list(build_css.CACHE.iterdir()) == []


def test_a_good_download_is_put_in_place(build_css, monkeypatch):
    monkeypatch.delenv("BIBLYO_CSS_HORS_LIGNE", raising=False)
    monkeypatch.setattr(
        build_css.urllib.request, "urlretrieve", lambda _url, path: Path(path).write_bytes(build_css.test_content)
    )
    assert build_css.main(["--telecharger", "linux-arm64"]) == 0
    target = build_css.target_for("linux-arm64")
    assert target.read_bytes() == build_css.test_content
    assert [item.name for item in build_css.CACHE.iterdir()] == [target.name]
