"""Reports: the calculation, the builder, /report and the schedule.

"Today" is pinned to 29.09.2026, 10:00 in Bangkok, so the numbers match
docs/SCENARIOS.md (yesterday = 28.09: 4 660 in 5 sales).
"""
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sheetbot import builder, report_builder
from sheetbot import reports as R
from sheetbot.sheets import table_from

from .fakes import FakeSheets, demo_grids, make_bot

TZ = ZoneInfo("Asia/Bangkok")


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 9, 29, 10, 0, tzinfo=TZ)}
    monkeypatch.setattr(report_builder, "local_now", lambda context: now["t"])
    from sheetbot import analysis_builder
    monkeypatch.setattr(analysis_builder, "local_now", lambda context: now["t"])
    return now


def sales():
    return table_from("Продажи", demo_grids()["Продажи"])


# ---- calculation ----

@pytest.mark.parametrize("text,expected", [
    ("28.09.2026", date(2026, 9, 28)), ("2026-09-28", date(2026, 9, 28)),
    ("9/28/2026", date(2026, 9, 28)), ("28/9/2026", date(2026, 9, 28)),
    ("28.09.26", date(2026, 9, 28)), ("28.09.2026 14:30", date(2026, 9, 28)),
    ("31.02.2026", None), ("Молоко", None), ("12", None),
])
def test_parse_date(text, expected):
    assert R.parse_date(text) == expected


def test_column_kinds_on_demo_sales():
    t = sales()
    assert R.date_columns(t) == ["Дата"]
    assert R.number_columns(t) == ["Количество", "Сумма", "Себестоимость"]
    assert R.text_columns(t) == ["Товар", "Менеджер"]


@pytest.mark.parametrize("period,start,end", [
    (R.Period.YESTERDAY, date(2026, 9, 28), date(2026, 9, 28)),
    (R.Period.TODAY, date(2026, 9, 29), date(2026, 9, 29)),
    (R.Period.LAST_7, date(2026, 9, 22), date(2026, 9, 28)),
    (R.Period.THIS_MONTH, date(2026, 9, 1), date(2026, 9, 29)),
    (R.Period.LAST_MONTH, date(2026, 8, 1), date(2026, 8, 31)),
])
def test_periods(period, start, end):
    assert R.period_range(period, date(2026, 9, 29)) == (start, end)


def test_yesterday_by_manager_matches_the_design():
    rp = R.Report("Продажи", "Дата", R.Period.YESTERDAY,
                  [R.Metric(R.Func.SUM, "Сумма"), R.Metric(R.Func.COUNT)], group_by="Менеджер")
    res = R.run(rp, sales(), date(2026, 9, 29))
    assert res.rows == 5 and res.totals == [4660, 5]
    assert res.groups == [("Анна", [2184, 3]), ("Борис", [1920, 1]), ("Вера", [556, 1])]


def test_week_average_and_extremes():
    rp = R.Report("Продажи", "Дата", R.Period.LAST_7,
                  [R.Metric(R.Func.SUM, "Сумма"), R.Metric(R.Func.AVG, "Сумма"), R.Metric(R.Func.MAX, "Сумма")])
    res = R.run(rp, sales(), date(2026, 9, 29))
    assert res.totals[0] == 11850 and res.rows == 24
    assert res.totals[1] == pytest.approx(11850 / 24)


def test_missing_column():
    rp = R.Report("Продажи", "Дата", metrics=[R.Metric(R.Func.SUM, "Выручка")])
    with pytest.raises(R.ReportError) as e:
        R.run(rp, sales(), date(2026, 9, 29))
    assert e.value.missing == "Выручка"


def test_schedule():
    rp = R.Report("Продажи", "Дата", at="09:00")
    assert R.last_due(rp, datetime(2026, 9, 29, 8, 0, tzinfo=TZ)) == datetime(2026, 9, 28, 9, 0, tzinfo=TZ)
    assert R.last_due(rp, datetime(2026, 9, 29, 9, 0, tzinfo=TZ)) == datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
    rp.freq = R.Freq.WEEKDAYS                    # 27.09.2026 is a Sunday
    assert R.last_due(rp, datetime(2026, 9, 28, 8, 0, tzinfo=TZ)).date() == date(2026, 9, 25)
    rp.freq, rp.weekday = R.Freq.WEEKLY, 0       # Mondays
    assert R.last_due(rp, datetime(2026, 9, 30, 8, 0, tzinfo=TZ)).date() == date(2026, 9, 28)
    rp.freq = R.Freq.MONTHLY
    assert R.last_due(rp, datetime(2026, 9, 30, 8, 0, tzinfo=TZ)).date() == date(2026, 9, 1)


@pytest.mark.parametrize("text,ok", [("9:30", "09:30"), ("18.00", "18:00"), ("18 00", "18:00"),
                                     ("25:00", None), ("утром", None)])
def test_valid_time(text, ok):
    assert R.valid_time(text) == ok


# ---- builder ----

def build_daily_sales(chat):
    chat.command(builder.cmd_newrule)
    for b in ("📊 Отчёт", "Продажи", "Вчера", "Сумму", "Сумма", "➕ Ещё показатель", "Число строк",
              "Готово", "Менеджер", "Каждый день", "09:00", "Меня"):
        chat.press(b)


