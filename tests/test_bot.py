"""Command handlers with fake Telegram and a fake spreadsheet."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import gspread
import pytest

from sheetbot import bot
from sheetbot.config import Config
from sheetbot.sheets import Problem, SheetsClient, SheetsError, summarize
from sheetbot.storage import Storage

DEMO = summarize("Магазин — демо", {
    "Склад": [["Товар", "Категория", "Остаток", "Минимум", "Цена", "Закупка", "Поставщик"]] + [["x"]] * 15,
    "Продажи": [["Дата", "Товар", "Количество", "Сумма", "Себестоимость", "Менеджер"]] + [["x"]] * 101,
})


class FakeSheets:
    def __init__(self, result):
        self.result = result

    def read_all(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Chat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.sent = []

    async def send_message(self, text):
        self.sent.append(text)


def make(tmp_path, sheet_result=DEMO):
    key = tmp_path / "key.json"
    key.write_text('{"client_email": "sheetbot@demo.iam.gserviceaccount.com"}')
    cfg = Config("1:x", key, "id", ZoneInfo("Asia/Bangkok"), tmp_path)
    storage = Storage(tmp_path / "db.sqlite")
    ctx = SimpleNamespace(application=SimpleNamespace(
        bot_data={"config": cfg, "storage": storage, "sheets": FakeSheets(sheet_result)}))
    return ctx, storage


def update_for(chat_id, name="Danga"):
    chat = Chat(chat_id)
    return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(full_name=name)), chat


def run(coro):
    return asyncio.run(coro)


def test_first_start_makes_admin_and_lists_sheets(tmp_path):
    ctx, storage = make(tmp_path)
    upd, chat = update_for(100)
    run(bot.cmd_start(upd, ctx))
    assert storage.user(100).is_admin
    text = chat.sent[0]
    assert "Магазин — демо" in text and "Склад (15)" in text and "Продажи (101)" in text
    assert "admin" in text


def test_stranger_gets_no_access_and_their_chat_id(tmp_path):
    ctx, storage = make(tmp_path)
    run(bot.cmd_start(update_for(100)[0], ctx))
    upd, chat = update_for(200, "Stranger")
    run(bot.cmd_start(upd, ctx))
    run(bot.cmd_status(upd, ctx))
    assert storage.user(200) is None
    assert all("200" in m and "private" in m for m in chat.sent)


def test_status_shows_sheets_columns_and_timezone(tmp_path):
    ctx, _ = make(tmp_path)
    upd, chat = update_for(100)
    run(bot.cmd_start(upd, ctx))
    run(bot.cmd_status(upd, ctx))
    text = chat.sent[-1]
    assert text.startswith("✅")
    assert "Склад — 15 rows" in text
    assert "Товар, Категория, Остаток, Минимум, Цена, Закупка, …" in text
    assert "Asia/Bangkok" in text


@pytest.mark.parametrize("problem,expect", [
    (Problem.NOT_SHARED, "sheetbot@demo.iam.gserviceaccount.com"),
    (Problem.BAD_KEY, "JSON key"),
    (Problem.NETWORK, "isn't answering"),
])
def test_status_explains_each_problem(tmp_path, problem, expect):
    ctx, storage = make(tmp_path, SheetsError(problem))
    storage.claim_admin(100, "Danga", "en")
    upd, chat = update_for(100)
    run(bot.cmd_status(upd, ctx))
    assert chat.sent[-1].startswith("❌") and expect in chat.sent[-1]


def test_start_still_works_when_sheet_is_down(tmp_path):
    ctx, storage = make(tmp_path, SheetsError(Problem.NOT_SHARED))
    upd, chat = update_for(100)
    run(bot.cmd_start(upd, ctx))
    assert storage.user(100).is_admin
    assert "sheetbot@demo" in chat.sent[0]


def test_lang_switches_and_sticks(tmp_path):
    ctx, storage = make(tmp_path)
    upd, chat = update_for(100)
    run(bot.cmd_start(upd, ctx))
    run(bot.cmd_lang(upd, ctx))
    assert "русский" in chat.sent[-1]
    run(bot.cmd_status(upd, ctx))
    assert "Таблица «Магазин — демо» на связи" in chat.sent[-1]
    assert "Склад — строк: 15" in chat.sent[-1]
    run(bot.cmd_lang(upd, ctx))
    assert storage.user(100).lang == "en"


# ---- SheetsClient against a fake gspread ----

class FakeWS:
    def __init__(self, title):
        self.title = title


class FakeBook:
    title = "Магазин — демо"

    def worksheets(self):
        return [FakeWS("Склад"), FakeWS("Продажи")]

    def values_batch_get(self, ranges):
        assert ranges == ["'Склад'", "'Продажи'"]
        return {"valueRanges": [
            {"values": [["Товар", "Остаток"], ["Молоко", "3"]]},
            {"values": [["Дата", "Сумма"]]},
        ]}


def test_client_reads_all_sheets_in_one_request(monkeypatch, tmp_path):
    asked = {}

    def service_account(filename, scopes):
        asked["scopes"] = scopes
        return SimpleNamespace(open_by_key=lambda _id: FakeBook())
    monkeypatch.setattr(gspread, "service_account", service_account)
    info = SheetsClient(Path("k.json"), "id", sleep=lambda s: None).read_all()
    assert [(s.name, s.rows) for s in info.sheets] == [("Склад", 1), ("Продажи", 0)]
    assert asked["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]   # can't edit the sheet


def test_client_maps_not_found_to_not_shared(monkeypatch):
    def open_by_key(_id):
        raise gspread.exceptions.SpreadsheetNotFound()
    monkeypatch.setattr(gspread, "service_account",
                        lambda filename, scopes: SimpleNamespace(open_by_key=open_by_key))
    with pytest.raises(SheetsError) as e:
        SheetsClient(Path("k.json"), "id", sleep=lambda s: None).read_all()
    assert e.value.problem is Problem.NOT_SHARED


def test_client_maps_broken_key(monkeypatch):
    def broken(filename, scopes):
        raise ValueError("invalid key")
    monkeypatch.setattr(gspread, "service_account", broken)
    with pytest.raises(SheetsError) as e:
        SheetsClient(Path("k.json"), "id", sleep=lambda s: None).read_all()
    assert e.value.problem is Problem.BAD_KEY


def test_offline_is_not_reported_as_a_broken_key(monkeypatch):
    from google.auth.exceptions import TransportError

    def open_by_key(_id):
        raise TransportError("no internet")
    monkeypatch.setattr(gspread, "service_account",
                        lambda filename, scopes: SimpleNamespace(open_by_key=open_by_key))
    with pytest.raises(SheetsError) as e:
        SheetsClient(Path("k.json"), "id", sleep=lambda s: None).read_all()
    assert e.value.problem is Problem.NETWORK
