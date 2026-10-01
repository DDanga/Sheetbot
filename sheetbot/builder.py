"""The rule builder and the rule list: everything happens on buttons.

One Telegram message is edited step by step, so the chat doesn't fill up with menus.
Button data carries indexes, not names: Telegram limits it to 64 bytes, and
Cyrillic column names would not fit.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .common import admin_user, clear_drafts, deps, error_text, fit, to_rows
from .i18n import t
from .rules import Match, Op, Recipients, Rule, RuleError, Target, check, parse_number, placeholders, render
from .sheets import SheetsError, Table
from .storage import Storage, User
from .texts import broken_text, op_text, recipients_text, report_title, rule_title
from .watcher import apply_fix, check_soon

log = logging.getLogger(__name__)

NAMES_IN_PREVIEW = 5
LINES_IN_CHECK = 10
PAGE = 30              # buttons per page in a picker; Telegram allows 100 per message
BUTTON_CHARS = 40      # longer column names are shortened on the button (the rule keeps the full name)
MAX_VALUE = 100        # characters in a typed value
MAX_TEMPLATE = 500     # characters in a message template
OPS_ROW_1 = (Op.LT, Op.LE, Op.EQ, Op.NE, Op.GE, Op.GT)
OPS_ROW_2 = (Op.CHANGED, Op.EMPTY, Op.CONTAINS)


# ---------------------------------------------------------------- helpers

async def edit(query, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the menu message; a double tap that changes nothing isn't an error."""
    try:
        await query.edit_message_text(fit(text), reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def button_text(text: str) -> str:
    """Telegram refuses a button without text, and a very long one is unreadable anyway."""
    text = " ".join(str(text).split()) or "—"
    return text if len(text) <= BUTTON_CHARS else text[:BUTTON_CHARS - 1] + "…"


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(button_text(text), callback_data=data) for text, data in row]
                                 for row in rows if row])


def paged(buttons: list[tuple[str, str]], page: int, page_data: str, per_row: int = 1) -> list[list[tuple[str, str]]]:
    """One page of buttons plus ◀ 2/5 ▶ when there are more than fit in one message."""
    pages = max(1, -(-len(buttons) // PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = buttons[page * PAGE:(page + 1) * PAGE]
    rows = [chunk[i:i + per_row] for i in range(0, len(chunk), per_row)]
    if pages > 1:
        nav = [("◀", f"{page_data}:{page - 1}")] if page > 0 else []
        nav.append((f"{page + 1}/{pages}", f"{page_data}:{page}"))
        if page < pages - 1:
            nav.append(("▶", f"{page_data}:{page + 1}"))
        rows.append(nav)
    return rows


def grid(items: list[str], prefix: str, per_row: int = 2, page: int = 0) -> list[list[tuple[str, str]]]:
    """Numbered buttons for a list; a long list (a sheet with 150 columns) gets pages."""
    buttons = [(name, f"{prefix}:{i}") for i, name in enumerate(items)]
    return paged(buttons, page, prefix.split(":")[0] + ":page", per_row)


# Every builder screen is stamped with a number. A tap on an older screen — a double
# tap that arrives after the menu already moved on, or a menu scrolled up in the chat —
# carries an old number and is ignored, instead of picking "item 3" of a different list.

MENU_NUMBERS = itertools.count(1)   # unique across every menu of every builder since the start


def stamp(markup: InlineKeyboardMarkup | None, prefix: str, version: int) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    return InlineKeyboardMarkup([
        [Btn(b.text, callback_data=f"{b.callback_data}:v{version}" if b.callback_data.startswith(prefix + ":")
             else b.callback_data) for b in row]
        for row in markup.inline_keyboard])


def unstamp(data: str) -> tuple[str, int | None]:
    head, _, tail = data.rpartition(":")
    if head and tail[:1] == "v" and tail[1:].isdigit():
        return head, int(tail[1:])
    return data, None


def is_stale(data: str, draft) -> bool:
    _, version = unstamp(data)
    return draft is None or (version is not None and version != draft.version)


async def load_tables(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Table]:
    _, _, sheets = deps(context)
    return await asyncio.to_thread(sheets.tables)


# ---------------------------------------------------------------- the draft

@dataclass
class Draft:
    rule: Rule
    step: str = "kind"
    history: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)   # what the numbered buttons stand for
    awaiting: str | None = None                        # "value" or "template": next text message
    editing: bool = False                              # changing part of a finished rule
    label_auto: bool = True                            # pick the row-name column automatically
    message_id: int | None = None
    version: int = 0                                   # see stamp()
    paged: tuple[str, int] = ("", 0)                   # (step, page) of a long picker

    def go(self, step: str) -> None:
        if self.step != step:
            self.history.append(self.step)
        self.step = step

    @property
    def page(self) -> int:
        return self.paged[1] if self.paged[0] == self.step else 0


def get_draft(context: ContextTypes.DEFAULT_TYPE) -> Draft | None:
    return context.user_data.get("draft")


_DATE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}")


