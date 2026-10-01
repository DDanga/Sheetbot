"""The background check, run tick by tick on the demo spreadsheet."""
import asyncio

from sheetbot import builder, watcher
from sheetbot.sheets import Problem, SheetsError
from sheetbot.storage import Storage

from .fakes import FakeSheets, make_bot
from .test_builder import create_stock_rule


def tick(chat):
    asyncio.run(watcher.tick(chat.context))


def stock(sheets, product, column, value):
    grid = sheets.grids["Склад"]
    col = grid[0].index(column)
    for row in grid[1:]:
        if row[0] == product:
            row[col] = value
            return
    raise AssertionError(product)


def setup(tmp_path, **kw):
    sheets = FakeSheets()
    chat, storage = make_bot(tmp_path, sheets, **kw)
    return chat, storage, sheets, chat.context.bot


def saved_stock_rule(chat):
    create_stock_rule(chat)
    chat.press("✅ Сохранить")


def test_first_check_sends_everything_once(tmp_path):
    chat, _, _, bot = setup(tmp_path)
    saved_stock_rule(chat)
    tick(chat)
    [msg] = bot.to(100)
    assert msg.startswith("🔔 Склад: Остаток < Минимум")
    assert "⚠ Молоко 3,2%: Остаток 3 (Минимум 10)" in msg
    assert msg.count("⚠") == 4
    tick(chat)
    assert len(bot.to(100)) == 1          # nothing new → silence


def test_new_problem_and_back_to_normal(tmp_path):
    chat, _, sheets, bot = setup(tmp_path)
    saved_stock_rule(chat)
    tick(chat)
    stock(sheets, "Молоко 3,2%", "Остаток", "25")   # restocked
    stock(sheets, "Кефир", "Остаток", "2")           # running out
    tick(chat)
    alert, resolved = bot.to(100)[1:]
    assert "⚠ Кефир: Остаток 2 (Минимум 8)" in alert and "Молоко" not in alert
    assert "Молоко 3,2% — снова в норме" in resolved


def test_back_to_normal_can_be_turned_off(tmp_path):
    chat, _, sheets, bot = setup(tmp_path)
    create_stock_rule(chat)
    chat.press("🔁 Сообщать «снова в норме»: да")
    assert "🔁 Сообщать «снова в норме»: нет" in chat.button_texts()
    chat.press("✅ Сохранить")
    tick(chat)
    stock(sheets, "Молоко 3,2%", "Остаток", "25")
    tick(chat)
    assert len(bot.to(100)) == 1


def test_restart_doesnt_repeat_old_alerts(tmp_path):
    chat, storage, _, bot = setup(tmp_path)
    saved_stock_rule(chat)
    tick(chat)
    storage.close()
    chat.context.application.bot_data["storage"] = Storage(tmp_path / "db.sqlite")
    tick(chat)
    assert len(bot.to(100)) == 1


def test_editing_the_condition_starts_over(tmp_path):
    chat, _, _, bot = setup(tmp_path)
    saved_stock_rule(chat)
    tick(chat)
    chat.command(builder.cmd_rules)
    chat.press("🔔 Склад: Остаток < Минимум")
    chat.press("✏ Изменить")
    chat.press("✏ Условие")
    for b in ("Остаток", "≤", "С числом"):
        chat.press(b)
    chat.type("5")
    chat.press("✅ Сохранить")
    tick(chat)
    latest = bot.to(100)[-1]
    assert latest.startswith("🔔 Склад: Остаток ≤ 5") and latest.count("⚠") == 4


