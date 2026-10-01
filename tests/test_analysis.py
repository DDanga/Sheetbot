"""Analysis on the demo spreadsheet: numbers from docs/SCENARIOS.md, sections 4 and 4b."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from sheetbot import analysis as A
from sheetbot import analysis_builder, builder, report_builder
from sheetbot.reports import Func, Metric, Period
from sheetbot.sheets import table_from

from .fakes import demo_grids, make_bot

TZ = ZoneInfo("Asia/Bangkok")


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 9, 29, 10, 0, tzinfo=TZ)}
    monkeypatch.setattr(report_builder, "local_now", lambda context: now["t"])
    monkeypatch.setattr(analysis_builder, "local_now", lambda context: now["t"])
    return now


def sales():
    return table_from("Продажи", demo_grids()["Продажи"])


def weekly_sum(**kw):
    return A.Analysis(A.Kind.COMPARE, "Продажи", "Дата", metric=Metric(Func.SUM, "Сумма"), unit=A.Unit.WEEK, **kw)


def stars(**kw):
    settings = {"item": "Товар", "qty": "Количество", "profit": A.Profit.DIFF, "revenue": "Сумма",
                    "cost": "Себестоимость", "period": Period.LAST_MONTH}
    return A.Analysis(A.Kind.TOP, "Продажи", "Дата", **{**settings, **kw})


# ---- calculation ----

def test_week_to_week_drop():
    res = A.compare(weekly_sum(), sales(), date(2026, 9, 29))
    assert res.current == (date(2026, 9, 22), date(2026, 9, 28))
    assert res.previous == (date(2026, 9, 15), date(2026, 9, 21))
    assert (res.now, res.before, res.now_rows, res.before_rows) == (11850, 22154, 24, 25)
    assert round(res.change, 1) == -46.5
    assert res.leaders == [("Чай зелёный", 3840), ("Апельсины", 2197), ("Гречка", 1760)]


@pytest.mark.parametrize("when,pct,worth", [
    (A.When.ALWAYS, 20, True), (A.When.DROP, 20, True), (A.When.DROP, 50, False),
    (A.When.CHANGE, 40, True), (A.When.CHANGE, 60, False),
])
def test_when_to_speak(when, pct, worth):
    an = weekly_sum(when=when, pct=pct)
    assert A.worth_sending(an, A.compare(an, sales(), date(2026, 9, 29))) is worth


def test_unit_ranges():
    assert A.unit_ranges(A.Unit.DAY, date(2026, 9, 29)) == ((date(2026, 9, 28),) * 2, (date(2026, 9, 27),) * 2)
    assert A.unit_ranges(A.Unit.MONTH, date(2026, 10, 1)) == \
        ((date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 8, 1), date(2026, 8, 31)))


def test_no_previous_data_gives_no_percent():
    res = A.compare(weekly_sum(), sales(), date(2026, 9, 5))   # nothing before September
    assert res.change is None
    assert not A.worth_sending(weekly_sum(when=A.When.DROP), res)


def test_top_and_hidden_star():
    res = A.top(stars(), sales(), date(2026, 10, 1))
    assert res.by_qty[:3] == [("Кефир", 39), ("Сыр гауда", 38), ("Багет", 37)]
    assert res.by_profit[:3] == [("Кофе в зёрнах", 8460), ("Сыр гауда", 7980), ("Чай зелёный", 5510)]
    s = res.star
    assert (s.name, s.qty_rank, s.profit_rank, s.qty, round(s.margin)) == ("Кофе в зёрнах", 10, 1, 18, 53)
    assert (s.best_seller, s.best_seller_profit) == ("Кефир", 663)


def test_top_without_profit_has_no_star():
    an = stars(profit=A.Profit.NONE, qty="")
    res = A.top(an, sales(), date(2026, 10, 1))
    assert res.by_profit == [] and res.star is None
    assert res.by_qty[0][1] >= res.by_qty[1][1]


# ---- builder and messages ----

def test_compare_builder_scenario(tmp_path, clock):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов", "Продажи", "Неделя к неделе", "Сумму", "Сумма",
              "Если упало больше чем на…", "20%", "Раз в неделю", "Пн", "09:00", "Меня"):
        chat.press(b)
    p = chat.last
    assert "📈 Продажи: Сумма · неделя к неделе" in p
    assert "🔔 только если упало больше чем на 20%" in p and "🗓 По понедельникам, 09:00" in p
    assert "📉 Сумма: −46,5% · неделя к неделе" in p
    assert "22.09–28.09: 11 850 (строк: 24)" in p and "15.09–21.09: 22 154 (строк: 25)" in p
    assert "Лидеры: Чай зелёный 3 840 · Апельсины 2 197 · Гречка 1 760" in p
    chat.press("✅ Сохранить")
    [an] = storage.analyses()
    assert an.when is A.When.DROP and an.pct == 20 and an.weekday == 0


def test_quiet_note_and_typed_percent(tmp_path, clock):
    chat, _ = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов", "Продажи", "Неделя к неделе", "Число строк",
              "Если изменилось больше чем на…", "Своё"):
        chat.press(b)
    chat.type("много")
    assert "Напиши число" in chat.last
    chat.type("15")                         # 24 vs 25 rows is only −4%
    for b in ("Каждый день", "09:00", "Меня"):
        chat.press(b)
    assert "только если изменилось больше чем на 15%" in chat.last
    assert "по расписанию я бы промолчал" in chat.last


def test_top_builder_scenario(tmp_path, clock):
    clock["t"] = datetime(2026, 10, 1, 10, 0, tzinfo=TZ)
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Топ товаров", "Продажи", "Товар", "Количество", "Выручка − себестоимость",
              "Сумма", "Себестоимость", "Прошлый месяц", "3", "1-го числа каждого месяца", "10:00", "Меня"):
        chat.press(b)
    p = chat.last
    assert "📈 Продажи: топ-3 по «Товар» · прошлый месяц" in p
    assert "🏆 Топ · 01.09–30.09" in p
    assert "1. Кефир — 39" in p and "3. Багет — 37" in p
    assert "1. Кофе в зёрнах — 8 460" in p
    assert "💎 Скрытая звезда: Кофе в зёрнах" in p
    assert "только 10-е место (18), но по прибыли 1-е: маржа 53%" in p
    assert "Кефир продаётся лучше всех, но принёс всего 663." in p
    chat.press("✅ Сохранить")
    assert storage.analyses()[0].kind is A.Kind.TOP


def test_in_rules_edit_and_report_command(tmp_path, clock):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов", "Продажи", "Неделя к неделе", "Сумму", "Сумма",
              "Всегда", "Каждый день", "09:00", "Меня", "✅ Сохранить"):
        chat.press(b)
    chat.command(builder.cmd_rules)
    title = "📈 Продажи: Сумма · неделя к неделе"
    assert title in chat.button_texts()
    chat.press(title)
    chat.press("✏ Изменить")
    chat.press("✏ Настройки")
    for b in ("Месяц к месяцу", "Сумму", "Сумма", "Всегда"):
        chat.press(b)
    assert "месяц к месяцу" in chat.last
    chat.press("✅ Сохранить")
    assert storage.analyses()[0].unit is A.Unit.MONTH
    chat.command(report_builder.cmd_report)
    assert "Сумма" in chat.last and "месяц к месяцу" in chat.last


def test_schedule_stays_quiet_unless_worth_it(tmp_path, clock):
    chat, storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📈 Анализ", "Сравнение периодов", "Продажи", "Неделя к неделе", "Сумму", "Сумма",
              "Если упало больше чем на…", "Своё"):
        chat.press(b)
    chat.type("50")                         # −46.5% isn't enough
    for b in ("Каждый день", "09:00", "Меня", "✅ Сохранить"):
        chat.press(b)
    bot = chat.context.bot
    clock["t"] = datetime(2026, 9, 30, 9, 0, 30, tzinfo=TZ)
    asyncio.run(analysis_builder.schedule_tick(chat.context))
    assert bot.to(100) == []
    an = storage.analyses()[0]
    an.pct = 20
    storage.save_analysis(an)
    clock["t"] = datetime(2026, 10, 1, 9, 0, 30, tzinfo=TZ)   # 22.09–28.09 → 23.09–29.09 now
    asyncio.run(analysis_builder.schedule_tick(chat.context))
    expected = A.compare(an, sales(), date(2026, 10, 1))
    assert len(bot.to(100)) == (1 if expected.change is not None and expected.change <= -20 else 0)


def test_one_builder_at_a_time(tmp_path, clock):
    """A half-finished report waiting for a typed time mustn't swallow the next rule's number."""
    chat, _storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📊 Отчёт", "Продажи", "Вчера", "Сумму", "Сумма", "Готово", "Без разбивки",
              "Каждый день", "Другое время"):
        chat.press(b)
    chat.command(builder.cmd_newrule)        # changed my mind
    for b in ("🔔 Уведомление", "Склад", "Остаток", "<", "С числом"):
        chat.press(b)
    chat.type("5")
    assert "Кого уведомлять" in chat.last
