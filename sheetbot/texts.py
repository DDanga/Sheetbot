"""How rules read in messages: titles, conditions, recipients, problems."""

from __future__ import annotations

from .i18n import t
from .rules import Op, Recipients, Rule, RuleError, Target, parse_number


def op_text(lang: str, op: Op) -> str:
    return op.symbol or t(lang, f"op_{op.value}")


def condition_text(lang: str, rule: Rule) -> str:
    """'Остаток < Минимум', 'Остаток < 10', 'Товар содержит «молоко»', 'Остаток пусто'."""
    op = op_text(lang, rule.op)
    if not rule.op.needs_target:
        return f"{rule.column} {op}"
    right = rule.target_value
    if rule.target is Target.VALUE and parse_number(right) is None:
        right = f"«{right}»"
    return f"{rule.column} {op} {right}"


def rule_title(lang: str, rule: Rule) -> str:
    return f"{rule.sheet}: {condition_text(lang, rule)}"


def recipients_text(lang: str, rule: Rule) -> str:
    if rule.recipients is Recipients.TEAM:
        return t(lang, "to_team", team=rule.team)
    return t(lang, "to_me" if rule.recipients is Recipients.ME else "to_all")


def broken_text(lang: str, rule: Rule, err: RuleError) -> str:
    if err.kind == "sheet":
        return t(lang, "rule_broken_sheet", name=err.missing)
    return t(lang, "rule_broken_col", name=err.missing, sheet=rule.sheet)


# ---------------------------------------------------------------- reports

def fmt_number(lang: str, value: float | None, decimals: int | None = None) -> str:
    """4660 → '4 660' (ru) / '4,660' (en); 1234.5 → '1 234,5' / '1,234.5'. `decimals=2` always shows cents."""
    if value is None:
        return "—"
    if decimals is not None:
        text = f"{value:,.{decimals}f}"
    elif abs(value - round(value)) < 1e-9:
        text = f"{int(round(value)):,}"
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    if lang != "ru":
        return text
    return text.replace(",", "\u202f").replace(".", ",")


def fmt_money(lang: str, value: float | None, symbol: str = "") -> str:
    """With a currency the sheet shows: '$1,827.32' (en), '1 827,32 ₽' (ru); without one, a plain number."""
    if not symbol or value is None:
        return fmt_number(lang, value)
    text = fmt_number(lang, abs(value), decimals=2)
    sign = "−" if value < 0 else ""
    return f"{sign}{text} {symbol}" if lang == "ru" else f"{sign}{symbol}{text}"


def metric_label(lang: str, metric) -> str:
    from .reports import Func
    if metric.func is Func.COUNT:
        return t(lang, "m_count")
    if metric.func is Func.SUM:
        return metric.column
    return t(lang, f"m_{metric.func.value}", column=metric.column)


def period_text(lang: str, start, end) -> str:
    if start == end:
        return start.strftime("%d.%m")
    return f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}"


def schedule_text(lang: str, report) -> str:
    from .reports import Freq
    if report.freq is Freq.WEEKLY:
        when = t(lang, "freq_weekly_on", day=t(lang, f"wd_{report.weekday}"))
    else:
        when = t(lang, f"freq_{report.freq.value}")
    return f"{when}, {report.at}"


def report_title(lang: str, report) -> str:
    return f"{report.sheet} · {t(lang, 'period_' + report.period.value).lower()}"


