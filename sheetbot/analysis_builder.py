"""The analysis builder, its card in /rules, and its schedule.

Two kinds: compare periods ("sales fell 46% week to week") and top items with
"hidden stars" (sells little, earns the most). Same button style as the other
builders; the preview runs the real analysis on the live sheet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .analysis import Analysis, Kind, Profit, Unit, When, compare, top, worth_sending
from .builder import MENU_NUMBERS, edit, grid, is_stale, keyboard, load_tables, rules_list_view, stamp, unstamp
from .common import admin_user, clear_drafts, deps, error_text, fit, known_user, recipients, send_safely, to_rows
from .i18n import t
from .report_builder import TIMES, broken_text, local_now
from .reports import (
    Freq,
    Func,
    Metric,
    Period,
    ReportError,
    date_columns,
    last_due,
    number_columns,
    text_columns,
    valid_time,
)
from .rules import Recipients, parse_number
from .sheets import SheetsError, Table, table_from
from .storage import User
from .texts import analysis_title, compare_message, recipients_text, schedule_text, top_message, when_text

log = logging.getLogger(__name__)

PCTS = (10, 20, 30)
TOP_SIZES = (3, 5, 10)
CATCH_UP_HOURS = 12


@dataclass
class AnalysisDraft:
    an: Analysis
    step: str = "kind"
    history: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    awaiting: str | None = None
    editing: bool = False
    version: int = 0                      # see builder.stamp()
    paged: tuple[str, int] = ("", 0)      # (step, page) of a long picker

    def go(self, step: str) -> None:
        if self.step != step:
            self.history.append(self.step)
        self.step = step

    @property
    def page(self) -> int:
        return self.paged[1] if self.paged[0] == self.step else 0


def message(lang: str, an: Analysis, table: Table | None, today) -> tuple[str, bool]:
    """The analysis text and whether a scheduled run would send it."""
    if an.kind is Kind.COMPARE:
        res = compare(an, table, today)
        return compare_message(lang, an, res), worth_sending(an, res)
    return top_message(lang, an, top(an, table, today)), True


# ---------------------------------------------------------------- screens

async def screen(lang: str, d: AnalysisDraft, context) -> tuple[str, InlineKeyboardMarkup]:
    an = d.an
    back = [(t(lang, "btn_back"), "ab:back")]
    cancel = ([(t(lang, "btn_cancel"), "ab:cancel")] if an.id is None
              else [(t(lang, "btn_back_rules"), "ab:torules")])
    nav = [back + cancel] if d.history else [cancel]

    if d.step == "kind":
        return t(lang, "ab_kind"), keyboard([
            [(t(lang, "ak_compare"), "ab:kind:compare"), (t(lang, "ak_top"), "ab:kind:top")]] + nav)

    tables = await load_tables(context)
    table = tables.get(an.sheet)
    numbers = number_columns(table) if table else []

    def pick(options: list[str], per_row: int = 3, extra: list | None = None):
        d.options = options
        return keyboard((extra or []) + grid(options, "ab:pick", per_row, d.page) + nav)

    if d.step == "sheet":
        d.options = [name for name, tb in tables.items() if date_columns(tb)]
        if not d.options:
            return t(lang, "rb_no_dates"), keyboard(nav)
        return t(lang, "rb_sheet"), keyboard(grid(d.options, "ab:pick", 2, d.page) + nav)
    if d.step == "date":
        return t(lang, "rb_date"), pick(date_columns(table) if table else [])
    if d.step == "unit":
        return t(lang, "ab_unit"), keyboard(
            [[(t(lang, f"unit_{u.value}").capitalize(), f"ab:unit:{u.value}")] for u in Unit] + nav)
    if d.step == "func":
        funcs = [Func.SUM, Func.AVG, Func.MAX, Func.MIN, Func.COUNT]
        return t(lang, "rb_func"), keyboard(
            [[(t(lang, f"f_{f.value}"), f"ab:func:{f.value}") for f in funcs[:2]],
             [(t(lang, f"f_{f.value}"), f"ab:func:{f.value}") for f in funcs[2:4]],
             [(t(lang, "f_count"), "ab:func:count")]] + nav)
    if d.step == "column":
        return t(lang, "rb_column", func=t(lang, f"f_{an.metric.func.value}")), pick(numbers)
    if d.step == "when":
        return t(lang, "ab_when"), keyboard(
            [[(t(lang, f"w_{w.value}"), f"ab:when:{w.value}")] for w in When] + nav)
    if d.step == "pct":
        return t(lang, "ab_pct"), keyboard(
            [[(f"{p}%", f"ab:pct:{p}") for p in PCTS] + [(t(lang, "btn_own_pct"), "ab:ownpct")]] + nav)
    if d.step == "typepct":
        d.awaiting = "pct"
        return t(lang, "ab_type_pct"), keyboard(nav)
    if d.step == "item":
        return t(lang, "ab_item"), pick([c for c in (text_columns(table) if table else []) if c != an.date_column])
    if d.step == "qty":
        return t(lang, "ab_qty"), pick(numbers, extra=[[(t(lang, "q_rows"), "ab:qrows")]])
    if d.step == "profit":
        return t(lang, "ab_profit"), keyboard(
            [[(t(lang, f"p_{p.value}"), f"ab:profit:{p.value}")] for p in Profit] + nav)
    if d.step == "revenue":
        return t(lang, "ab_revenue"), pick(numbers)
    if d.step == "cost":
        return t(lang, "ab_cost"), pick([c for c in numbers if c != an.revenue])
    if d.step == "profit_col":
        return t(lang, "ab_profit_col"), pick(numbers)
    if d.step == "period":
        periods = [Period.LAST_7, Period.THIS_MONTH, Period.LAST_MONTH]
        return t(lang, "ab_top_period"), keyboard(
            [[(t(lang, f"period_{p.value}"), f"ab:period:{p.value}")] for p in periods] + nav)
    if d.step == "n":
        return t(lang, "ab_n"), keyboard([[(str(n), f"ab:n:{n}") for n in TOP_SIZES]] + nav)
    if d.step == "freq":
        return t(lang, "rb_freq"), keyboard(
            [[(t(lang, f"freq_{f.value}"), f"ab:freq:{f.value}")] for f in Freq] + nav)
    if d.step == "weekday":
        return t(lang, "rb_weekday"), keyboard([[(t(lang, f"wds_{i}"), f"ab:wd:{i}") for i in range(7)]] + nav)
    if d.step == "time":
        config, _, _ = deps(context)
        return t(lang, "rb_time", tz=config.timezone.key), keyboard(
            [[(x, f"ab:time:{x}") for x in TIMES], [(t(lang, "btn_own_time"), "ab:owntime")]] + nav)
    if d.step == "typetime":
        d.awaiting = "time"
        return t(lang, "rb_type_time"), keyboard(nav)
    if d.step == "to":
        _, storage, _ = deps(context)
        d.options = storage.teams()
        return t(lang, "nr_to"), keyboard(to_rows(lang, "ab", d.options) + nav)

    # preview
    d.history = []
    return preview(lang, an, table, local_now(context).date()), keyboard([
        [(t(lang, "btn_save"), "ab:save")],
        [(t(lang, "btn_settings"), "ab:editsettings"), (t(lang, "btn_schedule"), "ab:editschedule")],
        [(t(lang, "btn_to"), "ab:editto")],
        cancel,
    ])


def preview(lang: str, an: Analysis, table: Table | None, today) -> str:
    try:
        sample, worth = message(lang, an, table, today)
        if not worth:
            sample += "\n\n" + t(lang, "an_quiet")
    except ReportError as err:
        sample = broken_text(lang, an, err)
    return t(lang, "ab_preview", title=analysis_title(lang, an), when=when_text(lang, an),
             schedule=schedule_text(lang, an), to=recipients_text(lang, an), sample=sample)


async def show(update: Update, context, user: User, d: AnalysisDraft, new_message: bool = False) -> None:
    try:
        text, markup = await screen(user.lang, d, context)
    except SheetsError as err:
        config, _, _ = deps(context)
        text, markup = error_text(user.lang, err, config), None
    d.version = next(MENU_NUMBERS)
    markup = stamp(markup, "ab", d.version)
    if update.callback_query and not new_message:
        await edit(update.callback_query, text, markup)
    else:
        await update.effective_chat.send_message(fit(text), reply_markup=markup)


# ---------------------------------------------------------------- building

def _first_setting(an: Analysis) -> str:
    return "unit" if an.kind is Kind.COMPARE else "item"


async def start(update: Update, context, user: User) -> None:
    """Called from /newrule → 📈 Analysis."""
    clear_drafts(context)
    d = AnalysisDraft(an=Analysis(Kind.COMPARE, sheet="", date_column="", owner=user.chat_id))
    context.user_data["adraft"] = d
    await show(update, context, user, d)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = await admin_user(update, context)
    if not user:
        await query.answer()
        return
    d: AnalysisDraft | None = context.user_data.get("adraft")
    if is_stale(query.data, d):
        await query.answer(t(user.lang, "stale_tap"))
        return
    await query.answer()
    _, action, *rest = unstamp(query.data)[0].split(":", 2)
    arg = rest[0] if rest else ""
    an = d.an
    d.awaiting = None
    after_settings = "preview" if d.editing else "freq"
    after_time = "preview" if d.editing else "to"

    if action == "page":
        d.paged = (d.step, int(arg))
    elif action == "kind":
        an.kind = Kind(arg)
        d.go("sheet")
    elif action == "pick":
        choice = d.options[int(arg)]
        if d.step == "sheet":
            an.sheet = choice
            table = (await load_tables(context)).get(choice)
            dates = date_columns(table) if table else []
            if len(dates) == 1:
                an.date_column = dates[0]
                d.go(_first_setting(an))
            else:
                d.go("date")
        elif d.step == "date":
            an.date_column = choice
            d.go(_first_setting(an))
        elif d.step == "column":
            an.metric = Metric(an.metric.func, choice)
            d.go("when")
        elif d.step == "item":
            an.item = choice
            d.go("qty")
        elif d.step == "qty":
            an.qty = choice
            d.go("profit")
        elif d.step == "revenue":
            an.revenue = choice
            d.go("cost")
        elif d.step == "cost":
            an.cost = choice
            d.go("period")
        elif d.step == "profit_col":
            an.profit_col = choice
            d.go("period")
    elif action == "unit":
        an.unit = Unit(arg)
        d.go("func")
    elif action == "func":
        an.metric = Metric(Func(arg))
        d.go("when" if an.metric.func is Func.COUNT else "column")
    elif action == "when":
        an.when = When(arg)
        d.go(after_settings if an.when is When.ALWAYS else "pct")
    elif action == "pct":
        an.pct = float(arg)
        d.go(after_settings)
    elif action == "ownpct":
        d.go("typepct")
    elif action == "qrows":
        an.qty = ""
        d.go("profit")
    elif action == "profit":
        an.profit = Profit(arg)
        d.go({Profit.DIFF: "revenue", Profit.COLUMN: "profit_col", Profit.NONE: "period"}[an.profit])
    elif action == "period":
        an.period = Period(arg)
        d.go("n")
    elif action == "n":
        an.top_n = int(arg)
        d.go(after_settings)
    elif action == "freq":
        an.freq = Freq(arg)
        d.go("weekday" if an.freq is Freq.WEEKLY else "time")
    elif action == "wd":
        an.weekday = int(arg)
        d.go("time")
    elif action == "time":
        an.at = arg
        d.go(after_time)
    elif action == "owntime":
        d.go("typetime")
    elif action == "to":
        an.recipients, an.team = Recipients(arg), ""
        d.go("preview")
    elif action == "team":
        an.recipients, an.team = Recipients.TEAM, d.options[int(arg)]
        d.go("preview")
    elif action == "editsettings":
        d.editing, d.history, d.step = True, ["preview"], _first_setting(an)
    elif action == "editschedule":
        d.editing, d.history, d.step = True, ["preview"], "freq"
    elif action == "editto":
        d.history, d.step = ["preview"], "to"
    elif action == "back":
        d.step = d.history.pop() if d.history else "kind"
    elif action == "save":
        _, storage, _ = deps(context)
        if an.extra.pop("broken", None):
            an.paused = False
        due = last_due(an, local_now(context))
        an.extra["last_sent"] = due.isoformat() if due else ""
        storage.save_analysis(an)
        context.user_data.pop("adraft", None)
        await edit(query, t(user.lang, "an_saved", title=analysis_title(user.lang, an),
                            schedule=schedule_text(user.lang, an)))
        log.info("Analysis %s saved", an.id)
        return
    elif action == "cancel":
        context.user_data.pop("adraft", None)
        await edit(query, t(user.lang, "nr_cancelled"))
        return
    elif action == "torules":
        context.user_data.pop("adraft", None)
        _, storage, _ = deps(context)
        text, markup = rules_list_view(user.lang, storage)
        await edit(query, text, markup)
        return
    await show(update, context, user, d)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    d: AnalysisDraft | None = context.user_data.get("adraft")
    if not d or not d.awaiting:
        return False
    user = await admin_user(update, context)
    if not user:
        return True
    text = update.effective_message.text or ""
    if d.awaiting == "time":
        at = valid_time(text)
        if not at:
            await update.effective_chat.send_message(t(user.lang, "rb_bad_time"))
            return True
        d.an.at = at
        d.go("preview" if d.editing else "to")
    else:
        pct = parse_number(text)
        if pct is None or not 0 < pct <= 1000:
            await update.effective_chat.send_message(t(user.lang, "ab_bad_pct"))
            return True
        d.an.pct = pct
        d.go("preview" if d.editing else "freq")
    d.awaiting = None
    await show(update, context, user, d, new_message=True)
    return True


# ---------------------------------------------------------------- card in /rules

def card_view(lang: str, an: Analysis) -> tuple[str, InlineKeyboardMarkup]:
    if an.extra.get("broken"):
        status = t(lang, "rule_broken_status")
    else:
        status = t(lang, "rule_off" if an.paused else "rule_on")
    pause = ("btn_resume", "resume") if an.paused else ("btn_pause", "pause")
    return (
        t(lang, "an_card", title=analysis_title(lang, an), when=when_text(lang, an),
          schedule=schedule_text(lang, an), to=recipients_text(lang, an), status=status),
        keyboard([
            [(t(lang, "btn_edit"), f"an:edit:{an.id}"), (t(lang, pause[0]), f"an:{pause[1]}:{an.id}")],
            [(t(lang, "btn_send_now"), f"an:now:{an.id}"), (t(lang, "btn_delete"), f"an:del:{an.id}")],
            [(t(lang, "btn_all_rules"), "rl:list")],
        ]),
    )


async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    send_now_only = query.data.split(":")[1] == "now"
    user = await (known_user if send_now_only else admin_user)(update, context)
    await query.answer()
    if not user:
        return
    _, storage, _ = deps(context)
    _, action, aid = query.data.split(":")
    lang = user.lang
    an = storage.analysis(int(aid))
    if an is None:
        text, markup = rules_list_view(lang, storage)
        await edit(query, text, markup)
        return

    if action in ("pause", "resume"):
        an.paused = action == "pause"
        an.extra.pop("broken", None)
        storage.save_analysis(an)
    elif action == "del":
        await edit(query, t(lang, "rule_delete_q", title=analysis_title(lang, an)), keyboard(
            [[(t(lang, "btn_delete_yes"), f"an:delok:{an.id}"), (t(lang, "btn_cancel"), f"an:open:{an.id}")]]))
        return
    elif action == "delok":
        storage.delete_rule(an.id)
        text, markup = rules_list_view(lang, storage)
        await edit(query, t(lang, "rule_deleted") + "\n\n" + text, markup)
        return
    elif action == "now":
        await send_now(update, context, user, an)
        return
    elif action == "edit":
        d = AnalysisDraft(an=an, step="preview", editing=True)
        clear_drafts(context)
        context.user_data["adraft"] = d
        await show(update, context, user, d)
        return
    text, markup = card_view(lang, an)
    await edit(query, text, markup)


async def send_now(update: Update, context, user: User, an: Analysis) -> None:
    """Manual run: always answers, even when a scheduled run would stay quiet."""
    config, _, _ = deps(context)
    try:
        tables = await load_tables(context)
        text, _ = message(user.lang, an, tables.get(an.sheet), local_now(context).date())
    except SheetsError as err:
        text = error_text(user.lang, err, config)
    except ReportError as err:
        text = broken_text(user.lang, an, err)
    await update.effective_chat.send_message(text)


# ---------------------------------------------------------------- schedule

async def schedule_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every minute: run the analyses whose time has come."""
    _, storage, sheets = deps(context)
    now = local_now(context)
    due_items = []
    for an in storage.analyses():
        if an.paused:
            continue
        due = last_due(an, now)
        sent = an.extra.get("last_sent", "")
        if due is None or (sent and datetime.fromisoformat(sent) >= due):
            continue
        an.extra["last_sent"] = due.isoformat()
        storage.save_analysis(an)
        if (now - due).total_seconds() <= CATCH_UP_HOURS * 3600:
            due_items.append(an)
    if not due_items:
        return
    try:
        _, grids = await asyncio.to_thread(sheets.read_grids, 60)
    except SheetsError as err:
        log.warning("Analyses not run, spreadsheet unreachable: %s", err.problem.value)
        return
    tables = {name: table_from(name, values, getattr(sheets, "decimal", None)) for name, values in grids.items()}

    for an in due_items:
        try:
            texts = {lang: message(lang, an, tables.get(an.sheet), now.date()) for lang in ("en", "ru")}
        except Exception as err:
            if not isinstance(err, ReportError):
                log.exception("Analysis %s failed", an.id)
                continue
            an.paused = True
            an.extra["broken"] = {"missing": err.missing, "kind": err.kind}
            storage.save_analysis(an)
            for admin in (u for u in storage.users() if u.is_admin):
                await send_safely(context, admin.chat_id,
                            t(admin.lang, "an_broken", title=analysis_title(admin.lang, an),
                              reason=broken_text(admin.lang, an, err)),
                            keyboard([[(t(admin.lang, "btn_edit"), f"an:edit:{an.id}")]]))
            continue
        if not texts["en"][1]:
            log.info("Analysis %s: nothing worth reporting", an.id)
            continue
        for user in recipients(storage, an):
            await send_safely(context, user.chat_id, texts.get(user.lang, texts["en"])[0])
        log.info("Analysis %s sent", an.id)
