"""What happens when things go wrong: Google blips, Telegram limits, odd rules, crashes."""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from sheetbot import bot as botmod
from sheetbot import builder, common, watcher
from sheetbot.sheets import Problem, SheetsClient, SheetsError, quote_sheet

from .fakes import FakeSheets, make_bot
from .test_builder import create_stock_rule

# ---- Google ----

class FlakyClient(SheetsClient):
    def __init__(self, failures, problem=Problem.NETWORK):
        self.pauses = []
        super().__init__(Path("k.json"), "id", sleep=self.pauses.append)
        self.failures, self.problem, self.calls = failures, problem, 0

    def _read_once(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise SheetsError(self.problem, "boom")
        return "Shop", {"A": [["x"]]}


def test_network_blips_are_retried():
    client = FlakyClient(failures=2)
    assert client.read_grids()[0] == "Shop"
    assert client.pauses == [2, 5]


def test_gives_up_after_three_tries():
    client = FlakyClient(failures=5)
    with pytest.raises(SheetsError):
        client.read_grids()
    assert client.calls == 3


def test_missing_share_is_not_retried():
    client = FlakyClient(failures=5, problem=Problem.NOT_SHARED)
    with pytest.raises(SheetsError):
        client.read_grids()
    assert client.calls == 1 and client.pauses == []


def test_sheet_names_with_quotes():
    assert quote_sheet("Склад") == "'Склад'"
    assert quote_sheet("O'Brien's") == "'O''Brien''s'"


# ---- the background check ----

def tick(chat):
    asyncio.run(watcher.tick(chat.context))


def test_sheet_down_only_after_three_failed_checks(tmp_path):
    sheets = FakeSheets()
    chat, storage = make_bot(tmp_path, sheets)
    bot = chat.context.bot
    sheets.error = SheetsError(Problem.NETWORK)
    tick(chat)
    tick(chat)
    assert bot.to(100) == []                    # two blips: quiet
    tick(chat)
    assert "Не получается прочитать таблицу" in bot.to(100)[-1]
    tick(chat)
    assert len(bot.to(100)) == 1                # still down: not repeated
    sheets.error = None
    tick(chat)
    assert "снова на связи" in bot.to(100)[-1]
    assert storage.meta("fail_streak") == "0"


def test_one_crashing_rule_doesnt_stop_the_others(tmp_path, monkeypatch):
    chat, _storage = make_bot(tmp_path)
    create_stock_rule(chat)
    chat.press("✅ Сохранить")
    chat.command(builder.cmd_newrule)
    for b in ("🔔 Уведомление", "Склад", "Поставщик", "пусто", "Меня", "✅ Сохранить"):
        chat.press(b)
    real = watcher.evaluate

    def evaluate(rule, *a):
        if rule.id == 2:
            raise ZeroDivisionError("weird data")
        return real(rule, *a)
    monkeypatch.setattr(watcher, "evaluate", evaluate)
    chat.context.application.bot_data["storage"].forget_hits(1)
    tick(chat)
    assert any("Остаток < Минимум" in m for m in chat.context.bot.to(100))


# ---- Telegram ----

class Bot:
    def __init__(self, *errors):
        self.errors, self.sent = list(errors), []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(text)


def send(bot):
    return asyncio.run(common.send_safely(SimpleNamespace(bot=bot), 1, "hi"))


def test_flood_control_waits_and_retries(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr(common.asyncio, "sleep", fake_sleep)
    bot = Bot(RetryAfter(timedelta(seconds=3)))
    assert send(bot) and bot.sent == ["hi"] and waits == [3]


def test_timeout_retried_once(monkeypatch):
    async def fake_sleep(s):
        pass
    monkeypatch.setattr(common.asyncio, "sleep", fake_sleep)
    assert send(Bot(TimedOut()))
    assert not send(Bot(TimedOut(), NetworkError("down")))


@pytest.mark.parametrize("error", [Forbidden("bot was blocked"), BadRequest("chat not found")])
def test_blocked_or_bad_message_not_retried(error):
    bot = Bot(error, AssertionError("must not be tried again"))
    assert not send(bot)
    assert len(bot.errors) == 1             # the second attempt never happened


# ---- unexpected errors ----

def test_admins_hear_about_crashes_but_not_every_time(tmp_path):
    chat, storage = make_bot(tmp_path)
    ctx = chat.context
    ctx.error = KeyError("oops")
    asyncio.run(botmod.on_error(None, ctx))
    asyncio.run(botmod.on_error(None, ctx))
    msgs = ctx.bot.to(100)
    assert len(msgs) == 1 and "KeyError" in msgs[0] and "sheetbot.log" in msgs[0]
    storage.set_meta("last_error_alert", "2000-01-01T00:00:00+00:00")
    ctx.error = NetworkError("connection reset")
    asyncio.run(botmod.on_error(None, ctx))
    assert len(ctx.bot.to(100)) == 1            # lost connection isn't worth a message


# ---- database ----

def test_daily_backup_keeps_a_week(tmp_path):
    _chat, storage = make_bot(tmp_path)
    folder = tmp_path / "backups"
    for day in range(1, 10):
        assert storage.backup(folder, today=f"2026-10-{day:02d}") is not None
    assert storage.backup(folder, today="2026-10-09") is None      # once a day
    kept = sorted(p.name for p in folder.glob("*.sqlite"))
    assert kept == [f"sheetbot-2026-10-{d:02d}.sqlite" for d in range(3, 10)]
    import sqlite3
    copy = sqlite3.connect(folder / kept[-1])
    assert copy.execute("SELECT name FROM users").fetchone() == ("Danga",)
