import pytest
from longai_mcps.calc.server import safe_eval


def test_basic_arithmetic():
    assert safe_eval("2 + 3") == 5
    assert safe_eval("10 - 4") == 6
    assert safe_eval("3 * 4") == 12
    assert safe_eval("15 / 3") == 5.0
    assert safe_eval("2 ** 8") == 256


def test_parentheses():
    assert safe_eval("(2 + 3) * 4") == 20


def test_floats():
    assert safe_eval("1.5 * 2") == 3.0


def test_unary():
    assert safe_eval("-3 + 5") == 2


def test_rejects_attribute_access():
    with pytest.raises(ValueError):
        safe_eval("().__class__")


def test_rejects_function_calls():
    with pytest.raises(ValueError):
        safe_eval("abs(-1)")


def test_rejects_names():
    with pytest.raises(ValueError):
        safe_eval("x + 1")


def test_rejects_imports():
    with pytest.raises(ValueError):
        safe_eval("__import__('os')")