def test_changed_rule_reports_old_and_new_value(tmp_path):
    chat, _, sheets, bot = setup(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Цена", "изменилось", "Меня", "✅ Сохранить"):
        chat.press(b)
    tick(chat)                                      # first look: just remembers the prices
    assert bot.to(100) == []
    stock(sheets, "Молоко 3,2%", "Цена", "95")
    tick(chat)
    assert "✏ Молоко 3,2%: Цена 89 → 95" in bot.to(100)[0]
    tick(chat)
    assert len(bot.to(100)) == 1                    # reported once


def test_sorting_the_sheet_is_not_a_change(tmp_path):
    chat, _, sheets, bot = setup(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Цена", "изменилось", "Меня", "✅ Сохранить"):
        chat.press(b)
    tick(chat)
    grid = sheets.grids["Склад"]
    grid[1:] = sorted(grid[1:], key=lambda r: r[0])
    tick(chat)
    assert bot.to(100) == []


def test_renamed_column_pauses_rule_and_offers_fix(tmp_path):
    chat, storage, sheets, bot = setup(tmp_path)
    saved_stock_rule(chat)
    tick(chat)
    sheets.grids["Склад"][0][2] = "Остаток, шт"
    tick(chat)
    _, text, markup = bot.sent[-1]
    assert "остановлено" in text and "нет колонки «Остаток»" in text and "«Остаток, шт»" in text
    assert storage.rules()[0].paused
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert buttons == ["Использовать «Остаток, шт»", "✏ Изменить"]
    tick(chat)
    assert len(bot.sent) == 2                       # told once, not every 5 minutes

    chat.command(builder.cmd_rules)
    assert "⏸ Склад: Остаток < Минимум" in chat.button_texts()
    chat.messages.append([text, markup])            # the admin taps the button in that alert
    chat.press("Использовать «Остаток, шт»")
    assert "Исправлено" in chat.last
    rule = storage.rules()[0]
    assert rule.column == "Остаток, шт" and not rule.paused and "broken" not in rule.extra
    tick(chat)
    assert bot.to(100)[-1].count("⚠") == 4          # working again


def test_sheet_down_told_once_and_recovery(tmp_path):
    chat, _, sheets, bot = setup(tmp_path)
    saved_stock_rule(chat)
    sheets.error = SheetsError(Problem.NOT_SHARED)
    tick(chat)
    tick(chat)
    [down] = bot.to(100)
    assert "Не получается прочитать таблицу" in down and "sheetbot@demo" in down
    sheets.error = None
    tick(chat)
    assert "снова на связи" in bot.to(100)[1]
    assert bot.to(100)[2].startswith("🔔")


def test_everyone_gets_it_in_their_language_and_a_blocked_user_doesnt_stop_others(tmp_path):
    chat, storage, _, bot = setup(tmp_path)
    storage.db.execute("INSERT INTO users (chat_id, name, role, lang) VALUES (200, 'Anna', 'staff', 'en')")
    storage.db.execute("INSERT INTO users (chat_id, name, role, lang) VALUES (300, 'Boris', 'staff', 'ru')")
    storage.db.commit()
    bot.blocked.add(200)
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Остаток", "<", "С другой колонкой", "Минимум",
              "Всех, у кого есть доступ", "✅ Сохранить"):
        chat.press(b)
    storage.db.execute("UPDATE users SET lang = 'en' WHERE chat_id = 100")
    storage.db.commit()
    tick(chat)
    assert bot.to(100)[0].startswith("🔔 Склад: Остаток < Минимум")
    assert bot.to(300) and bot.to(200) == []


def test_status_shows_rules_and_last_check(tmp_path):
    from sheetbot import bot as botmod
    chat, _, _, _ = setup(tmp_path)
    saved_stock_rule(chat)
    chat.command(botmod.cmd_status)
    assert "Правил: 1 активных, 0 на паузе" in chat.last and "Ещё не проверял" in chat.last
    tick(chat)
    chat.command(botmod.cmd_status)
    assert "Последняя проверка: только что · каждые 5 мин" in chat.last


def test_overlapping_checks_dont_double_report(tmp_path):
    chat, _, _, bot = setup(tmp_path)
    saved_stock_rule(chat)

    async def two_at_once():
        await asyncio.gather(watcher.tick(chat.context), watcher.tick(chat.context))
    asyncio.run(two_at_once())
    assert len(bot.to(100)) == 1


def test_suggestion_for_renamed_sheet():
    from sheetbot.rules import Op, Rule, RuleError
    from sheetbot.sheets import table_from
    rule = Rule("Склад", "Остаток", Op.EMPTY)
    tables = {"Склад 2026": table_from("Склад 2026", [["Остаток"]])}
    assert watcher.suggestion(RuleError("Склад", "sheet"), rule, tables) == "Склад 2026"