def guess_label(table: Table | None) -> str:
    """The column that names a row: the first one holding mostly text (not numbers or dates).

    On the demo *Stock* sheet that's «Товар», so messages read "⚠ Milk: …", not "⚠ row 2: …".
    """
    if not table:
        return ""
    for i, header in enumerate(table.headers):
        cells = [row[i].strip() for row in table.rows if row[i].strip()]
        if not cells:
            continue
        words = [c for c in cells if table.number(c) is None and not _DATE.match(c)]
        if len(words) >= 0.6 * len(cells):
            return header
    return ""


# ---------------------------------------------------------------- screens

async def screen(lang: str, draft: Draft, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    """Text and buttons for the draft's current step."""
    r = draft.rule
    back = [(t(lang, "btn_back"), "nr:back")]
    if r.id is None:
        cancel = [(t(lang, "btn_cancel"), "nr:cancel")]
    else:
        # editing a saved rule: leaving discards the changes and returns to the list
        cancel = [(t(lang, "btn_back_rules"), "nr:torules")]
    nav = [back + cancel] if draft.history else [cancel]

    if draft.step == "kind":
        return t(lang, "nr_kind"), keyboard([
            [(t(lang, "kind_notify"), "nr:kind:notify")],
            [(t(lang, "kind_report"), "nr:kind:report"), (t(lang, "kind_analysis"), "nr:kind:analysis")],
            cancel,
        ])

    tables = await load_tables(context)

    if draft.step == "sheet":
        draft.options = [name for name, tb in tables.items() if tb.headers]
        if not draft.options:
            return t(lang, "nr_no_sheets"), keyboard(nav)
        return t(lang, "nr_sheet"), keyboard(grid(draft.options, "nr:pick", 2, draft.page) + nav)

    table = tables.get(r.sheet)
    headers = table.headers if table else []

    if draft.step == "column":
        draft.options = headers
        return t(lang, "nr_column", sheet=r.sheet), keyboard(grid(headers, "nr:pick", 3, draft.page) + nav)

    if draft.step == "op":
        ops = [[(op_text(lang, op), f"nr:op:{op.value}") for op in OPS_ROW_1],
               [(op_text(lang, op), f"nr:op:{op.value}") for op in OPS_ROW_2]]
        return t(lang, "nr_op", column=r.column), keyboard(ops + nav)

    if draft.step == "target":
        value_btn = "tgt_value_num" if r.op.numeric else "tgt_value_text"
        return t(lang, "nr_target", column=r.column, op=op_text(lang, r.op)), keyboard([
            [(t(lang, value_btn), "nr:tgt:value"), (t(lang, "tgt_column"), "nr:tgt:column")],
        ] + nav)

    if draft.step == "target_column":
        draft.options = [h for h in headers if h != r.column]
        return (t(lang, "nr_target_col", column=r.column, op=op_text(lang, r.op)),
                keyboard(grid(draft.options, "nr:pick", 3, draft.page) + nav))

    if draft.step == "value":
        draft.awaiting = "value"
        key = "nr_type_number" if r.op.numeric else ("nr_type_text" if r.op is Op.CONTAINS else "nr_type_value")
        return t(lang, key), keyboard(nav)

    if draft.label_auto and draft.step in ("to", "preview"):
        r.label = guess_label(table)
        draft.label_auto = False

    if draft.step == "label":
        draft.options = headers
        current = f"«{r.label}»" if r.label else t(lang, "label_row").lower()
        return t(lang, "nr_label", current=current), keyboard(
            grid(draft.options, "nr:pick", 3, draft.page) + [[(t(lang, "label_row"), "nr:label:none")]] + nav)

    if draft.step == "to":
        _, storage, _ = deps(context)
        draft.options = storage.teams()
        return t(lang, "nr_to"), keyboard(to_rows(lang, "nr", draft.options) + nav)

    if draft.step == "template":
        draft.awaiting = "template"
        example = r.label or (headers[0] if headers else "#")
        return t(lang, "nr_msg", example=example, column=r.column, columns=", ".join(headers)), keyboard(
            [[(t(lang, "btn_msg_reset"), "nr:msgreset")], back])

    # preview
    draft.history = []
    rows = [
        [(t(lang, "btn_save"), "nr:save")],
        [(t(lang, "btn_cond"), "nr:editcond"), (t(lang, "btn_msg"), "nr:editmsg")],
        [(t(lang, "btn_label"), "nr:editlabel"), (t(lang, "btn_to"), "nr:editto")],
    ]
    if r.op is not Op.CHANGED:   # a change has no "back to normal"
        rows.append([(t(lang, "btn_resolved_on" if r.notify_resolved else "btn_resolved_off"), "nr:resolved")])
    return preview(lang, r, table), keyboard(rows + [cancel])


def preview(lang: str, rule: Rule, table: Table | None) -> str:
    try:
        matches = check(rule, table)
    except RuleError as err:
        return broken_text(lang, rule, err)
    sample_match = matches[0] if matches else Match(
        row=2, name=f"{t(lang, 'row_word')} 2",
        values=dict(zip(table.headers, table.rows[0])) if table and table.rows else {})
    sample = render(rule, sample_match)
    if rule.op is Op.CHANGED:
        now = t(lang, "now_changed", column=rule.column)
    elif matches:
        names = ", ".join(m.name for m in matches[:NAMES_IN_PREVIEW])
        if len(matches) > NAMES_IN_PREVIEW:
            names = t(lang, "now_hits_more", names=names, more=len(matches) - NAMES_IN_PREVIEW)
        now = t(lang, "now_hits", n=len(matches), names=names)
    else:
        now = t(lang, "now_none")
    return t(lang, "nr_preview", title=rule_title(lang, rule), to=recipients_text(lang, rule),
             sample=sample, now=now)


# ---------------------------------------------------------------- showing

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, draft: Draft,
               new_message: bool = False) -> None:
    """Redraw the builder: edit the menu message, or send a new one after typed input."""
    try:
        text, markup = await screen(user.lang, draft, context)
    except SheetsError as err:
        config, _, _ = deps(context)
        text, markup = error_text(user.lang, err, config), None
    draft.version = next(MENU_NUMBERS)
    markup = stamp(markup, "nr", draft.version)
    query = update.callback_query
    if query and not new_message:
        await edit(query, text, markup)
    else:
        msg = await update.effective_chat.send_message(fit(text), reply_markup=markup)
        draft.message_id = msg.message_id


