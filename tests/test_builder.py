"""The rule builder, driven like a user: commands, button presses, typed text.

Runs against the demo spreadsheet, so the numbers match docs/SCENARIOS.md.
"""
from sheetbot import builder
from sheetbot.rules import Op, Recipients, Target
from sheetbot.sheets import Problem, SheetsError

from .fakes import FakeSheets, make_bot


def create_stock_rule(chat):
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Уведомление")
    chat.press("Склад")
    chat.press("Остаток")
    chat.press("<")
    chat.press("С другой колонкой")
    chat.press("Минимум")
    chat.press("Меня")


def test_scenario_stock_below_minimum(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    preview = chat.last
    assert "🔔 Склад: Остаток < Минимум" in preview
    assert "Сейчас сработало бы для строк: 4" in preview
    for name in ("Молоко 3,2%", "Хлеб бородинский", "Рис басмати", "Апельсины"):
        assert name in preview
    assert "⚠ Молоко 3,2%: Остаток 3 (Минимум 10)" in preview

    chat.press("✅ Сохранить")
    assert "Сохранено" in chat.last
    [rule] = storage.rules()
    assert (rule.sheet, rule.column, rule.op, rule.target, rule.target_value, rule.label) == \
        ("Склад", "Остаток", Op.LT, Target.COLUMN, "Минимум", "Товар")
    assert rule.recipients is Recipients.ME and rule.owner == 100


def test_typed_number_and_validation(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Остаток", "<", "С числом"):
        chat.press(b)
    chat.type("много")
    assert "Это не число" in chat.last
    chat.type("5")
    chat.press("Меня")
    # stock under 5: milk 3, bread 2, rice 4
    assert "Сейчас сработало бы для строк: 3" in chat.last
    chat.press("✅ Сохранить")
    assert storage.rules()[0].target_value == "5"


def test_contains_asks_for_text_and_empty_needs_nothing(tmp_path):
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Поставщик", "содержит"):
        chat.press(b)
    assert "Напиши текст" in chat.last
    chat.type("пекарня")
    chat.press("Меня")
    assert "Поставщик содержит «пекарня»" in chat.last
    assert "строк: 3" in chat.last   # three bakery products

    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Остаток", "пусто", "Меня"):
        chat.press(b)
    assert "Сейчас ни одна строка не подходит" in chat.last


def test_changed_rule_explains_itself(tmp_path):
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Цена", "изменилось", "Меня"):
        chat.press(b)
    assert "Сработает, когда в колонке «Цена» изменится значение" in chat.last


def test_custom_message_with_placeholders(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✏ Сообщение")
    chat.type("Закажи {Товар} у {Поставщк}")
    assert "Нет такой колонки: {Поставщк}" in chat.last
    chat.type("Закажи {Товар} у {Поставщик}, осталось {Остаток}")
    assert "Закажи Молоко 3,2% у Ферма «Луг», осталось 3" in chat.last
    chat.press("✅ Сохранить")
    assert storage.rules()[0].template.startswith("Закажи {Товар}")


def test_back_and_cancel(tmp_path):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Остаток"):
        chat.press(b)
    chat.press("⬅ Назад")
    assert "Какую колонку проверяем" in chat.last
    chat.press("⬅ Назад")
    assert chat.last == "Какой лист?"
    chat.press("✖ Отмена")
    assert chat.last == "Отменено." and storage.rules() == []


def test_rule_list_pause_check_edit_delete(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✅ Сохранить")

    chat.command(builder.cmd_rules)
    assert "Правила (1)" in chat.last
    chat.press("🔔 Склад: Остаток < Минимум")
    assert "▶ работает" in chat.last

    chat.press("⏸ Пауза")
    assert "⏸ на паузе" in chat.last and storage.rules()[0].paused
    chat.press("▶ Включить")
    assert not storage.rules()[0].paused

    chat.press("🧪 Проверить сейчас")
    assert "Сейчас срабатывает для строк: 4" in chat.last
    assert "⚠ Хлеб бородинский: Остаток 2 (Минимум 12)" in chat.last
    chat.press("⬅ Назад")

    # edit only the condition: the name column and recipients are kept
    chat.press("✏ Изменить")
    chat.press("✏ Условие")
    for b in ("Остаток", "≤", "С числом"):
        chat.press(b)
    chat.type("5")
    assert "Склад: Остаток ≤ 5" in chat.last and "строк: 4" in chat.last  # 3, 2, 4, 5
    chat.press("✅ Сохранить")
    [rule] = storage.rules()
    assert rule.target is Target.VALUE and rule.label == "Товар" and rule.id == 1

    chat.command(builder.cmd_rules)
    chat.press("🔔 Склад: Остаток ≤ 5")
    chat.press("🗑 Удалить")
    assert "Удалить правило" in chat.last
    chat.press("🗑 Да, удалить")
    assert storage.rules() == [] and "Правило удалено" in chat.last


def test_rule_survives_but_reports_a_renamed_column(tmp_path):
    sheets = FakeSheets()
    chat, storage = make_bot(tmp_path, sheets)
    create_stock_rule(chat)
    chat.press("✅ Сохранить")
    sheets.grids["Склад"][0][2] = "Остаток, шт"      # someone renames the column
    chat.command(builder.cmd_rules)
    chat.press("🔔 Склад: Остаток < Минимум")
    chat.press("🧪 Проверить сейчас")
    assert "больше нет колонки «Остаток»" in chat.last


def test_sheet_down_during_builder(tmp_path):
    chat, _ = make_bot(tmp_path, FakeSheets(error=SheetsError(Problem.NOT_SHARED)))
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Уведомление")
    assert "sheetbot@demo.iam.gserviceaccount.com" in chat.last


def test_stale_menu_after_restart(tmp_path):
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    chat.context.user_data.clear()          # the bot was restarted
    chat.press("🔔 Уведомление")
    assert "меню устарело" in chat.alerts[-1]     # a short pop-up; the menu itself stays as it was
    assert chat.last == "Что создаём?"


def test_english_builder(tmp_path):
    chat, _ = make_bot(tmp_path, lang="en")
    chat.command(builder.cmd_newrule)
    chat.press("🔔 Notification")
    assert chat.last == "Which sheet?"


def test_double_tap_that_changes_nothing_is_ignored():
    import asyncio

    from telegram.error import BadRequest

    class Q:
        async def edit_message_text(self, text, reply_markup=None):
            raise BadRequest("Message is not modified: specified new message content ...")
    asyncio.run(builder.edit(Q(), "same"))


def test_row_name_column_is_picked_automatically(tmp_path):
    from sheetbot.sheets import table_from

    from .fakes import demo_grids
    grids = demo_grids()
    assert builder.guess_label(table_from("Склад", grids["Склад"])) == "Товар"
    # dates don't count as names, so on the sales sheet it's the product too
    assert builder.guess_label(table_from("Продажи", grids["Продажи"])) == "Товар"
    only_numbers = table_from("X", [["A", "B"], ["1", "2"]])
    assert builder.guess_label(only_numbers) == ""


def test_row_name_can_be_changed_in_preview(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✏ Название строки")
    assert "Сейчас: «Товар»" in chat.last
    chat.press("Поставщик")
    assert "⚠ Ферма «Луг»: Остаток 3 (Минимум 10)" in chat.last
    chat.press("✏ Название строки")
    chat.press("Номером строки")
    assert "⚠ #2: Остаток 3" in chat.last
    chat.press("✅ Сохранить")
    assert storage.rules()[0].label == ""


def test_editing_offers_back_to_rules_and_discards_changes(tmp_path):
    chat, storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✅ Сохранить")
    chat.command(builder.cmd_rules)
    chat.press("🔔 Склад: Остаток < Минимум")
    chat.press("✏ Изменить")
    assert "✖ Отмена" not in chat.button_texts()
    chat.press("✏ Условие")
    chat.press("Цена")                        # start changing, then change our mind
    chat.press("⬅ К списку правил")
    assert "Правила (1)" in chat.last
    assert storage.rules()[0].column == "Остаток"
    assert "draft" not in chat.context.user_data