def report_message(lang: str, report, result) -> str:
    head = t(lang, "report_head", sheet=report.sheet, period=period_text(lang, result.start, result.end))
    if result.rows == 0:
        return head + "\n\n" + t(lang, "report_empty")

    def value(i: int, v: float | None) -> str:
        m = report.metrics[i]
        return fmt_money(lang, v, result.currency.get(m.column, ""))

    totals = " · ".join(f"{metric_label(lang, m)}: {value(i, v)}"
                        for i, (m, v) in enumerate(zip(report.metrics, result.totals)))
    text = f"{head}\n\n{totals}"
    if report.group_by and result.groups:
        lines = [f"{name} — " + " · ".join(value(i, v) for i, v in enumerate(vals))
                 for name, vals in result.groups[:GROUP_LINES]]
        if len(result.groups) > GROUP_LINES:
            lines.append(t(lang, "alert_more", n=len(result.groups) - GROUP_LINES))
        text += "\n\n" + t(lang, "report_by", column=report.group_by) + "\n" + "\n".join(lines)
    return text


GROUP_LINES = 10


# ---------------------------------------------------------------- analysis

def _pct(lang: str, value: float) -> str:
    text = f"{abs(value):.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if lang == "ru" else text


def analysis_title(lang: str, an) -> str:
    from .analysis import Kind
    if an.kind is Kind.COMPARE:
        return f"{an.sheet}: {metric_label(lang, an.metric)} · {t(lang, 'unit_' + an.unit.value)}"
    return f"{an.sheet}: {t(lang, 'an_top_title', n=an.top_n, item=an.item)} · " \
           f"{t(lang, 'period_' + an.period.value).lower()}"


def compare_message(lang: str, an, res) -> str:
    unit = t(lang, "unit_" + an.unit.value)
    label = metric_label(lang, an.metric)
    if res.change is None:
        head = f"📊 {label} · {unit}"
    else:
        icon = "📈" if res.change > 0 else "📉" if res.change < 0 else "➡"
        sign = "+" if res.change > 0 else "−" if res.change < 0 else ""
        head = f"{icon} {label}: {sign}{_pct(lang, res.change)}% · {unit}"
    lines = [
        t(lang, "an_line", period=period_text(lang, *res.current), value=fmt_money(lang, res.now, res.currency),
          n=res.now_rows),
        t(lang, "an_line", period=period_text(lang, *res.previous), value=fmt_money(lang, res.before, res.currency),
          n=res.before_rows),
    ]
    text = head + "\n\n" + "\n".join(lines)
    if res.leaders:
        text += "\n\n" + t(lang, "an_leaders") + " " + \
            " · ".join(f"{name} {fmt_money(lang, v, res.currency)}" for name, v in res.leaders)
    return text


def top_message(lang: str, an, res) -> str:
    head = t(lang, "an_top_head", period=period_text(lang, res.start, res.end))
    if res.rows == 0:
        return head + "\n\n" + t(lang, "report_empty")
    n = an.top_n
    text = head + "\n\n" + t(lang, "an_by_qty") + "\n" + "\n".join(
        f"{i}. {name} — {fmt_number(lang, v)}" for i, (name, v) in enumerate(res.by_qty[:n], 1))
    if res.by_profit:
        text += "\n\n" + t(lang, "an_by_profit") + "\n" + "\n".join(
            f"{i}. {name} — {fmt_money(lang, v, res.currency)}" for i, (name, v) in enumerate(res.by_profit[:n], 1))
    star = res.star
    if star:
        margin = t(lang, "an_margin", pct=_pct(lang, round(star.margin))) if star.margin is not None else ""
        text += "\n\n" + t(lang, "an_star", name=star.name, qty_rank=star.qty_rank, qty=fmt_number(lang, star.qty),
                           profit_rank=star.profit_rank, margin=margin)
        best_profit_rank = next((i for i, (nm, _) in enumerate(res.by_profit, 1) if nm == star.best_seller), 0)
        if best_profit_rank > n:
            text += " " + t(lang, "an_best_seller", name=star.best_seller,
                            profit=fmt_money(lang, star.best_seller_profit, res.currency))
    return text


def when_text(lang: str, an) -> str:
    from .analysis import Kind, When
    if an.kind is not Kind.COMPARE or an.when is When.ALWAYS:
        return t(lang, "when_always")
    return t(lang, "when_" + an.when.value, pct=_pct(lang, an.pct))