# ---------------------------------------------------------------- entry points

async def cmd_newrule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await admin_user(update, context)
    if not user:
        return
    draft = Draft(rule=Rule(sheet="", column="", op=Op.LT, owner=user.chat_id))
    clear_drafts(context)
    context.user_data["draft"] = draft
    await show(update, context, user, draft, new_message=True)


async def on_builder_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = await admin_user(update, context)
    if not user:
        await query.answer()
        return
    draft = get_draft(context)
    if is_stale(query.data, draft):
        await query.answer(t(user.lang, "stale_tap"))
        return

    _, action, *rest = unstamp(query.data)[0].split(":")
    arg = rest[0] if rest else ""
    r = draft.rule
    draft.awaiting = None

    if action == "page":
        draft.paged = (draft.step, int(arg))
    elif action == "kind":
        if arg == "report":
            await query.answer()
            from .report_builder import start  # the report builder lives next door
            await start(update, context, user)
            return
        if arg == "analysis":
            await query.answer()
            from .analysis_builder import start as start_analysis
            await start_analysis(update, context, user)
            return
        draft.go("sheet")
    elif action == "pick":
        choice = draft.options[int(arg)]
        if draft.step == "sheet":
            r.sheet = choice
            draft.go("column")
        elif draft.step == "column":
            r.column = choice
            draft.go("op")
        elif draft.step == "target_column":
            r.target, r.target_value = Target.COLUMN, choice
            draft.go(_after_target(draft))
        elif draft.step == "label":
            r.label = choice
            draft.step, draft.history = "preview", []
    elif action == "op":
        r.op = Op(arg)
        if not r.op.needs_target:
            r.target, r.target_value = None, ""
            draft.go(_after_target(draft))
        elif r.op is Op.CONTAINS:
            r.target = Target.VALUE
            draft.go("value")
        else:
            draft.go("target")
    elif action == "tgt":
        if arg == "column":
            draft.go("target_column")
        else:
            r.target = Target.VALUE
            draft.go("value")
    elif action == "label":
        r.label = ""
        draft.step, draft.history = "preview", []
    elif action == "to":
        r.recipients, r.team = Recipients(arg), ""
        draft.go("preview")
    elif action == "team":
        r.recipients, r.team = Recipients.TEAM, draft.options[int(arg)]
        draft.go("preview")
    elif action == "editcond":
        draft.editing = True
        draft.history = ["preview"]
        draft.step = "column"
    elif action == "editmsg":
        draft.history = ["preview"]
        draft.step = "template"
    elif action == "resolved":
        r.notify_resolved = not r.notify_resolved
    elif action == "editlabel":
        draft.history = ["preview"]
        draft.step = "label"
    elif action == "editto":
        draft.history = ["preview"]
        draft.step = "to"
    elif action == "msgreset":
        r.template = ""
        draft.step, draft.history = "preview", []
    elif action == "back":
        draft.step = draft.history.pop() if draft.history else "kind"
    elif action == "save":
        _, storage, _ = deps(context)
        if r.extra.pop("broken", None):
            r.paused = False            # the rule was fixed by editing it
        storage.save_rule(r)
        if draft.editing:
            storage.forget_hits(r.id)   # the condition may be different now: start over
        check_soon(context)
        context.user_data.pop("draft", None)
        await query.answer()
        await edit(query, t(user.lang, "nr_saved", title=rule_title(user.lang, r)))
        log.info("Rule %s saved: %s", r.id, rule_title("en", r))
        return
    elif action == "cancel":
        context.user_data.pop("draft", None)
        await query.answer()
        await edit(query, t(user.lang, "nr_cancelled"))
        return
    elif action == "torules":
        context.user_data.pop("draft", None)
        await query.answer()
        _, storage, _ = deps(context)
        text, markup = rules_list_view(user.lang, storage)
        await edit(query, text, markup)
        return

    await query.answer()
    await show(update, context, user, draft)


