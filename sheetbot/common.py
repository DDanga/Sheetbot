"""Small helpers shared by the command handlers and the rule builder."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from .config import Config
from .i18n import DEFAULT_LANG, t
from .rules import Recipients
from .sheets import Problem, SheetsClient, SheetsError, service_account_email
from .storage import Storage, User

log = logging.getLogger(__name__)
DRAFT_KEYS = ("draft", "rdraft", "adraft")
TELEGRAM_LIMIT = 4096   # characters in one message


def fit(text: str) -> str:
    """Cut a message Telegram would refuse; a huge cell must not swallow the whole alert."""
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1] + "…"


def deps(context: ContextTypes.DEFAULT_TYPE) -> tuple[Config, Storage, SheetsClient]:
    d = context.application.bot_data
    return d["config"], d["storage"], d["sheets"]


async def known_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    """The user if they have access; otherwise tells them so and returns None."""
    _, storage, _ = deps(context)
    chat = update.effective_chat
    user = storage.user(chat.id)
    if user is None:
        await chat.send_message(t(DEFAULT_LANG, "no_access", chat_id=chat.id))
    return user


async def admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    """The user if they're an admin; members are told it's for admins only."""
    user = await known_user(update, context)
    if user and not user.is_admin:
        await update.effective_chat.send_message(t(user.lang, "admin_only"))
        return None
    return user


def error_text(lang: str, err: SheetsError, config: Config) -> str:
    if err.problem is Problem.BAD_KEY:
        return t(lang, "err_bad_key")
    if err.problem is Problem.NOT_SHARED:
        return t(lang, "err_not_shared", email=service_account_email(config.key_file) or "?")
    if err.problem is Problem.NOT_FOUND:
        return t(lang, "err_not_found")
    return t(lang, "err_network")


async def send_safely(context, chat_id: int, text: str, markup=None) -> bool:
    """Send without letting one blocked or deleted chat stop the rest of the messages.

    Telegram's flood control ("retry after N seconds") and network blips get one
    more try; a blocked bot or a deleted chat doesn't.
    """
    for attempt in (1, 2):
        try:
            await context.bot.send_message(chat_id, fit(text), reply_markup=markup)
            return True
        except RetryAfter as e:
            wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else e.retry_after
            log.info("Telegram asks to slow down, waiting %s s", wait)
            await asyncio.sleep(min(float(wait), 60))
        except BadRequest as e:          # a broken message or chat: retrying won't help
            log.warning("Couldn't message %s: %s", chat_id, e)
            return False
        except NetworkError as e:        # timeouts and lost connections
            if attempt == 2:
                log.warning("Couldn't message %s: %s", chat_id, e)
                return False
            await asyncio.sleep(2)
        except TelegramError as e:       # blocked by the user, chat deleted, …
            log.warning("Couldn't message %s: %s", chat_id, e)
            return False
    log.warning("Couldn't message %s after a retry", chat_id)
    return False


def clear_drafts(context) -> None:
    """Only one builder at a time, so typed text always goes to the one on screen."""
    for key in DRAFT_KEYS:
        context.user_data.pop(key, None)


def recipients(storage: Storage, item) -> list[User]:
    """Who gets a rule's, report's or analysis's messages right now (muted people are skipped)."""
    if item.recipients is Recipients.ALL:
        users = storage.users()
    elif item.recipients is Recipients.TEAM:
        users = [u for u in storage.users() if u.team == item.team]
    else:
        owner = storage.user(item.owner)
        users = [owner] if owner else []
    now = datetime.now(timezone.utc)
    return [u for u in users if not u.muted(now)]


def to_rows(lang: str, prefix: str, teams: list[str]) -> list[list[tuple[str, str]]]:
    """The "who gets it" buttons: me, everyone, and one per team."""
    rows = [[(t(lang, "to_me"), f"{prefix}:to:me"), (t(lang, "to_all"), f"{prefix}:to:all")]]
    team_buttons = [("👥 " + team, f"{prefix}:team:{i}") for i, team in enumerate(teams)]
    rows += [team_buttons[i:i + 2] for i in range(0, len(team_buttons), 2)]
    return rows
