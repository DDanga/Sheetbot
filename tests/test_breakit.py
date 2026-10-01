"""The control check: trying to break the bot with odd sheets, odd input and odd taps.

Every test here started as an attempt to make the bot crash, go silent or say
something wrong. They stay as tests so the same hole can't open again.
"""
import asyncio
import time

import pytest

from sheetbot import access, builder, watcher
from sheetbot.numbers import decimal_for, parse_number
from sheetbot.rules import Op, Rule, Target, check
from sheetbot.sheets import column_letter, table_from

from .fakes import FakeSheets, demo_grids, make_bot
from .test_builder import create_stock_rule
from .test_reports import build_daily_sales, clock  # noqa: F401  (fixture)


def tick(chat):
    asyncio.run(watcher.tick(chat.context))


def with_sheet(tmp_path, name, values):
    grids = demo_grids()
    grids[name] = values
    sheets = FakeSheets(grids)
    chat, storage = make_bot(tmp_path, sheets)
    return chat, storage, sheets


def all_buttons(chat):
    return [b for row in chat.screen().inline_keyboard for b in row]


# ---------------------------------------------------------------- numbers

@pytest.mark.parametrize("text, expected", [
    ("1 234,5 ₽", 1234.5), ("1,234.5", 1234.5), ("1.234,56", 1234.56), ("12%", 12),
    ("−15", -15), ("(300)", -300), ("-0,5", -0.5), ("+7", 7), ("1'234", 1234),
    ("1,234,567", 1234567), ("1.234.567", 1234567), ("1.5E+3", 1500),
    # not numbers, however hard they try
    ("NaN", None), ("inf", None), ("Infinity", None), ("1_000", None), ("1e999", None),
    ("#N/A", None), ("#REF!", None), ("01.09.2026", None), ("12:30", None), ("1,2,3", None),
    ("--5", None), ("-", None), ("()", None), ("", None), ("   ", None), ("ок", None),
])
def test_parse_number_edge_cases(text, expected):
    assert parse_number(text) == expected


def test_lone_separator_follows_the_sheet_locale():
    assert parse_number("1,234") == 1.234                 # unknown locale: a decimal comma
    assert parse_number("1,234", decimal=",") == 1.234    # a Russian sheet
    assert parse_number("1,234", decimal=".") == 1234     # an American sheet
    assert parse_number("1.234", decimal=",") == 1234
    assert parse_number("3,5", decimal=".") == 3.5        # not three digits: still a decimal
    assert decimal_for("ru_RU") == "," and decimal_for("en_US") == "." and decimal_for("de_CH") == "."
    assert decimal_for(None) is None


# ---------------------------------------------------------------- odd sheets

def test_columns_without_a_title_or_with_the_same_title():
    table = table_from("X", [["Товар", "", "Цена", "Цена", "Цена", ""], ["a", "b", "1", "2", "3", ""]])
    assert table.headers == ["Товар", "[B]", "Цена", "Цена (2)", "Цена (3)"]
    assert column_letter(0) == "A" and column_letter(25) == "Z" and column_letter(26) == "AA"


def test_builder_on_a_sheet_with_untitled_and_repeated_columns(tmp_path):
    chat, storage, _ = with_sheet(tmp_path, "Хаос", [
        ["Товар", "", "Остаток", "Остаток"], ["Молоко", "x", "5", "1"], ["Хлеб", "y", "9", "20"]])
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Хаос"):
        chat.press(b)
    texts = [b.text for b in all_buttons(chat)]
    assert "[B]" in texts and "Остаток (2)" in texts and all(t.strip() for t in texts)
    for b in ("Остаток (2)", ">", "С числом"):
        chat.press(b)
    chat.type("10")
    chat.press("Меня")
    assert "Хлеб" in chat.last and "строк: 1" in chat.last    # the second «Остаток» column, not the first


def test_a_sheet_with_150_columns_gets_pages(tmp_path):
    headers = ["Товар"] + [f"Колонка {i}" for i in range(1, 150)]
    chat, storage, _ = with_sheet(tmp_path, "Широкий", [headers, ["Молоко"] + ["1"] * 149])
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Широкий"):
        chat.press(b)
    assert len(all_buttons(chat)) <= 100
    assert "Колонка 120" not in [b.text for b in all_buttons(chat)]
    for _ in range(4):
        chat.press("▶")
    chat.press("Колонка 120")
    assert "Колонка 120" in chat.last                        # the condition screen for that column
    chat.press("◀") if "◀" in chat.button_texts() else None  # no pager on the next screen
    assert "◀" not in chat.button_texts()