def test_report_builder_scenario(tmp_path, clock):
    chat, storage = make_bot(tmp_path)
    build_daily_sales(chat)
    preview = chat.last
    assert "📊 Продажи · вчера" in preview and "🗓 Каждый день, 09:00" in preview
    assert "📊 Продажи · 28.09" in preview
    assert "Сумма: 4 660 · Строк: 5" in preview
    assert "По колонке «Менеджер»:" in preview
    assert "Анна — 2 184 · 3" in preview and "Вера — 556 · 1" in preview
    chat.press("✅ Сохранить")
    assert "Сохранено" in chat.last
    [rp] = storage.reports()
    assert (rp.sheet, rp.date_column, rp.group_by, rp.at) == ("Продажи", "Дата", "Менеджер", "09:00")
    assert [m.func for m in rp.metrics] == [R.Func.SUM, R.Func.COUNT]


def test_typed_time_and_weekly(tmp_path, clock):
    chat, _storage = make_bot(tmp_path)
    chat.command(builder.cmd_newrule)
    for b in ("📊 Отчёт", "Продажи", "Последние 7 дней", "Сумму", "Сумма", "Готово", "Без разбивки",
              "Раз в неделю", "Пн", "Другое время"):
        chat.press(b)
    chat.type("утром")
    assert "Это не время" in chat.last
    chat.type("8:45")
    chat.press("Меня")
    assert "🗓 По понедельникам, 08:45" in chat.last
    assert "Сумма: 11 850" in chat.last and "22.09–28.09" in chat.last


def test_empty_period_still_reports(tmp_path, clock):
    clock["t"] = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)     # no sales in October
    chat, _ = make_bot(tmp_path)
    build_daily_sales(chat)
    assert "За этот период данных нет" in chat.last


def test_report_appears_in_rules_and_can_be_edited(tmp_path, clock):
    chat, storage = make_bot(tmp_path)
    build_daily_sales(chat)
    chat.press("✅ Сохранить")
    chat.command(builder.cmd_rules)
    assert "📊 Продажи · вчера" in chat.button_texts()
    chat.press("📊 Продажи · вчера")
    assert "Статус: ▶ работает" in chat.last
    chat.press("✏ Изменить")
    chat.press("✏ Расписание")
    chat.press("По будням")
    chat.press("18:00")
    assert "🗓 По будням, 18:00" in chat.last
    chat.press("✅ Сохранить")
    rp = storage.reports()[0]
    assert rp.freq is R.Freq.WEEKDAYS and rp.at == "18:00" and rp.id == 1
    chat.command(builder.cmd_rules)
    chat.press("📊 Продажи · вчера")
    chat.press("🗑 Удалить")
    chat.press("🗑 Да, удалить")
    assert storage.reports() == []


def test_report_command_sends_now(tmp_path, clock):
    chat, _ = make_bot(tmp_path)
    chat.command(report_builder.cmd_report)
    assert "Отчётов пока нет" in chat.last
    build_daily_sales(chat)
    chat.press("✅ Сохранить")
    chat.command(report_builder.cmd_report)
    assert chat.last.startswith("📊 Продажи · 28.09")


# ---- schedule ----

def tick(chat):
    asyncio.run(report_builder.schedule_tick(chat.context))


def test_sent_at_its_time_once(tmp_path, clock):
    chat, _ = make_bot(tmp_path)
    build_daily_sales(chat)
    chat.press("✅ Сохранить")          # saved at 10:00, after today's 09:00
    bot = chat.context.bot
    tick(chat)
    assert bot.to(100) == []            # today's 09:00 had already passed when it was created
    clock["t"] = datetime(2026, 9, 30, 9, 0, 30, tzinfo=TZ)
    tick(chat)
    tick(chat)
    [msg] = bot.to(100)
    assert msg.startswith("📊 Продажи · 29.09")


def test_missed_while_off_is_caught_up_only_if_fresh(tmp_path, clock):
    chat, _storage = make_bot(tmp_path)
    build_daily_sales(chat)
    chat.press("✅ Сохранить")
    bot = chat.context.bot
    clock["t"] = datetime(2026, 9, 30, 11, 0, tzinfo=TZ)    # the bot was off at 09:00
    tick(chat)
    assert len(bot.to(100)) == 1
    clock["t"] = datetime(2026, 10, 2, 8, 0, tzinfo=TZ)     # off for a day and a half
    tick(chat)
    assert len(bot.to(100)) == 1                            # 01.10 09:00 is 23 h old: skipped


def test_paused_report_is_not_sent_and_broken_one_is_paused(tmp_path, clock):
    sheets = FakeSheets()
    chat, storage = make_bot(tmp_path, sheets)
    build_daily_sales(chat)
    chat.press("✅ Сохранить")
    sheets.grids["Продажи"][0][4] = "Себест."          # unrelated column renamed: still fine
    sheets.grids["Продажи"][0][3] = "Выручка"          # the summed column renamed
    clock["t"] = datetime(2026, 9, 30, 9, 1, tzinfo=TZ)
    tick(chat)
    bot = chat.context.bot
    [(_chat_id, text, _markup)] = bot.sent
    assert "Отчёт «Продажи · вчера» остановлен" in text and "нет колонки «Сумма»" in text
    assert storage.reports()[0].paused
    clock["t"] += timedelta(days=1)
    tick(chat)
    assert len(bot.sent) == 1
