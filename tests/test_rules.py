import pytest

from sheetbot.rules import Match, Op, Rule, RuleError, Target, check, parse_number, render
from sheetbot.sheets import table_from


@pytest.mark.parametrize("text,value", [
    ("10", 10), ("1 234,5", 1234.5), ("1,234.5", 1234.5), ("89 ₽", 89), ("12%", 12),
    ("1 000", 1000), ("-3", -3), ("", None), ("abc", None), ("1,234,567", 1234567),
])
def test_parse_number(text, value):
    assert parse_number(text) == value


TABLE = table_from("Склад", [
    ["Товар", "Остаток", "Минимум", "Поставщик"],
    ["Молоко", "3", "10", "Ферма"],
    ["Кефир", "14", "8", "Ферма"],
    ["Хлеб", "", "12", "Пекарня"],
    ["", "", "", ""],
    ["Сыр", "много", "5", "Лавка"],
])


def names(rule, table=TABLE, previous=None):
    return [m.name for m in check(rule, table, previous)]


def test_column_to_column():
    assert names(Rule("Склад", "Остаток", Op.LT, Target.COLUMN, "Минимум", "Товар")) == ["Молоко"]


def test_text_cells_and_blanks_are_skipped_for_numbers():
    assert names(Rule("Склад", "Остаток", Op.GE, Target.VALUE, "0", "Товар")) == ["Молоко", "Кефир"]


def test_empty_and_contains_and_equal_text():
    assert names(Rule("Склад", "Остаток", Op.EMPTY, label="Товар")) == ["Хлеб"]
    assert names(Rule("Склад", "Поставщик", Op.CONTAINS, Target.VALUE, "ферм", "Товар")) == ["Молоко", "Кефир"]
    assert names(Rule("Склад", "Поставщик", Op.EQ, Target.VALUE, "пекарня", "Товар")) == ["Хлеб"]


def test_row_number_when_no_label():
    assert names(Rule("Склад", "Остаток", Op.LT, Target.VALUE, "5")) == ["#2"]


def test_changed_matches_rows_by_name_not_position():
    before = table_from("Склад", [["Товар", "Остаток"], ["Кефир", "14"], ["Молоко", "3"]])
    after = table_from("Склад", [["Товар", "Остаток"], ["Молоко", "3"], ["Кефир", "9"]])
    rule = Rule("Склад", "Остаток", Op.CHANGED, label="Товар")
    assert names(rule, after, before) == ["Кефир"]


def test_missing_column_or_sheet():
    with pytest.raises(RuleError) as e:
        check(Rule("Склад", "Цена", Op.EMPTY), TABLE)
    assert e.value.missing == "Цена"
    with pytest.raises(RuleError) as e:
        check(Rule("Склад", "Остаток", Op.EMPTY), None)
    assert e.value.kind == "sheet"


def test_render_default_and_custom():
    rule = Rule("Склад", "Остаток", Op.LT, Target.COLUMN, "Минимум", "Товар")
    m = Match(2, "Молоко", {"Товар": "Молоко", "Остаток": "3", "Минимум": "10"})
    assert render(rule, m) == "⚠ Молоко: Остаток 3 (Минимум 10)"
    rule.template = "Закажи {Товар}: {Остаток} из {Минимум} {Нет}"
    assert render(rule, m) == "Закажи Молоко: 3 из 10 {Нет}"


def test_round_trip():
    rule = Rule("Склад", "Остаток", Op.LT, Target.COLUMN, "Минимум", "Товар", owner=7)
    again = Rule.from_dict(rule.to_dict(), 5)
    assert again.op is Op.LT and again.target is Target.COLUMN and again.id == 5 and again.owner == 7