def test_a_huge_cell_doesnt_break_messages(tmp_path):
    chat, storage, sheets = with_sheet(tmp_path, "Хаос", [
        ["Товар", "Остаток"], ["Молоко " + "очень " * 2000, "1"], ["Хлеб", "50"]])
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Хаос", "Остаток", "<", "С числом"):
        chat.press(b)
    chat.type("10")
    chat.press("Меня")
    assert len(chat.last) <= 4096
    chat.press("✅ Сохранить")
    tick(chat)
    [alert] = chat.context.bot.to(100)
    assert len(alert) <= 4096 and alert.startswith("🔔 Хаос")


def test_formula_errors_and_text_in_a_number_column(tmp_path):
    table = table_from("X", [["Товар", "Остаток"], ["a", "#N/A"], ["b", "нет"], ["c", "NaN"], ["d", "2"]])
    rule = Rule("X", "Остаток", Op.LT, Target.VALUE, "5", label="Товар")
    assert [m.name for m in check(rule, table)] == ["d"]
    rule = Rule("X", "Остаток", Op.EQ, Target.VALUE, "#n/a", label="Товар")
    assert [m.name for m in check(rule, table)] == ["a"]        # but you can watch for the error itself


def test_ten_thousand_rows_are_quick(tmp_path):
    rows = [["Товар", "Остаток", "Минимум"]] + [[f"Товар {i}", str(i % 50), "10"] for i in range(10_000)]
    table = table_from("Склад", rows)
    rule = Rule("Склад", "Остаток", Op.LT, Target.COLUMN, "Минимум", label="Товар")
    started = time.perf_counter()
    matches = check(rule, table)
    assert len(matches) == 2000 and time.perf_counter() - started < 1
    text = watcher.alert_text("ru", rule, watcher.RuleOutcome(new=matches))
    assert "…и ещё 1980" in text and len(text) <= 4096


def test_a_sheet_with_only_a_header_or_nothing_at_all(tmp_path):
    grids = demo_grids()
    grids["Пусто"] = []
    grids["Шапка"] = [["Товар", "Остаток"]]
    chat, _ = make_bot(tmp_path, FakeSheets(grids))
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Уведомление")
    assert "Пусто" not in chat.button_texts()
    for b in ("Шапка", "Остаток", "пусто", "Меня"):
        chat.press(b)
    assert "Сейчас ни одна строка не подходит" in chat.last


# ---------------------------------------------------------------- odd input

