"""The background check: every few minutes, read the sheet and send what's new.

A rule reports a row once, when it starts meeting the condition, and (if the rule
wants it) once more when it's back to normal. Rows already reported are kept in
the database, so a restart doesn't repeat old alerts.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .common import deps, error_text, recipients, send_safely
from .i18n import t
from .rules import Match, Op, Rule, RuleError, Target, check, render
from .sheets import Problem, SheetsError, Table, table_from
from .storage import Storage
from .texts import broken_text, rule_title

log = logging.getLogger(__name__)

MAX_LINES = 20             # rows per message; the rest is "…and N more"
DOWN_AFTER = 3             # failed checks in a row before admins hear the sheet is down
TELEGRAM_LIMIT = 4000      # a little under Telegram's 4096 characters


@dataclass
class RuleOutcome:
    new: list[Match] = field(default_factory=list)   # rows that just started meeting the condition
    resolved: list[str] = field(default_factory=list)  # rows that are back to normal
    reported: set[str] = field(default_factory=set)  # what to remember for next time


def evaluate(rule: Rule, table: Table | None, previous: Table | None, reported: set[str]) -> RuleOutcome:
    """Compare this check with what the rule has already reported. No side effects."""
    matches = check(rule, table, previous)
    if rule.op is Op.CHANGED:
        # every change is news; there's no "still changed" state to remember
        return RuleOutcome(new=matches)
    current = {m.key for m in matches}
    return RuleOutcome(
        new=[m for m in matches if m.key not in reported],
        resolved=sorted(reported - current) if rule.notify_resolved else [],
        reported=current,
    )


def _limit(lines: list[str], lang: str) -> list[str]:
    if len(lines) > MAX_LINES:
        return lines[:MAX_LINES] + [t(lang, "alert_more", n=len(lines) - MAX_LINES)]
    return lines


def _fit(text: str) -> str:
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1] + "…"


def alert_text(lang: str, rule: Rule, outcome: RuleOutcome) -> str:
    lines = [render(rule, m) for m in outcome.new]
    return _fit(t(lang, "alert_head", title=rule_title(lang, rule)) + "\n\n" + "\n".join(_limit(lines, lang)))


def resolved_text(lang: str, rule: Rule, outcome: RuleOutcome) -> str:
    lines = [t(lang, "resolved_line", name=key) for key in outcome.resolved]
    return _fit(t(lang, "resolved_head", title=rule_title(lang, rule)) + "\n\n" + "\n".join(_limit(lines, lang)))


def suggestion(err: RuleError, rule: Rule, tables: dict[str, Table]) -> str:
    """A likely new name for a renamed column or sheet: «Остаток» → «Остаток, шт»."""
    if err.kind == "sheet":
        pool = list(tables)
    else:
        table = tables.get(rule.sheet)
        pool = table.headers if table else []
    close = difflib.get_close_matches(err.missing, pool, n=1, cutoff=0.5)
    if close:
        return close[0]
    # «Остаток» inside «Остаток, шт» is a strong hint even when the strings differ a lot
    inside = [p for p in pool if err.missing.casefold() in p.casefold()]
    return inside[0] if len(inside) == 1 else ""


def apply_fix(rule: Rule) -> str:
    """Point a broken rule at the suggested column or sheet. Returns the new name."""
    info = rule.extra.pop("broken", None) or {}
    old, new = info.get("missing", ""), info.get("suggest", "")
    if not new:
        return ""
    if info.get("kind") == "sheet":
        rule.sheet = new
    else:
        if rule.column == old:
            rule.column = new
        if rule.target is Target.COLUMN and rule.target_value == old:
            rule.target_value = new
        if rule.label == old:
            rule.label = new
        rule.template = rule.template.replace("{" + old + "}", "{" + new + "}")
    rule.paused = False
    return new


async def _tell_admins(context: ContextTypes.DEFAULT_TYPE, storage: Storage, make_message) -> None:
    """make_message(lang) -> (text, buttons or None), so each admin reads their own language."""
    for user in storage.users():
        if user.is_admin:
            text, buttons = make_message(user.lang)
            await send_safely(context, user.chat_id, text, buttons)


async def _rule_broke(context, storage: Storage, rule: Rule, err: RuleError, tables: dict[str, Table]) -> None:
    suggest = suggestion(err, rule, tables)
    rule.paused = True
    rule.extra["broken"] = {"missing": err.missing, "kind": err.kind, "suggest": suggest}
    storage.save_rule(rule)
    log.warning("Rule %s paused: %s «%s» is missing", rule.id, err.kind, err.missing)

    def message(lang: str):
        text = t(lang, "broken_paused", title=rule_title(lang, rule), reason=broken_text(lang, rule, err))
        rows = []
        if suggest:
            text += "\n" + t(lang, "broken_suggest", name=suggest)
            rows.append([InlineKeyboardButton(t(lang, "btn_use", name=suggest), callback_data=f"rl:fix:{rule.id}")])
        rows.append([InlineKeyboardButton(t(lang, "btn_edit"), callback_data=f"rl:edit:{rule.id}")])
        return text, InlineKeyboardMarkup(rows)

    await _tell_admins(context, storage, message)


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One check of the whole spreadsheet against every active rule.

    The scheduled check and "check right after saving" can overlap; the lock makes
    the second one wait, so a row is never reported twice.
    """
    lock = context.application.bot_data.setdefault("check_lock", asyncio.Lock())
    async with lock:
        await _check_everything(context)


