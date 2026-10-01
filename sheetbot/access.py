"""Who can use the bot: invite links, approval, teams, admins, and /pause.

The first person to press /start is the admin. Everyone else needs a one-time
invite link from an admin, and the admin approves each request with one tap,
choosing the person's team on the way. Only admins change rules and people;
members receive messages, ask for reports and can pause their notifications.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, time, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from .builder import edit, keyboard, paged
from .common import admin_user, deps, known_user, send_safely
from .i18n import DEFAULT_LANG, t
from .storage import ADMIN, MEMBER, Storage, User

log = logging.getLogger(__name__)

INVITE_PREFIX = "inv_"
MORNING = time(9, 0)


def help_text(user: User) -> str:
    return t(user.lang, "help" if user.is_admin else "help_member")


# ---------------------------------------------------------------- invites

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin = await admin_user(update, context)
    if not admin:
        return
    _, storage, _ = deps(context)
    token = secrets.token_urlsafe(9)
    storage.create_invite(token, admin.chat_id)
    link = f"https://t.me/{context.bot.username}?start={INVITE_PREFIX}{token}"
    await update.effective_chat.send_message(t(admin.lang, "invite_link", link=link))


async def on_invite_start(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    """Someone opened an invite link: hold them until an admin decides."""
    _, storage, _ = deps(context)
    chat = update.effective_chat
    if storage.user(chat.id):
        await chat.send_message(t(DEFAULT_LANG, "invite_already"))
        return
    inv = storage.invite(token)
    if inv is None or inv.status != "open" or not storage.invite_fresh(inv):
        await chat.send_message(t(DEFAULT_LANG, "invite_bad"))
        return
    name = update.effective_user.full_name if update.effective_user else str(chat.id)
    storage.set_invite(token, "pending", chat.id, name)
    await chat.send_message(t(DEFAULT_LANG, "invite_sent"))
    log.info("Access requested by %s (%s)", name, chat.id)
    for admin in storage.admins():
        await send_safely(context, admin.chat_id, t(admin.lang, "invite_request", name=name),
                          team_keyboard(admin.lang, storage.teams(), f"ac:{token}"))


def team_keyboard(lang: str, teams: list[str], prefix: str, reject: bool = True):
    rows = [[(t(lang, "btn_no_team"), f"{prefix}:t:-")]]
    buttons = [("👥 " + team, f"{prefix}:t:{i}") for i, team in enumerate(teams)]
    rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([(t(lang, "btn_new_team"), f"{prefix}:new")])
    if reject:
        rows.append([(t(lang, "btn_reject"), f"{prefix}:no")])
    return keyboard(rows)


async def on_request_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ac:<token>:t:<team index or ->  ·  ac:<token>:new  ·  ac:<token>:no"""
    query = update.callback_query
    await query.answer()
    admin = await admin_user(update, context)
    if not admin:
        return
    _, storage, _ = deps(context)
    _, token, action, *rest = query.data.split(":")
    inv = storage.invite(token)
    if inv is None or inv.status != "pending":
        await edit(query, t(admin.lang, "invite_handled"))
        return
    if action == "no":
        storage.set_invite(token, "done")
        await edit(query, t(admin.lang, "invite_rejected_admin", name=inv.name))
        await send_safely(context, inv.chat_id, t(DEFAULT_LANG, "invite_denied"))
        return
    if action == "new":
        context.user_data["access_await"] = {"kind": "invite", "token": token}
        await edit(query, t(admin.lang, "type_team"))
        return
    teams = storage.teams()
    team = "" if rest[0] == "-" else teams[int(rest[0])]
    await approve(context, storage, inv, team)
    await edit(query, approved_text(admin.lang, inv.name, team))


def approved_text(lang: str, name: str, team: str) -> str:
    part = t(lang, "in_team", team=team) if team else ""
    return t(lang, "invite_approved_admin", name=name, team=part)


async def approve(context, storage: Storage, inv, team: str) -> None:
    storage.add_user(inv.chat_id, inv.name, MEMBER, team, DEFAULT_LANG)
    storage.set_invite(inv.token, "done")
    user = storage.user(inv.chat_id)
    log.info("Access granted to %s (%s), team «%s»", inv.name, inv.chat_id, team)
    await send_safely(context, inv.chat_id, t(user.lang, "invite_welcome", help=help_text(user)))


# ---------------------------------------------------------------- /users

def users_view(lang: str, storage: Storage, page: int = 0):
    users = storage.users()
    buttons = []
    for u in users:
        label = ("👑 " if u.is_admin else "👤 ") + (u.name or str(u.chat_id))
        if u.team:
            label += f" · {u.team}"
        buttons.append((label, f"us:open:{u.chat_id}"))
    return t(lang, "users_title", n=len(users)), keyboard(paged(buttons, page, "us:list"))


def user_card(lang: str, u: User, tz):
    if not u.muted_until:
        muted = t(lang, "muted_no")
    elif u.muted_until == "forever":
        muted = t(lang, "muted_forever")
    else:
        muted = t(lang, "muted_until", time=local_time(u.muted_until, tz))
    role = t(lang, "role_admin" if u.is_admin else "role_member")
    text = t(lang, "user_card", icon="👑" if u.is_admin else "👤", name=u.name or u.chat_id,
             role=role, team=u.team or t(lang, "team_none"), muted=muted)
    admin_btn = ("btn_unadmin", "demote") if u.is_admin else ("btn_make_admin", "promote")
    return text, keyboard([
        [(t(lang, admin_btn[0]), f"us:{admin_btn[1]}:{u.chat_id}")],
        [(t(lang, "btn_team"), f"us:team:{u.chat_id}"), (t(lang, "btn_remove_user"), f"us:remove:{u.chat_id}")],
        [(t(lang, "btn_all_users"), "us:list:0")],
    ])


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin = await admin_user(update, context)
    if not admin:
        return
    _, storage, _ = deps(context)
    text, markup = users_view(admin.lang, storage)
    await update.effective_chat.send_message(text, reply_markup=markup)


