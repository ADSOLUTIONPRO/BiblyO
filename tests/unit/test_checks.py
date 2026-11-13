# SPDX-License-Identifier: EUPL-1.2
"""Contrôles automatiques : ils passent sur le dépôt et échouent sur des fichiers piégés."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


templates = _load("check_templates")
design = _load("check_design")
vendor = _load("check_vendor")


def test_templates_of_the_repository_pass():
    assert templates.check() == []


@pytest.mark.parametrize(
    ("content", "label"),
    [
        ("<script>alert(1)</script>", "script en ligne"),
        ("<style>p{}</style>", "bloc de style"),
        ('<p style="color:red">x</p>', "attribut style"),
        ('<a onclick="x()">x</a>', "gestionnaire"),
        ('<a hx-on:click="x()">x</a>', "hx-on"),
        ('<img src="https://exemple.invalid/a.png">', "ressource externe"),
        ('<script src="//exemple.invalid/a.js"></script>', "ressource externe"),
        ('<div x-data="inconnu"></div>', "non déclaré"),
        ('<div x-data="connu" x-show="a && b"></div>', "trop complexe"),
        ('<button @click="count++">x</button>', "trop complexe"),
    ],
)
def test_trapped_template_is_refused(tmp_path, content, label):
    path = tmp_path / "templates" / "piege.html"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    problems = templates.check_template(path, {"connu"})
    assert any(label in problem for problem in problems), problems


def test_clean_template_passes(tmp_path):
    path = tmp_path / "propre.html"
    path.write_text(
        '<script defer src="/static/a.js"></script><div x-data="connu" @click="toggle" :aria-expanded="expanded"></div>'
        '{% if on %}<a href="{{ url }}">x</a>{% endif %}<svg style-x="1"></svg>',
        encoding="utf-8",
    )
    assert templates.check_template(path, {"connu"}) == []


@pytest.mark.parametrize("code", ["eval('1')", "new Function('x')", "el.innerHTML = x", "document.write(x)"])
def test_dangerous_script_is_refused(tmp_path, code):
    js = tmp_path / "static" / "js"
    js.mkdir(parents=True)
    (js / "piege.js").write_text(code, encoding="utf-8")
    assert templates.check_scripts(tmp_path)


def test_design_of_the_repository_passes():
    assert design.check() == []


def test_contrast_formula():
    assert design.contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert design.contrast("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)


def test_low_contrast_token_is_refused():
    css = design.TOKENS.read_text(encoding="utf-8").replace("--muted:#5B605D", "--muted:#C8C4BC")
    problems = design.check_contrasts(css)
    assert any("thème clair : muted sur" in problem for problem in problems)


def test_low_contrast_in_dark_theme_is_refused():
    css = design.TOKENS.read_text(encoding="utf-8").replace("--muted:#A8B2B0", "--muted:#2B373B")
    assert any("thème sombre : muted sur" in problem for problem in design.check_contrasts(css))


def test_missing_token_is_reported():
    css = design.TOKENS.read_text(encoding="utf-8").replace("--faint:#6A6F6B;", "")
    assert any("jeton absent" in problem for problem in design.check_contrasts(css))


@pytest.mark.parametrize("value", ["#fff", "#13252A", "rgba(0,0,0,.1)", "rgb(1,2,3)", "hsl(1,2%,3%)"])
def test_hard_coded_colour_is_refused(tmp_path, value):
    (tmp_path / "components.css").write_text(f".a{{color:{value}}}", encoding="utf-8")
    assert design.check_hard_colors(tmp_path)


def test_tokens_file_may_hold_colours(tmp_path):
    (tmp_path / "tokens.css").write_text(":root{--a:#fff}", encoding="utf-8")
    assert design.check_hard_colors(tmp_path) == []


def test_vendored_files_match_their_digests():
    assert vendor.check() == []


def test_altered_vendored_file_is_detected(tmp_path):
    for folder in vendor.FOLDERS:
        (tmp_path / folder).mkdir()
    library = tmp_path / "vendor" / "lib.js"
    library.write_text("original", encoding="utf-8")
    ledger = tmp_path / "vendor" / "checksums.sha256"
    digest = vendor.digests(tmp_path, ledger.name)["vendor/lib.js"]
    ledger.write_text(f"{digest}  vendor/lib.js\n", encoding="utf-8")
    assert vendor.check(tmp_path, ledger) == []

    library.write_text("modifié", encoding="utf-8")
    assert any("empreinte incorrecte" in p for p in vendor.check(tmp_path, ledger))
    library.write_text("original", encoding="utf-8")
    (tmp_path / "fonts" / "intrus.woff2").write_text("x", encoding="utf-8")
    assert any("non enregistré" in p for p in vendor.check(tmp_path, ledger))
    library.unlink()
    assert any("absent" in p for p in vendor.check(tmp_path, ledger))


def test_missing_ledger_is_reported(tmp_path):
    assert vendor.check(tmp_path, tmp_path / "absent")


i18n_check = _load("check_i18n")


def test_catalogue_is_up_to_date_and_french():
    assert i18n_check.check() == []


def test_catalogue_really_contains_the_interface():
    known = i18n_check.messages(i18n_check.CATALOGUE)
    assert {"Se connecter", "Mon compte", "Accès refusé", "Identifiant ou mot de passe incorrect."} <= known


@pytest.mark.parametrize("text", ["Save", "Log in", "Delete this", "Please wait"])
def test_english_interface_terms_are_detected(text):
    assert i18n_check.english_terms({text}) == [text]


@pytest.mark.parametrize("text", ["Enregistrer", "Se connecter", "Mon compte", "Fermer la session", "Non cochée"])
def test_french_terms_pass(text):
    assert i18n_check.english_terms({text}) == []


def test_catalogue_detects_a_text_missing_from_it(tmp_path, monkeypatch):
    stale = tmp_path / "ancien.pot"
    stale.write_text('msgid ""\nmsgstr ""\n\nmsgid "Texte disparu"\nmsgstr ""\n', encoding="utf-8")
    monkeypatch.setattr(i18n_check, "CATALOGUE", stale)
    problems = i18n_check.check()
    assert any("disparu du code" in problem for problem in problems)
    assert any("absent du catalogue" in problem for problem in problems)
