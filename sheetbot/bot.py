"""Telegram side: commands, access check, replies."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from telegram import BotCommand, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import access, analysis_builder, builder, report_builder, watcher
from .common import deps, error_text, known_user, send_safely
from .config import Config
from .i18n import DEFAULT_LANG, next_lang, t
from .sheets import SheetsClient, SheetsError, SpreadsheetInfo
from .storage import Storage

log = logging.getLogger(__name__)

MAX_COLS_SHOWN = 6


def sheets_line(info: SpreadsheetInfo) -> str:
    return ", ".join(f"{s.name} ({s.rows})" for s in info.sheets)


def status_lines(lang: str, info: SpreadsheetInfo) -> str:
    lines = []
    for s in info.sheets:
        if not s.headers:
            lines.append(t(lang, "status_sheet_empty", name=s.name))
            continue
        cols = ", ".join(s.headers[:MAX_COLS_SHOWN])
        if len(s.headers) > MAX_COLS_SHOWN:
            cols += ", …"
        lines.append(t(lang, "status_sheet", name=s.name, rows=s.rows, cols=cols))
    return "\n".join(lines)


async def read_sheet(context: ContextTypes.DEFAULT_TYPE) -> SpreadsheetInfo:
    _, _, sheets = deps(context)
    return await asyncio.to_thread(sheets.read_all)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, storage, _ = deps(context)
    chat = update.effective_chat
    user = storage.user(chat.id)
    payload = context.args[0] if getattr(context, "args", None) else ""
    if user is None and payload.startswith(access.INVITE_PREFIX) and storage.has_admin():
        await access.on_invite_start(update, context, payload[len(access.INVITE_PREFIX):])
        return
    just_claimed = False
    if user is None:
        name = update.effective_user.full_name if update.effective_user else ""
        user = storage.claim_admin(chat.id, name, DEFAULT_LANG)
        if user is None:
            await chat.send_message(t(DEFAULT_LANG, "no_access", chat_id=chat.id))
            return
        just_claimed = True
        log.info("Admin set: %s (%s)", name, chat.id)

    lang = user.lang
    help_text = access.help_text(user)
    try:
        info = await read_sheet(context)
    except SheetsError as err:
        log.warning("Sheet unavailable on /start: %s", err)
        await chat.send_message(
            t(lang, "start_offline", error=error_text(lang, err, config), help=help_text)
        )
        return
    key = "start_admin" if just_claimed else "start_again"
    await chat.send_message(t(lang, key, title=info.title, sheets=sheets_line(info), help=help_text))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await known_user(update, context)
    if not user:
        return
    config, _, _ = deps(context)
    tz = config.timezone.key
    try:
        info = await read_sheet(context)
    except SheetsError as err:
        log.warning("Sheet unavailable on /status: %s", err)
        text = t(user.lang, "status_fail", error=error_text(user.lang, err, config), tz=tz)
    else:
        text = t(user.lang, "status_ok", title=info.title, lines=status_lines(user.lang, info), tz=tz)
    await update.effective_chat.send_message(text + "\n" + watch_lines(user.lang, context))


def watch_lines(lang: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    config, storage, _ = deps(context)
    rules = [*storage.rules(), *storage.reports(), *storage.analyses()]
    paused = sum(r.paused for r in rules)
    last = storage.meta("last_check")
    checked = (t(lang, "status_checked", ago=watcher.ago_text(lang, last), n=config.poll_minutes) if last
               else t(lang, "status_never", n=config.poll_minutes))
    return t(lang, "status_rules", active=len(rules) - paused, paused=paused) + "\n" + checked


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await known_user(update, context)
    if not user:
        return
    _, storage, _ = deps(context)
    lang = next_lang(user.lang)
    storage.set_lang(user.chat_id, lang)
    await update.effective_chat.send_message(t(lang, "lang_set"))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for builder_on_text in (access.on_text, builder.on_text, report_builder.on_text, analysis_builder.on_text):
        if await builder_on_text(update, context):
            return
    user = await known_user(update, context)
    if user:
        await update.effective_chat.send_message(access.help_text(user))


ERROR_ALERT_MINUTES = 30


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Something unexpected: log it in full, and tell the admins (not more than every 30 minutes)."""
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(context.error, NetworkError) and not isinstance(context.error, BadRequest):
        return   # lost connection to Telegram: the library reconnects by itself
    storage: Storage = context.application.bot_data["storage"]
    now = datetime.now(UTC)
    last = storage.meta("last_error_alert")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=ERROR_ALERT_MINUTES):
        return
    storage.set_meta("last_error_alert", now.isoformat(timespec="seconds"))
    name = type(context.error).__name__
    for admin in storage.admins():
        await send_safely(context, admin.chat_id, t(admin.lang, "internal_error", error=name))


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config, storage, _ = deps(context)
    made = await asyncio.to_thread(storage.backup, config.data_dir / "backups")
    if made:
        log.info("Database backed up to %s", made.name)


