"""The report builder, the report card in /rules, /report, and the report schedule.

Same idea as the rule builder: one message edited step by step, buttons carry
indexes, and the preview runs the real calculation on the live sheet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .builder import MENU_NUMBERS, edit, grid, is_stale, keyboard, load_tables, rules_list_view, stamp, unstamp
from .common import admin_user, clear_drafts, deps, error_text, fit, known_user, recipients, send_safely, to_rows
from .i18n import t
from .reports import (
    Freq,
    Func,
    Metric,
    Period,
    Report,
    ReportError,
    date_columns,
    last_due,
    number_columns,
    run,
    text_columns,
    valid_time,
)
from .rules import Recipients
from .sheets import SheetsError, Table, table_from
from .storage import User
from .texts import recipients_text, report_message, report_title, schedule_text

log = logging.getLogger(__name__)

TIMES = ("08:00", "09:00", "10:00", "18:00")
CATCH_UP_HOURS = 12   # a report missed while the bot was off is still sent if it's this fresh


@dataclass
class ReportDraft:
    report: Report
    step: str = "sheet"
    history: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    pending_func: Func | None = None
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


def local_now(context: ContextTypes.DEFAULT_TYPE) -> datetime:
    config, _, _ = deps(context)
    return datetime.now(config.timezone)


# ---------------------------------------------------------------- screens

async def screen(lang: str, d: ReportDraft, context) -> tuple[str, InlineKeyboardMarkup]:
    rp = d.report
    back = [(t(lang, "btn_back"), "rb:back")]
    cancel = ([(t(lang, "btn_cancel"), "rb:cancel")] if rp.id is None
              else [(t(lang, "btn_back_rules"), "rb:torules")])
    nav = [back + cancel] if d.history else [cancel]
    tables = await load_tables(context)
    table = tables.get(rp.sheet)

    if d.step == "sheet":
        d.options = [name for name, tb in tables.items() if date_columns(tb)]
        if not d.options:
            return t(lang, "rb_no_dates"), keyboard(nav)
        return t(lang, "rb_sheet"), keyboard(grid(d.options, "rb:pick", 2, d.page) + nav)

    if d.step == "date":
        d.options = date_columns(table) if table else []
        return t(lang, "rb_date"), keyboard(grid(d.options, "rb:pick", 2, d.page) + nav)

    if d.step == "period":
        return t(lang, "rb_period"), keyboard(
            [[(t(lang, f"period_{p.value}"), f"rb:period:{p.value}")] for p in Period] + nav)

    if d.step == "func":
        funcs = [Func.SUM, Func.AVG, Func.MAX, Func.MIN, Func.COUNT]
        rows = [[(t(lang, f"f_{f.value}"), f"rb:func:{f.value}") for f in funcs[:2]],
                [(t(lang, f"f_{f.value}"), f"rb:func:{f.value}") for f in funcs[2:4]],
                [(t(lang, "f_count"), "rb:func:count")]]
        return t(lang, "rb_func"), keyboard(rows + nav)

    if d.step == "column":
        d.options = number_columns(table) if table else []
        return (t(lang, "rb_column", func=t(lang, f"f_{d.pending_func.value}")),
                keyboard(grid(d.options, "rb:pick", 3, d.page) + nav))

    if d.step == "more":
        from .texts import metric_label
        names = ", ".join(metric_label(lang, m) for m in rp.metrics)
        return t(lang, "rb_more", metrics=names), keyboard(
            [[(t(lang, "btn_more"), "rb:more"), (t(lang, "btn_done"), "rb:done")]] + nav)

    if d.step == "group":
        d.options = [c for c in (text_columns(table) if table else []) if c != rp.date_column]
        return t(lang, "rb_group"), keyboard(
            [[(t(lang, "btn_no_split"), "rb:nogroup")]] + grid(d.options, "rb:pick", 3, d.page) + nav)

    if d.step == "freq":
        return t(lang, "rb_freq"), keyboard(
            [[(t(lang, f"freq_{f.value}"), f"rb:freq:{f.value}")] for f in Freq] + nav)

    if d.step == "weekday":
        return t(lang, "rb_weekday"), keyboard(
            [[(t(lang, f"wds_{i}"), f"rb:wd:{i}") for i in range(7)]] + nav)

    if d.step == "time":
        config, _, _ = deps(context)
        return t(lang, "rb_time", tz=config.timezone.key), keyboard(
            [[(x, f"rb:time:{x}") for x in TIMES], [(t(lang, "btn_own_time"), "rb:owntime")]] + nav)

    if d.step == "typetime":
        d.awaiting = "time"
        return t(lang, "rb_type_time"), keyboard(nav)

    if d.step == "to":
        _, storage, _ = deps(context)
        d.options = storage.teams()
        return t(lang, "nr_to"), keyboard(to_rows(lang, "rb", d.options) + nav)

    # preview
    d.history = []
    return preview(lang, rp, table, local_now(context).date()), keyboard([
        [(t(lang, "btn_save"), "rb:save")],
        [(t(lang, "btn_metrics"), "rb:editmetrics"), (t(lang, "btn_period"), "rb:editperiod")],
        [(t(lang, "btn_schedule"), "rb:editschedule"), (t(lang, "btn_to"), "rb:editto")],
        cancel,
    ])


def preview(lang: str, rp: Report, table: Table | None, today) -> str:
    try:
        sample = report_message(lang, rp, run(rp, table, today))
    except ReportError as err:
        sample = broken_text(lang, rp, err)
    return t(lang, "rb_preview", title=report_title(lang, rp), schedule=schedule_text(lang, rp),
             to=recipients_text(lang, rp), sample=sample)


def broken_text(lang: str, rp: Report, err: ReportError) -> str:
    if err.kind == "sheet":
        return t(lang, "rule_broken_sheet", name=err.missing)
    return t(lang, "rule_broken_col", name=err.missing, sheet=rp.sheet)


async def show(update: Update, context, user: User, d: ReportDraft, new_message: bool = False) -> None:
    try:
        text, markup = await screen(user.lang, d, context)
    except SheetsError as err:
        config, _, _ = deps(context)
        text, markup = error_text(user.lang, err, config), None
    d.version = next(MENU_NUMBERS)
    markup = stamp(markup, "rb", d.version)
    if update.callback_query and not new_message:
        await edit(update.callback_query, text, markup)
    else:
        await update.effective_chat.send_message(fit(text), reply_markup=markup)


# ---------------------------------------------------------------- building

async def start(update: Update, context, user: User) -> None:
    """Called from /newrule → 📊 Report."""
    clear_drafts(context)
    d = ReportDraft(report=Report(sheet="", date_column="", owner=user.chat_id))
    context.user_data["rdraft"] = d
    await show(update, context, user, d)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = await admin_user(update, context)
    if not user:
        await query.answer()
        return
    d: ReportDraft | None = context.user_data.get("rdraft")
    if is_stale(query.data, d):
        await query.answer(t(user.lang, "stale_tap"))
        return
    await query.answer()
    _, action, *rest = unstamp(query.data)[0].split(":", 2)
    arg = rest[0] if rest else ""
    rp = d.report
    d.awaiting = None

    if action == "page":
        d.paged = (d.step, int(arg))
    elif action == "pick":
        choice = d.options[int(arg)]
        if d.step == "sheet":
            rp.sheet = choice
            table = (await load_tables(context)).get(choice)   # None if the sheet was deleted meanwhile
            dates = date_columns(table) if table else []
            if len(dates) == 1:          # one date column: nothing to ask
                rp.date_column = dates[0]
                d.go("period")
            else:
                d.go("date")
        elif d.step == "date":
            rp.date_column = choice
            d.go("period")
        elif d.step == "column":
            rp.metrics.append(Metric(d.pending_func, choice))
            d.go("more")
        elif d.step == "group":
            rp.group_by = choice
            d.go("preview" if d.editing else "freq")
    elif action == "period":
        rp.period = Period(arg)
        d.go("preview" if d.editing else "func")
    elif action == "func":
        func = Func(arg)
        if func is Func.COUNT:
            rp.metrics.append(Metric(func))
            d.go("more")
        else:
            d.pending_func = func
            d.go("column")
    elif action == "more":
        d.go("func")
    elif action == "done":
        d.go("preview" if d.editing else "group")
    elif action == "nogroup":
        rp.group_by = ""
        d.go("preview" if d.editing else "freq")
    elif action == "freq":
        rp.freq = Freq(arg)
        d.go("weekday" if rp.freq is Freq.WEEKLY else "time")
    elif action == "wd":
        rp.weekday = int(arg)
        d.go("time")
    elif action == "time":
        rp.at = arg
        d.go("preview" if d.editing else "to")
    elif action == "owntime":
        d.go("typetime")
    elif action == "to":
        rp.recipients, rp.team = Recipients(arg), ""
        d.go("preview")
    elif action == "team":
        rp.recipients, rp.team = Recipients.TEAM, d.options[int(arg)]
        d.go("preview")
    elif action == "editmetrics":
        rp.metrics = []
        d.editing, d.history, d.step = True, ["preview"], "func"
    elif action == "editperiod":
        d.editing, d.history, d.step = True, ["preview"], "period"
    elif action == "editschedule":
        d.editing, d.history, d.step = True, ["preview"], "freq"
    elif action == "editto":
        d.history, d.step = ["preview"], "to"
    elif action == "back":
        d.step = d.history.pop() if d.history else "sheet"
        if d.step == "more" and not rp.metrics:
            d.step = "func"
    elif action == "save":
        _, storage, _ = deps(context)
        if not rp.metrics:
            rp.metrics = [Metric(Func.COUNT)]
        if rp.extra.pop("broken", None):
            rp.paused = False            # the report was fixed by editing it
        # the next send is the next scheduled time, not "right now"
        due = last_due(rp, local_now(context))
        rp.extra["last_sent"] = due.isoformat() if due else ""
        storage.save_report(rp)
        context.user_data.pop("rdraft", None)
        await edit(query, t(user.lang, "rb_saved", title=report_title(user.lang, rp),
                            schedule=schedule_text(user.lang, rp)))
        log.info("Report %s saved", rp.id)
        return
    elif action == "cancel":
        context.user_data.pop("rdraft", None)
        await edit(query, t(user.lang, "nr_cancelled"))
        return
    elif action == "torules":
        context.user_data.pop("rdraft", None)
        _, storage, _ = deps(context)
        text, markup = rules_list_view(user.lang, storage)
        await edit(query, text, markup)
        return
    await show(update, context, user, d)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    d: ReportDraft | None = context.user_data.get("rdraft")
    if not d or d.awaiting != "time":
        return False
    user = await admin_user(update, context)
    if not user:
        return True
    at = valid_time(update.effective_message.text or "")
    if not at:
        await update.effective_chat.send_message(t(user.lang, "rb_bad_time"))
        return True
    d.report.at = at
    d.awaiting = None
    d.go("preview" if d.editing else "to")
    await show(update, context, user, d, new_message=True)
    return True


# ---------------------------------------------------------------- card in /rules

def report_card_view(lang: str, rp: Report) -> tuple[str, InlineKeyboardMarkup]:
    if rp.extra.get("broken"):
        status = t(lang, "rule_broken_status")
    else:
        status = t(lang, "rule_off" if rp.paused else "rule_on")
    pause = ("btn_resume", "resume") if rp.paused else ("btn_pause", "pause")
    return (
        t(lang, "report_card", title=report_title(lang, rp), schedule=schedule_text(lang, rp),
          to=recipients_text(lang, rp), status=status),
        keyboard([
            [(t(lang, "btn_edit"), f"rp:edit:{rp.id}"), (t(lang, pause[0]), f"rp:{pause[1]}:{rp.id}")],
            [(t(lang, "btn_send_now"), f"rp:now:{rp.id}"), (t(lang, "btn_delete"), f"rp:del:{rp.id}")],
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
    _, action, rid = query.data.split(":")
    lang = user.lang
    rp = storage.report(int(rid))
    if rp is None:
        text, markup = rules_list_view(lang, storage)
        await edit(query, text, markup)
        return

    if action in ("pause", "resume"):
        rp.paused = action == "pause"
        rp.extra.pop("broken", None)
        storage.save_report(rp)
    elif action == "del":
        await edit(query, t(lang, "rule_delete_q", title=report_title(lang, rp)), keyboard(
            [[(t(lang, "btn_delete_yes"), f"rp:delok:{rp.id}"), (t(lang, "btn_cancel"), f"rp:open:{rp.id}")]]))
        return
    elif action == "delok":
        storage.delete_rule(rp.id)
        text, markup = rules_list_view(lang, storage)
        await edit(query, t(lang, "rule_deleted") + "\n\n" + text, markup)
        return
    elif action == "now":
        await send_report_now(update, context, user, rp)
        return
    elif action == "edit":
        d = ReportDraft(report=rp, step="preview", editing=True)
        clear_drafts(context)
        context.user_data["rdraft"] = d
        await show(update, context, user, d)
        return

    text, markup = report_card_view(lang, rp)
    await edit(query, text, markup)


# ---------------------------------------------------------------- sending

async def send_report_now(update: Update, context, user: User, rp: Report) -> None:
    config, _, _ = deps(context)
    try:
        tables = await load_tables(context)
        text = report_message(user.lang, rp, run(rp, tables.get(rp.sheet), local_now(context).date()))
    except SheetsError as err:
        text = error_text(user.lang, err, config)
    except ReportError as err:
        text = broken_text(user.lang, rp, err)
    await update.effective_chat.send_message(text)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await known_user(update, context)
    if not user:
        return
    from .analysis_builder import send_now as send_analysis_now
    from .texts import analysis_title
    _, storage, _ = deps(context)
    reports, analyses = storage.reports(), storage.analyses()
    if not reports and not analyses:
        await update.effective_chat.send_message(t(user.lang, "reports_none"))
    elif len(reports) + len(analyses) == 1:
        if reports:
            await send_report_now(update, context, user, reports[0])
        else:
            await send_analysis_now(update, context, user, analyses[0])
    else:
        rows = [[("📊 " + report_title(user.lang, r), f"rp:now:{r.id}")] for r in reports]
        rows += [[("📈 " + analysis_title(user.lang, a), f"an:now:{a.id}")] for a in analyses]
        await update.effective_chat.send_message(t(user.lang, "reports_pick"), reply_markup=keyboard(rows))


async def schedule_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every minute: send the reports whose time has come."""
    _, storage, sheets = deps(context)
    now = local_now(context)
    due_reports = []
    for rp in storage.reports():
        if rp.paused:
            continue
        due = last_due(rp, now)
        sent = rp.extra.get("last_sent", "")
        if due is None or (sent and datetime.fromisoformat(sent) >= due):
            continue
        rp.extra["last_sent"] = due.isoformat()
        storage.save_report(rp)
        if (now - due).total_seconds() <= CATCH_UP_HOURS * 3600:
            due_reports.append(rp)
        else:
            log.info("Report %s skipped: its time (%s) passed long ago", rp.id, due)
    if not due_reports:
        return

    try:
        _, grids = await asyncio.to_thread(sheets.read_grids, 60)
    except SheetsError as err:
        log.warning("Reports not sent, spreadsheet unreachable: %s", err.problem.value)
        return
    tables = {name: table_from(name, values, getattr(sheets, "decimal", None)) for name, values in grids.items()}

    for rp in due_reports:
        try:
            result = run(rp, tables.get(rp.sheet), now.date())
        except Exception as err:
            if not isinstance(err, ReportError):
                log.exception("Report %s failed", rp.id)
                continue
            rp.paused = True
            rp.extra["broken"] = {"missing": err.missing, "kind": err.kind}
            storage.save_report(rp)
            for admin in (u for u in storage.users() if u.is_admin):
                text = t(admin.lang, "report_broken", title=report_title(admin.lang, rp),
                         reason=broken_text(admin.lang, rp, err))
                await send_safely(context, admin.chat_id, text,
                            keyboard([[(t(admin.lang, "btn_edit"), f"rp:edit:{rp.id}")]]))
            continue
        for user in recipients(storage, rp):
            await send_safely(context, user.chat_id, report_message(user.lang, rp, result))
        log.info("Report %s sent", rp.id)

