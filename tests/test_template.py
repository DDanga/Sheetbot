"""The demo spreadsheet users get (template/sheetbot-demo.xlsx), driven in English.

These pin the story the demo is built to tell, so regenerating it with
template/make_demo.py can't quietly break the examples in docs/SCENARIOS.md.
"""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from sheetbot import analysis_builder, builder, report_builder, watcher
from sheetbot.reports import parse_date
from sheetbot.texts import fmt_money, fmt_number

from .fakes import FakeSheets, make_bot, template_grids

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Asia/Bangkok"))


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(report_builder, "local_now", lambda context: NOW)
    monkeypatch.setattr(analysis_builder, "local_now", lambda context: NOW)
    chat, _ = make_bot(tmp_path, FakeSheets(template_grids(), decimal="."), lang="en")
    return chat


def build(chat, *buttons):
    chat.command(builder.cmd_newrule)
    for b in buttons:
        chat.press(b)
    return chat.last


def test_the_demo_covers_july_to_october():
    sales = template_grids()["Sales"]
    days = {parse_date(row[0]) for row in sales[1:]}
    assert min(days) == date(2026, 7, 1) and max(days) == date(2026, 10, 31)
    assert {d.month for d in days} == {7, 8, 9, 10}
    assert all(sum(d.month == m for d in days) >= 30 for m in (7, 8, 9, 10))    # no gaps longer than a day


def test_low_stock_alert(chat):
    preview = build(chat, "🔔 Notification", "Stock", "In stock", "<", "Another column", "Minimum", "Me")
    assert "Right now it would fire for 4 rows: Whole milk, Sourdough bread, Basmati rice, Oranges." in preview
    chat.press("✅ Save")
    asyncio.run(watcher.tick(chat.context))
    [alert] = chat.context.bot.to(100)
    assert "⚠ Sourdough bread: In stock 2 (Minimum 12)" in alert


def test_daily_report_shows_dollars(chat):
    preview = build(chat, "📊 Report", "Sales", "Yesterday", "Sum", "Amount", "➕ One more", "Number of rows",
                    "Done", "Manager", "Every day", "09:00", "Me")
    assert "📊 Sales · 30.09" in preview
    assert "Amount: $42.82 · Rows: 5" in preview
    assert "Ben — $13.00 · 1" in preview


def test_bad_week_is_reported(chat):
    preview = build(chat, "📈 Analysis", "Compare periods", "Sales", "Week to week", "Sum", "Amount",
                    "If it fell by more than…", "20%", "Once a week", "Mon", "09:00", "Me")
    assert "📉 Amount: −32.7% · week to week" in preview
    assert "24.09–30.09: $303.78 (rows: 34)" in preview
    assert "stay quiet" not in preview          # a drop this big is worth a message


def test_september_grew_after_the_august_slump(chat):
    preview = build(chat, "📈 Analysis", "Compare periods", "Sales", "Month to month", "Sum", "Amount",
                    "Always", "Once a week", "Mon", "09:00", "Me")
    assert "📈 Amount: +20.6% · month to month" in preview
    assert "01.09–30.09: $1,827.32 (rows: 211)" in preview


def test_coffee_is_the_hidden_star(chat):
    preview = build(chat, "📈 Analysis", "Top items", "Sales", "Product", "Quantity", "Revenue − cost",
                    "Amount", "Cost", "Last month", "3", "On the 1st of the month", "10:00", "Me")
    assert "1. Greek yogurt — 80" in preview
    assert "1. Coffee beans — $121.50" in preview
    assert "💎 Hidden star: Coffee beans\nOnly #13 by quantity (15), but #1 by profit, margin 54%." in preview


@pytest.mark.parametrize("lang, value, symbol, expected", [
    ("en", 1827.32, "$", "$1,827.32"), ("en", 13, "$", "$13.00"), ("en", -5.4, "$", "−$5.40"),
    ("ru", 1827.32, "₽", "1 827,32 ₽"), ("en", 4660, "", "4,660"), ("ru", 4660, "", "4 660"),
    ("en", None, "$", "—"),
])
def test_money_and_numbers_read_naturally(lang, value, symbol, expected):
    assert fmt_money(lang, value, symbol) == expected
    if not symbol:
        assert fmt_number(lang, value) == expected
