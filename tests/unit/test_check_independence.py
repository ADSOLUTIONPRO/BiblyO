# SPDX-License-Identifier: EUPL-1.2
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_independence", ROOT / "scripts" / "check_independence.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

OLD = "She" + "lf"


def _tree(tmp_path: Path, relative: str, content: str) -> Path:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    return tmp_path


def test_the_real_repository_passes():
    assert lint.check() == []


def test_a_clean_tree_passes(tmp_path):
    assert lint.check(_tree(tmp_path, "src/a.py", "shelf_position = 1  # rangement\n")) == []


def test_the_former_product_name_is_refused(tmp_path):
    assert lint.check(_tree(tmp_path, "src/a.html", f"<p>Propulsé par {OLD}</p>\n"))


def test_a_former_internal_identifier_is_refused(tmp_path):
    assert lint.check(_tree(tmp_path, "src/a.py", "lire items_" + "live\n"))


def test_the_replaced_mockup_name_is_refused(tmp_path):
    assert lint.check(_tree(tmp_path, "src/a.html", "<title>Ray" + "on</title>\n"))


def test_the_common_french_noun_is_allowed(tmp_path):
    assert lint.check(_tree(tmp_path, "src/a.html", "<p>en fin de rayon</p>\n")) == []


def test_a_historical_file_name_is_refused(tmp_path):
    problems = lint.check(_tree(tmp_path, "src/patch_loans.py", "x = 1\n"))
    assert any("nom historique" in p for p in problems)