# the row-name column is picked automatically (see guess_label), so after the
# condition comes "who gets it"; when only the condition is being changed, the
# rest is already chosen and the builder jumps straight back to the preview

def _after_target(draft: Draft) -> str:
    return "preview" if draft.editing else "to"


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Typed input for the builder (a number, a text or a message template).

    Returns False when the builder isn't waiting for text, so the caller can
    answer the message in the usual way.
    """
    draft = get_draft(context)
    if not draft or not draft.awaiting:
        return False
    user = await admin_user(update, context)
    if not user:
        return True
    text = (update.effective_message.text or "").strip()
    r = draft.rule
    limit = MAX_VALUE if draft.awaiting == "value" else MAX_TEMPLATE
    if not text:
        await update.effective_chat.send_message(t(user.lang, "nr_empty_text"))
        return True
    if len(text) > limit:
        await update.effective_chat.send_message(t(user.lang, "nr_too_long", n=limit))
        return True

    if draft.awaiting == "value":
        if r.op.numeric and parse_number(text) is None:
            await update.effective_chat.send_message(t(user.lang, "nr_not_number"))
            return True
        r.target_value = text
        draft.awaiting = None
        draft.go(_after_target(draft))
    else:  # template
        tables = await load_tables(context)
        table = tables.get(r.sheet)
        known = set(table.headers if table else []) | {"#"}
        unknown = sorted(placeholders(text) - known)
        if unknown:
            await update.effective_chat.send_message(
                t(user.lang, "nr_msg_unknown", names=", ".join(f"{{{u}}}" for u in unknown)))
            return True
        r.template = text
        draft.awaiting = None
        draft.step, draft.history = "preview", []

    await show(update, context, user, draft, new_message=True)
    return True


# ---------------------------------------------------------------- /rules

def rules_list_view(lang: str, storage: Storage, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    from .texts import analysis_title
    rules, reports, analyses = storage.rules(), storage.reports(), storage.analyses()
    if not rules and not reports and not analyses:
        return t(lang, "rules_empty"), None
    rows = [[(("⏸ " if r.paused else "🔔 ") + rule_title(lang, r), f"rl:open:{r.id}")] for r in rules]
    rows += [[(("⏸ " if r.paused else "📊 ") + report_title(lang, r), f"rp:open:{r.id}")] for r in reports]
    rows += [[(("⏸ " if a.paused else "📈 ") + analysis_title(lang, a), f"an:open:{a.id}")] for a in analyses]
    return t(lang, "rules_title", n=len(rows)), keyboard(paged([r[0] for r in rows], page, "rl:list"))


def rule_card_view(lang: str, rule: Rule) -> tuple[str, InlineKeyboardMarkup]:
    if rule.extra.get("broken"):
        status = t(lang, "rule_broken_status")
    else:
        status = t(lang, "rule_off" if rule.paused else "rule_on")
    pause = ("btn_resume", "resume") if rule.paused else ("btn_pause", "pause")
    return (
        t(lang, "rule_card", title=rule_title(lang, rule), to=recipients_text(lang, rule), status=status),
        keyboard([
            [(t(lang, "btn_edit"), f"rl:edit:{rule.id}"), (t(lang, pause[0]), f"rl:{pause[1]}:{rule.id}")],
            [(t(lang, "btn_check"), f"rl:check:{rule.id}"), (t(lang, "btn_delete"), f"rl:del:{rule.id}")],
            [(t(lang, "btn_all_rules"), "rl:list")],
        ]),
    )


def check_view(lang: str, rule: Rule, table: Table | None) -> str:
    title = rule_title(lang, rule)
    if rule.op is Op.CHANGED:
        return t(lang, "check_changed", title=title)
    try:
        matches = check(rule, table)
    except RuleError as err:
        return f"🧪 {title}\n{broken_text(lang, rule, err)}"
    if not matches:
        return t(lang, "check_none", title=title)
    lines = [render(rule, m) for m in matches[:LINES_IN_CHECK]]
    if len(matches) > LINES_IN_CHECK:
        lines.append(t(lang, "check_more", n=len(matches) - LINES_IN_CHECK))
    return t(lang, "check_hits", title=title, n=len(matches), lines="\n".join(lines))


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await admin_user(update, context)
    if not user:
        return
    _, storage, _ = deps(context)
    text, markup = rules_list_view(user.lang, storage)
    await update.effective_chat.send_message(text, reply_markup=markup)


async def on_rules_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = await admin_user(update, context)
    await query.answer()
    if not user:
        return
    config, storage, _ = deps(context)
    _, action, *rest = query.data.split(":")
    lang = user.lang

    if action == "list":
        text, markup = rules_list_view(lang, storage, int(rest[0]) if rest else 0)
        await edit(query, text, markup)
        return

    rule = storage.rule(int(rest[0]))
    if rule is None:
        text, markup = rules_list_view(lang, storage)
        await edit(query, text, markup)
        return

    if action in ("pause", "resume"):
        rule.paused = action == "pause"
        rule.extra.pop("broken", None)
        storage.save_rule(rule)
        if not rule.paused:
            check_soon(context)
    elif action == "fix":
        new_name = apply_fix(rule)
        if new_name:
            storage.save_rule(rule)
            storage.forget_hits(rule.id)
            check_soon(context)
            text, markup = rule_card_view(lang, rule)
            await edit(query, t(lang, "rule_fixed", name=new_name) + "\n\n" + text, markup)
            return
    elif action == "del":
        await edit(
            query, t(lang, "rule_delete_q", title=rule_title(lang, rule)),
            reply_markup=keyboard([[(t(lang, "btn_delete_yes"), f"rl:delok:{rule.id}"),
                                    (t(lang, "btn_cancel"), f"rl:open:{rule.id}")]]))
        return
    elif action == "delok":
        storage.delete_rule(rule.id)
        text, markup = rules_list_view(lang, storage)
        await edit(query, t(lang, "rule_deleted") + "\n\n" + text, markup)
        return
    elif action == "check":
        try:
            tables = await load_tables(context)
            text = check_view(lang, rule, tables.get(rule.sheet))
        except SheetsError as err:
            text = error_text(lang, err, config)
        await edit(query, text, reply_markup=keyboard([[(t(lang, "btn_back"), f"rl:open:{rule.id}")]]))
        return
    elif action == "edit":
        draft = Draft(rule=rule, step="preview", editing=True, label_auto=False)
        clear_drafts(context)
        context.user_data["draft"] = draft
        await show(update, context, user, draft)
        return

    text, markup = rule_card_view(lang, rule)
    await edit(query, text, markup)