async def set_commands(app: Application) -> None:
    """The menu next to the message field, in both languages."""
    for lang in ("en", "ru"):
        commands = [
            BotCommand("newrule", t(lang, "cmd_newrule")),
            BotCommand("rules", t(lang, "cmd_rules")),
            BotCommand("report", t(lang, "cmd_report")),
            BotCommand("pause", t(lang, "cmd_pause")),
            BotCommand("invite", t(lang, "cmd_invite")),
            BotCommand("users", t(lang, "cmd_users")),
            BotCommand("status", t(lang, "cmd_status")),
            BotCommand("lang", t(lang, "cmd_lang")),
        ]
        await app.bot.set_my_commands(commands, language_code=None if lang == "en" else lang)
    me = await app.bot.get_me()
    log.info("Bot @%s is running. Press Ctrl+C to stop.", me.username)


def build_app(config: Config, storage: Storage, sheets: SheetsClient) -> Application:
    app = Application.builder().token(config.bot_token).post_init(set_commands).build()
    app.bot_data.update(config=config, storage=storage, sheets=sheets)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("newrule", builder.cmd_newrule))
    app.add_handler(CommandHandler("rules", builder.cmd_rules))
    app.add_handler(CommandHandler("report", report_builder.cmd_report))
    app.add_handler(CommandHandler("invite", access.cmd_invite))
    app.add_handler(CommandHandler("users", access.cmd_users))
    app.add_handler(CommandHandler("pause", access.cmd_pause))
    app.add_handler(CallbackQueryHandler(builder.on_builder_button, pattern=r"^nr:"))
    app.add_handler(CallbackQueryHandler(builder.on_rules_button, pattern=r"^rl:"))
    app.add_handler(CallbackQueryHandler(report_builder.on_button, pattern=r"^rb:"))
    app.add_handler(CallbackQueryHandler(report_builder.on_card_button, pattern=r"^rp:"))
    app.add_handler(CallbackQueryHandler(analysis_builder.on_button, pattern=r"^ab:"))
    app.add_handler(CallbackQueryHandler(analysis_builder.on_card_button, pattern=r"^an:"))
    app.add_handler(CallbackQueryHandler(access.on_request_button, pattern=r"^ac:"))
    app.add_handler(CallbackQueryHandler(access.on_user_team_button, pattern=r"^us:t\d"))
    app.add_handler(CallbackQueryHandler(access.on_users_button, pattern=r"^us:"))
    app.add_handler(CallbackQueryHandler(access.on_pause_button, pattern=r"^pz:"))
    app.add_handler(MessageHandler(filters.ALL, on_message))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(watcher.tick, interval=config.poll_minutes * 60, first=10, name="check")
    app.job_queue.run_repeating(report_builder.schedule_tick, interval=60, first=20, name="reports")
    app.job_queue.run_repeating(analysis_builder.schedule_tick, interval=60, first=30, name="analyses")
    app.job_queue.run_repeating(backup_job, interval=3600, first=60, name="backup")
    return app