async def _check_everything(context: ContextTypes.DEFAULT_TYPE) -> None:
    config, storage, sheets = deps(context)
    try:
        _, grids = await asyncio.to_thread(sheets.read_grids, 0)
    except SheetsError as err:
        # one failed check is usually a blip; admins hear about it once it's clearly not
        streak = int(storage.meta("fail_streak", "0")) + 1
        storage.set_meta("fail_streak", str(streak))
        log.warning("Check skipped (%d in a row), spreadsheet unreachable: %s", streak, err.problem.value)
        if storage.meta("sheet_down") != "1" and (streak >= DOWN_AFTER or err.problem is not Problem.NETWORK):
            storage.set_meta("sheet_down", "1")
            problem = err
            await _tell_admins(context, storage,
                               lambda lang: (t(lang, "sheet_down", error=error_text(lang, problem, config)), None))
        return

    storage.set_meta("fail_streak", "0")
    if storage.meta("sheet_down") == "1":
        storage.set_meta("sheet_down", "0")
        await _tell_admins(context, storage, lambda lang: (t(lang, "sheet_back"), None))
    storage.set_meta("last_check", datetime.now(UTC).isoformat(timespec="seconds"))

    tables = {name: table_from(name, values, getattr(sheets, "decimal", None)) for name, values in grids.items()}
    all_rules = storage.rules()
    watched_for_changes = {r.sheet for r in all_rules if r.op is Op.CHANGED}

    for rule in all_rules:
        if rule.paused:
            continue
        snap = storage.snapshot(rule.sheet) if rule.op is Op.CHANGED else None
        previous = table_from(rule.sheet, snap) if snap is not None else None
        try:
            outcome = evaluate(rule, tables.get(rule.sheet), previous, storage.hits(rule.id))
        except RuleError as err:
            await _rule_broke(context, storage, rule, err, tables)
            continue
        except Exception:
            # one odd rule must not stop the others
            log.exception("Rule %s failed", rule.id)
            continue
        if rule.op is not Op.CHANGED:
            storage.set_hits(rule.id, outcome.reported)
        for user in recipients(storage, rule):
            if outcome.new:
                await send_safely(context, user.chat_id, alert_text(user.lang, rule, outcome))
            if outcome.resolved:
                await send_safely(context, user.chat_id, resolved_text(user.lang, rule, outcome))
        if outcome.new or outcome.resolved:
            log.info("Rule %s: %d new, %d resolved", rule.id, len(outcome.new), len(outcome.resolved))

    for name in watched_for_changes:
        if name in grids:
            storage.save_snapshot(name, grids[name])


def check_soon(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a check in a couple of seconds, e.g. right after a rule is saved."""
    queue = getattr(context, "job_queue", None)
    if queue is not None:
        queue.run_once(tick, 2)


def ago_text(lang: str, iso: str) -> str:
    if not iso:
        return ""
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 1:
        return t(lang, "ago_now")
    if minutes < 60:
        return t(lang, "ago_min", n=minutes)
    return t(lang, "ago_hours", n=minutes // 60)