async def on_users_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """us:<action>:<chat_id>[:<team index or ->]"""
    query = update.callback_query
    await query.answer()
    admin = await admin_user(update, context)
    if not admin:
        return
    config, storage, _ = deps(context)
    lang = admin.lang
    _, action, target, *_ = query.data.split(":")
    if action == "list":
        text, markup = users_view(lang, storage, int(target or 0))
        await edit(query, text, markup)
        return
    u = storage.user(int(target))
    if u is None:
        text, markup = users_view(lang, storage)
        await edit(query, text, markup)
        return

    last_admin = u.is_admin and len(storage.admins()) == 1
    if action in ("demote", "remove") and last_admin:
        text, markup = user_card(lang, u, config.timezone)
        await edit(query, t(lang, "last_admin") + "\n\n" + text, markup)
        return
    if action == "promote":
        storage.update_user(u.chat_id, role=ADMIN)
    elif action == "demote":
        storage.update_user(u.chat_id, role=MEMBER)
    elif action == "remove":
        storage.remove_user(u.chat_id)
        await send_safely(context, u.chat_id, t(u.lang, "access_removed"))
        log.info("Access removed for %s (%s)", u.name, u.chat_id)
        text, markup = users_view(lang, storage)
        await edit(query, t(lang, "user_removed") + "\n\n" + text, markup)
        return
    elif action == "team":
        await edit(query, t(lang, "user_team_pick", name=u.name),
                   team_keyboard(lang, storage.teams(), f"us:t{u.chat_id}", reject=False))
        return
    text, markup = user_card(lang, storage.user(u.chat_id), config.timezone)
    await edit(query, text, markup)


async def on_user_team_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """us:t<chat_id>:t:<team index or ->  ·  us:t<chat_id>:new — from the team picker above."""
    query = update.callback_query
    await query.answer()
    admin = await admin_user(update, context)
    if not admin:
        return
    config, storage, _ = deps(context)
    _, who, action, *rest = query.data.split(":")
    chat_id = int(who[1:])
    if action == "new":
        context.user_data["access_await"] = {"kind": "user", "chat_id": chat_id}
        await edit(query, t(admin.lang, "type_team"))
        return
    teams = storage.teams()
    storage.update_user(chat_id, team="" if rest[0] == "-" else teams[int(rest[0])])
    text, markup = user_card(admin.lang, storage.user(chat_id), config.timezone)
    await edit(query, text, markup)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """A new team name typed by an admin."""
    waiting = context.user_data.get("access_await")
    if not waiting:
        return False
    admin = await admin_user(update, context)
    if not admin:
        context.user_data.pop("access_await", None)
        return True
    config, storage, _ = deps(context)
    team = " ".join((update.effective_message.text or "").split())[:40]
    if not team:
        await update.effective_chat.send_message(t(admin.lang, "type_team"))
        return True
    context.user_data.pop("access_await", None)
    if waiting["kind"] == "invite":
        inv = storage.invite(waiting["token"])
        if inv is None or inv.status != "pending":
            await update.effective_chat.send_message(t(admin.lang, "invite_handled"))
            return True
        await approve(context, storage, inv, team)
        await update.effective_chat.send_message(approved_text(admin.lang, inv.name, team))
    else:
        storage.update_user(waiting["chat_id"], team=team)
        text, markup = user_card(admin.lang, storage.user(waiting["chat_id"]), config.timezone)
        await update.effective_chat.send_message(text, reply_markup=markup)
    return True


# ---------------------------------------------------------------- /pause

def local_time(iso: str, tz) -> str:
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%d.%m %H:%M")


def pause_view(lang: str, u: User, tz):
    now = datetime.now(timezone.utc)
    if u.muted(now):
        text = (t(lang, "paused_forever") if u.muted_until == "forever"
                else t(lang, "paused_until", time=local_time(u.muted_until, tz)))
        return text, keyboard([[(t(lang, "btn_unpause"), "pz:off")]])
    return t(lang, "pause_menu"), keyboard([
        [(t(lang, "btn_1h"), "pz:1h"), (t(lang, "btn_morning"), "pz:morning")],
        [(t(lang, "btn_forever"), "pz:forever")],
    ])


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await known_user(update, context)
    if not user:
        return
    config, _, _ = deps(context)
    text, markup = pause_view(user.lang, user, config.timezone)
    await update.effective_chat.send_message(text, reply_markup=markup)


def mute_until(choice: str, tz, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if choice == "forever":
        return "forever"
    if choice == "1h":
        return (now + timedelta(hours=1)).isoformat(timespec="seconds")
    local = now.astimezone(tz)
    morning = datetime.combine(local.date(), MORNING, tzinfo=tz)
    if morning <= local:
        morning += timedelta(days=1)
    return morning.astimezone(timezone.utc).isoformat(timespec="seconds")


async def on_pause_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await known_user(update, context)
    if not user:
        return
    config, storage, _ = deps(context)
    choice = query.data.split(":")[1]
    if choice == "off":
        storage.update_user(user.chat_id, muted_until="")
        await edit(query, t(user.lang, "unpaused"))
        return
    storage.update_user(user.chat_id, muted_until=mute_until(choice, config.timezone))
    text, markup = pause_view(user.lang, storage.user(user.chat_id), config.timezone)
    await edit(query, text, markup)
