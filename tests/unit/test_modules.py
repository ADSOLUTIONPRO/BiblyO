# SPDX-License-Identifier: EUPL-1.2
import pytest

from biblyo.core.modules import Module, ModuleError, Permission, order_modules


def test_modules_are_ordered_by_dependency():
    a = Module("a", "A", depends_on=("b",))
    b = Module("b", "B")
    assert [m.code for m in order_modules([a, b])] == ["b", "a"]


def test_duplicate_code_is_refused():
    with pytest.raises(ModuleError, match="deux fois"):
        order_modules([Module("a", "A"), Module("a", "A bis")])


def test_missing_dependency_is_refused():
    with pytest.raises(ModuleError, match="absent"):
        order_modules([Module("a", "A", depends_on=("z",))])


def test_dependency_cycle_is_refused():
    with pytest.raises(ModuleError, match="Cycle"):
        order_modules([Module("a", "A", depends_on=("b",)), Module("b", "B", depends_on=("a",))])


def test_duplicate_permission_is_refused():
    p = Permission("x.y", "X")
    with pytest.raises(ModuleError, match="Permissions"):
        order_modules([Module("a", "A", permissions=(p,)), Module("b", "B", permissions=(p,))])
