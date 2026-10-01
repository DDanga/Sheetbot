"""Invites, approval, teams, admin rights, /pause — between two real-looking people."""
import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sheetbot import access, builder, report_builder, watcher
from sheetbot import bot as botmod

from .fakes import make_bot
from .test_builder import create_stock_rule

ANNA = 200


def invite_link(admin):
    admin.command(access.cmd_invite)
    text = admin.last
    assert "https://t.me/sheetbot_demo_bot?start=inv_" in text
    return text.split("start=")[1].split()[0]


def join(admin, chat_id=ANNA, name="Anna", team_button="Без команды"):
    payload = invite_link(admin)
    guest = admin.another(chat_id, name)
    guest.command(botmod.cmd_start, payload)
    assert "Request sent to the admin" in guest.last      # newcomers start in English
    _, request, markup = admin.context.bot.sent[-1]
    assert f"🔑 {name} просит доступ" in request
    admin.receive(request, markup)
    admin.press(team_button)
    return guest


def test_invite_and_new_team(tmp_path):
    admin, storage = make_bot(tmp_path)
    payload = invite_link(admin)
    guest = admin.another(ANNA, "Anna")
    guest.command(botmod.cmd_start, payload)
    _, request, markup = admin.context.bot.sent[-1]
    admin.receive(request, markup)
    assert admin.button_texts() == ["Без команды", "➕ Новая команда", "✖ Отклонить"]
    admin.press("➕ Новая команда")
    admin.type("Склад")
    assert "✅ Доступ для Anna открыт (команда «Склад»)" in admin.last
    anna = storage.user(ANNA)
    assert anna and not anna.is_admin and anna.team == "Склад"
    welcome = admin.context.bot.to(ANNA)[-1]
    assert welcome.startswith("Access granted ✅")
    assert "/pause" in welcome and "/newrule" not in welcome   # members get the short help


def test_link_is_one_time_and_expires(tmp_path):
    admin, storage = make_bot(tmp_path)
    payload = invite_link(admin)
    first = admin.another(ANNA, "Anna")
    first.command(botmod.cmd_start, payload)
    second = admin.another(300, "Boris")
    second.command(botmod.cmd_start, payload)
    assert "invalid or has expired" in second.last

    old = invite_link(admin)
    storage.db.execute("UPDATE invites SET created_at = datetime('now', '-25 hours') WHERE token = ?",
                       (old[len("inv_"):],))
    storage.db.commit()
    third = admin.another(400, "Vera")
    third.command(botmod.cmd_start, old)
    assert "invalid or has expired" in third.last


def test_reject(tmp_path):
    admin, storage = make_bot(tmp_path)
    payload = invite_link(admin)
    guest = admin.another(ANNA, "Anna")
    guest.command(botmod.cmd_start, payload)
    _, request, markup = admin.context.bot.sent[-1]
    admin.receive(request, markup)
    admin.press("✖ Отклонить")
    assert "отклонён" in admin.last and storage.user(ANNA) is None
    assert "declined" in admin.context.bot.to(ANNA)[-1]
    admin.receive(request, markup)            # a second tap on the old buttons
    admin.press("Без команды")
    assert "уже обработан" in admin.last and storage.user(ANNA) is None


def test_stranger_without_invite_is_still_refused(tmp_path):
    admin, storage = make_bot(tmp_path)
    stranger = admin.another(999, "Stranger")
    stranger.command(botmod.cmd_start, "inv_made_up")
    assert "invalid" in stranger.last and storage.user(999) is None
    stranger.command(botmod.cmd_start)
    assert "private" in stranger.last


def test_members_receive_but_dont_manage(tmp_path):
    admin, _ = make_bot(tmp_path)
    anna = join(admin)
    for handler in (builder.cmd_newrule, builder.cmd_rules, access.cmd_invite, access.cmd_users):
        anna.command(handler)
        assert "Only an admin" in anna.last
    anna.command(report_builder.cmd_report)
    assert "No reports yet" in anna.last


def test_team_recipients_and_pause(tmp_path):
    admin, storage = make_bot(tmp_path)
    join(admin, ANNA, "Anna", "Без команды")
    storage.update_user(ANNA, team="Склад")
    boris = join(admin, 300, "Boris", "Без команды")
    create_stock_rule(admin)                        # recipients: me
    admin.press("✏ Кому")
    assert "👥 Склад" in admin.button_texts()
    admin.press("👥 Склад")
    assert "👤 Команда «Склад»" in admin.last
    admin.press("✅ Сохранить")

    boris.command(access.cmd_pause)                 # not in the team anyway
    anna_chat = admin.another(ANNA, "Anna")
    asyncio.run(watcher.tick(admin.context))
    bot = admin.context.bot
    assert any("Склад: Остаток < Минимум" in m for m in bot.to(ANNA))
    assert not any("Остаток" in m for m in bot.to(100))
    assert not any("Остаток" in m for m in bot.to(300))

    anna_chat.command(access.cmd_pause)
    anna_chat.press("For 1 hour")
    assert "paused until" in anna_chat.last
    assert storage.user(ANNA).muted(datetime.now(UTC))
    anna_chat.press("▶ Turn back on")
    assert not storage.user(ANNA).muted(datetime.now(UTC))


def test_mute_until_morning():
    tz = ZoneInfo("Asia/Bangkok")
    evening = datetime(2026, 10, 1, 22, 0, tzinfo=tz)
    until = datetime.fromisoformat(access.mute_until("morning", tz, evening))
    assert until.astimezone(tz) == datetime(2026, 10, 2, 9, 0, tzinfo=tz)
    early = datetime(2026, 10, 2, 6, 0, tzinfo=tz)
    assert datetime.fromisoformat(access.mute_until("morning", tz, early)).astimezone(tz).day == 2
    assert access.mute_until("forever", tz) == "forever"
    one = datetime.fromisoformat(access.mute_until("1h", tz, evening))
    assert one - evening == timedelta(hours=1)


def test_users_admin_rights_team_and_removal(tmp_path):
    admin, storage = make_bot(tmp_path)
    join(admin)
    admin.command(access.cmd_users)
    assert admin.button_texts() == ["👑 Danga", "👤 Anna"]

    admin.press("👑 Danga")
    admin.press("Снять права администратора")
    assert "единственный администратор" in admin.last and storage.user(100).is_admin

    admin.press("⬅ Все люди")
    admin.press("👤 Anna")
    admin.press("👥 Команда")
    admin.press("➕ Новая команда")
    admin.type("Продажи")
    assert "Команда: Продажи" in admin.last and storage.user(ANNA).team == "Продажи"
    admin.press("👑 Сделать администратором")
    assert storage.user(ANNA).is_admin

    admin.press("⬅ Все люди")
    admin.press("👤 Anna · Продажи".replace("👤", "👑"))
    admin.press("🗑 Убрать доступ")
    assert storage.user(ANNA) is None and "closed" in admin.context.bot.to(ANNA)[-1]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    from sheetbot.storage import Storage
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
               "role TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en', added_at TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO users (chat_id, name, role, lang) VALUES (1, 'Danga', 'admin', 'ru')")
    db.commit()
    db.close()
    user = Storage(path).user(1)
    assert user.is_admin and user.team == "" and user.muted_until == ""