def test_empty_and_overlong_typed_values(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Поставщик", "содержит"):
        chat.press(b)
    chat.type("   ")                               # "contains nothing" would match every row
    assert "нужен текст" in chat.last
    chat.type(None)                                # a sticker or a photo instead of text
    assert "нужен текст" in chat.last
    chat.type("а" * 101)
    assert "100 символов" in chat.last
    chat.type("пекарня")
    chat.press("Меня")
    assert "строк: 3" in chat.last


def test_absurd_templates(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✏ Сообщение")
    chat.type("x" * 501)
    assert "500 символов" in chat.last
    chat.type("{Нет такой}")
    assert "{Нет такой}" in chat.last
    chat.type("{Товар — без скобки и {} пустые {{Остаток}}")
    preview = chat.last
    assert "✅ Сохранить" in chat.button_texts()
    assert "{Товар — без скобки" in preview and "{3}" in preview   # left as typed, not a crash


def test_numbers_typed_in_odd_ways(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Остаток", "<", "С числом"):
        chat.press(b)
    for bad in ("бесконечность", "inf", "NaN", "1e999"):
        chat.type(bad)
        assert "Это не число" in chat.last
    chat.type("−5")                                # a typographic minus from a phone keyboard
    chat.press("Меня")
    assert "Сейчас ни одна строка не подходит" in chat.last


@pytest.mark.parametrize("typed", ["24:00", "9:60", "-1:00", "9", "99:99", "9:5"])
def test_impossible_times(tmp_path, clock, typed):  # noqa: F811
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📊 Отчёт", "Продажи", "Вчера", "Число строк", "Готово", "Без разбивки",
              "Каждый день", "Другое время"):
        chat.press(b)
    chat.type(typed)
    assert "Это не время" in chat.last


# ---------------------------------------------------------------- odd taps

def test_double_tap_picks_nothing_twice(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Уведомление")
    sheets_screen = chat.screen()
    chat.press("Склад")
    assert "Склад" in chat.last
    chat.press("Склад", on=sheets_screen)          # the second tap of a double tap
    assert "устарело" in chat.alerts[-1]
    assert "Выбери колонку" in chat.last or "колонк" in chat.last.lower()
    assert storage.rules() == []


def test_double_tap_save_saves_once(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    preview = chat.screen()
    chat.press("✅ Сохранить")
    chat.press("✅ Сохранить", on=preview)
    assert len(storage.rules()) == 1
    assert "Сохранено" in chat.last                # the confirmation stays on screen


def test_double_tap_save_on_a_report(tmp_path, clock):  # noqa: F811
    chat, storage = make_bot(tmp_path)
    build_daily_sales(chat)
    preview = chat.screen()
    chat.press("✅ Сохранить")
    chat.press("✅ Сохранить", on=preview)
    assert len(storage.reports()) == 1


def test_an_old_menu_after_starting_a_new_one(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    old = chat.screen()
    chat.command(builder.cmd_newrule)              # changed their mind, started over
    chat.press("🔔 Уведомление", on=old)
    assert "устарело" in chat.alerts[-1]


def test_deleting_a_rule_while_its_card_is_open_elsewhere(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✅ Сохранить")
    chat.command(builder.cmd_rules)
    chat.press(chat.button_texts()[0])
    card = chat.screen()
    chat.press("🗑 Удалить")
    chat.press("🗑 Да, удалить")
    assert storage.rules() == []
    for b in ("✏ Изменить", "🧪 Проверить сейчас", "⏸ Пауза", "🗑 Удалить"):
        chat.press(b, on=card)                     # the stale card: back to the (empty) list, no crash
        assert "Пока нет ни одного правила" in chat.last or "Правил" in chat.last


def test_admin_demoted_in_the_middle_of_building(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Уведомление")
    storage.update_user(100, role="member")
    chat.press("Склад")
    assert "только администратор" in chat.last.lower() or "администратор" in chat.last
    assert storage.rules() == []


def test_removed_user_cant_finish_a_rule(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    storage.add_user(200, "Второй", "admin", "", "ru")
    storage.remove_user(100)
    chat.press("✅ Сохранить")
    assert storage.rules() == []
    assert "this bot is private" in chat.last


def test_a_hundred_rules_still_fit_the_list(tmp_path):
    chat, storage = make_bot(tmp_path)
    for i in range(120):
        storage.save_rule(Rule("Склад", "Остаток", Op.LT, Target.VALUE, str(i), owner=100))
    chat.command(builder.cmd_rules)
    assert len(all_buttons(chat)) <= 100
    chat.press("▶")
    assert "2/4" in chat.button_texts()


def test_a_hundred_people_still_fit_the_list(tmp_path):
    chat, storage = make_bot(tmp_path)
    for i in range(150):
        storage.add_user(1000 + i, f"Человек {i}", "member", "", "ru")
    chat.command(access.cmd_users)
    assert len(all_buttons(chat)) <= 100
    chat.press("▶")
    assert "2/6" in chat.button_texts()


@pytest.mark.parametrize("typed", ["0", "-20", "1001", "NaN", "inf", ""])
def test_impossible_percentages(tmp_path, clock, typed):  # noqa: F811
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов", "Продажи", "Неделя к неделе", "Число строк",
              "Если изменилось больше чем на…", "Своё"):
        chat.press(b)
    chat.type(typed)
    assert "от 1 до 1000" in chat.last


def test_sheet_deleted_between_two_taps(tmp_path, clock):  # noqa: F811
    chat, _, sheets = with_sheet(tmp_path, "Лишний", [["A"], ["1"]])
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов"):
        chat.press(b)
    del sheets.grids["Продажи"]                    # someone deleted the sheet in Google meanwhile
    chat.press("Продажи")
    chat.press("⬅ Назад")                          # no crash; the list no longer has it
    assert "Продажи" not in chat.button_texts()


def test_empty_team_name_asks_again(tmp_path):
    from sheetbot import bot as botmod
    admin, storage = make_bot(tmp_path)
    admin.command(access.cmd_invite)
    payload = admin.last.split("start=")[1].split()[0]
    guest = admin.another(555, "Anna")
    guest.command(botmod.cmd_start, payload)
    _, request, markup = admin.context.bot.sent[-1]
    admin.receive(request, markup)
    admin.press("➕ Новая команда")
    admin.type("   ")
    assert "название новой команды" in admin.last
    admin.type("  Склад   №2 ")
    assert storage.user(555).team == "Склад №2"


def test_an_american_sheet_reads_thousands_right():
    from pathlib import Path
    from types import SimpleNamespace

    from sheetbot.sheets import SheetsClient

    class Book:
        title, locale = "Shop", "en_US"

        def worksheets(self):
            return [SimpleNamespace(title="Stock")]

        def values_batch_get(self, ranges, params=None):
            return {"valueRanges": [{"values": [["Item", "Qty"], ["Milk", "1,234"], ["Bread", "950"]]}]}

    client = SheetsClient(Path("k.json"), "id")
    client._book = Book()
    stock = client.table("Stock")
    rule = Rule("Stock", "Qty", Op.GT, Target.VALUE, "1000", label="Item")
    assert [m.name for m in check(rule, stock)] == ["Milk"]     # 1,234 is over a thousand here
