"""Analysis: compare two periods, or rank items and find the "hidden stars".

Plain calculation, no Telegram, like reports.py. The schedule fields match a
report's, so the same `last_due` decides when an analysis runs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import Enum

from .reports import Freq, Func, Metric, Period, ReportError, _aggregate, currency_of, parse_date, period_range, \
    text_columns
from .rules import Recipients
from .sheets import Table


class Kind(str, Enum):
    COMPARE = "compare"   # this period against the previous one
    TOP = "top"           # best items by quantity and by profit


class Unit(str, Enum):
    DAY = "day"       # yesterday vs the day before
    WEEK = "week"     # the last 7 days vs the 7 before them
    MONTH = "month"   # last month vs the month before


class When(str, Enum):
    ALWAYS = "always"
    DROP = "drop"       # only if it fell by more than pct
    CHANGE = "change"   # only if it moved by more than pct either way


class Profit(str, Enum):
    DIFF = "diff"       # revenue column minus cost column
    COLUMN = "column"   # the sheet already has a profit column
    NONE = "none"


@dataclass
class Analysis:
    kind: Kind
    sheet: str
    date_column: str
    # compare
    metric: Metric = field(default_factory=lambda: Metric(Func.SUM))
    unit: Unit = Unit.WEEK
    when: When = When.ALWAYS
    pct: float = 20
    # top
    item: str = ""
    qty: str = ""                 # column with quantities; "" = count rows
    profit: Profit = Profit.NONE
    revenue: str = ""
    cost: str = ""
    profit_col: str = ""
    period: Period = Period.LAST_MONTH
    top_n: int = 3
    # schedule, like a report
    freq: Freq = Freq.WEEKLY
    weekday: int = 0
    at: str = "09:00"
    recipients: Recipients = Recipients.ME
    team: str = ""
    owner: int = 0
    paused: bool = False
    id: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(kind=self.kind.value, unit=self.unit.value, when=self.when.value, profit=self.profit.value,
                 period=self.period.value, freq=self.freq.value, recipients=self.recipients.value,
                 metric={"func": self.metric.func.value, "column": self.metric.column})
        d.pop("id")
        return d

    @classmethod
    def from_dict(cls, d: dict, an_id: int | None = None) -> "Analysis":
        m = d.get("metric") or {"func": "sum", "column": ""}
        return cls(
            kind=Kind(d["kind"]), sheet=d["sheet"], date_column=d["date_column"],
            metric=Metric(Func(m["func"]), m.get("column", "")), unit=Unit(d.get("unit", "week")),
            when=When(d.get("when", "always")), pct=d.get("pct", 20),
            item=d.get("item", ""), qty=d.get("qty", ""), profit=Profit(d.get("profit", "none")),
            revenue=d.get("revenue", ""), cost=d.get("cost", ""), profit_col=d.get("profit_col", ""),
            period=Period(d.get("period", "last_month")), top_n=d.get("top_n", 3),
            freq=Freq(d.get("freq", "weekly")), weekday=d.get("weekday", 0), at=d.get("at", "09:00"),
            recipients=Recipients(d.get("recipients", "me")), team=d.get("team", ""),
            owner=d.get("owner", 0),
            paused=d.get("paused", False), id=an_id, extra=d.get("extra", {}),
        )

    def columns_used(self) -> set[str]:
        used = {self.date_column}
        if self.kind is Kind.COMPARE:
            if self.metric.column:
                used.add(self.metric.column)
        else:
            used |= {c for c in (self.item, self.qty) if c}
            if self.profit is Profit.DIFF:
                used |= {self.revenue, self.cost}
            elif self.profit is Profit.COLUMN:
                used.add(self.profit_col)
        return used


def _check_columns(an: Analysis, table: Table | None) -> None:
    if table is None:
        raise ReportError(an.sheet, kind="sheet")
    for col in an.columns_used():
        if table.column(col) is None:
            raise ReportError(col)


def _rows_between(table: Table, date_column: str, start: date, end: date) -> list[list[str]]:
    di = table.column(date_column)
    return [r for r in table.rows if (d := parse_date(r[di])) is not None and start <= d <= end]


def _num(table: Table, row: list[str], column: str) -> float:
    return table.number(row[table.column(column)]) or 0.0


# ---------------------------------------------------------------- compare

def unit_ranges(unit: Unit, today: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """(current period, previous period), each as first and last day."""
    if unit is Unit.DAY:
        cur = today - timedelta(days=1)
        return (cur, cur), (cur - timedelta(days=1), cur - timedelta(days=1))
    if unit is Unit.WEEK:
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
        return (start, end), (start - timedelta(days=7), start - timedelta(days=1))
    cur = period_range(Period.LAST_MONTH, today)
    return cur, period_range(Period.LAST_MONTH, cur[0])


@dataclass
class CompareResult:
    current: tuple[date, date]
    previous: tuple[date, date]
    now: float | None
    before: float | None
    now_rows: int
    before_rows: int
    change: float | None          # percent; None when the previous period is zero or empty
    leaders: list[tuple[str, float]]
    currency: str = ""            # "$" when the column shows money


def compare(an: Analysis, table: Table | None, today: date) -> CompareResult:
    _check_columns(an, table)
    cur, prev = unit_ranges(an.unit, today)

    def value(rows: list[list[str]]) -> float | None:
        if an.metric.func is Func.COUNT:
            return float(len(rows))
        return _aggregate(an.metric.func, [_num(table, r, an.metric.column) for r in rows])

    now_rows = _rows_between(table, an.date_column, *cur)
    before_rows = _rows_between(table, an.date_column, *prev)
    now, before = value(now_rows), value(before_rows)
    change = None
    if now is not None and before:
        change = (now - before) / abs(before) * 100

    # leaders of the current period by the first text column (usually the product)
    leaders: list[tuple[str, float]] = []
    names = [c for c in text_columns(table) if c != an.date_column]
    if names and now_rows:
        ni = table.column(names[0])
        sums: dict[str, float] = defaultdict(float)
        for r in now_rows:
            sums[r[ni].strip() or "—"] += 1 if an.metric.func is Func.COUNT else _num(table, r, an.metric.column)
        leaders = sorted(sums.items(), key=lambda x: x[1], reverse=True)[:3]
    money = currency_of(table, an.metric.column) if an.metric.func is not Func.COUNT else ""
    return CompareResult(cur, prev, now, before, len(now_rows), len(before_rows), change, leaders, money)


def worth_sending(an: Analysis, res: CompareResult) -> bool:
    """Scheduled runs speak only when there's something to say."""
    if an.when is When.ALWAYS:
        return True
    if res.change is None:
        return False
    if an.when is When.DROP:
        return res.change <= -an.pct
    return abs(res.change) >= an.pct


# ---------------------------------------------------------------- top items

@dataclass
class Star:
    name: str
    qty_rank: int
    profit_rank: int
    qty: float
    margin: float | None           # percent of revenue; None without revenue
    best_seller: str
    best_seller_profit: float


@dataclass
class TopResult:
    start: date
    end: date
    rows: int
    by_qty: list[tuple[str, float]]
    by_profit: list[tuple[str, float]]       # empty when profit isn't counted
    star: Star | None
    currency: str = ""                       # of the profit, when the sheet shows money


STAR_GAP = 3   # a star ranks at least this many places higher by profit than by quantity


def top(an: Analysis, table: Table | None, today: date) -> TopResult:
    _check_columns(an, table)
    start, end = period_range(an.period, today)
    rows = _rows_between(table, an.date_column, start, end)
    ii = table.column(an.item)
    qty: dict[str, float] = defaultdict(float)
    profit: dict[str, float] = defaultdict(float)
    revenue: dict[str, float] = defaultdict(float)
    for r in rows:
        name = r[ii].strip() or "—"
        qty[name] += _num(table, r, an.qty) if an.qty else 1
        if an.profit is Profit.DIFF:
            rev = _num(table, r, an.revenue)
            profit[name] += rev - _num(table, r, an.cost)
            revenue[name] += rev
        elif an.profit is Profit.COLUMN:
            profit[name] += _num(table, r, an.profit_col)

    by_qty = sorted(qty.items(), key=lambda x: x[1], reverse=True)
    by_profit = sorted(profit.items(), key=lambda x: x[1], reverse=True)
    star = None
    if by_profit:
        qty_rank = {name: i + 1 for i, (name, _) in enumerate(by_qty)}
        candidates = [(qty_rank[name] - (i + 1), i + 1, name)
                      for i, (name, _) in enumerate(by_profit[:an.top_n])
                      if qty_rank[name] - (i + 1) >= STAR_GAP]
        if candidates:
            gap, p_rank, name = max(candidates)
            best = by_qty[0][0]
            margin = profit[name] / revenue[name] * 100 if revenue.get(name) else None
            star = Star(name, qty_rank[name], p_rank, qty[name], margin, best, profit[best])
    money = currency_of(table, an.revenue if an.profit is Profit.DIFF else an.profit_col) \
        if an.profit is not Profit.NONE else ""
    return TopResult(start, end, len(rows), by_qty, by_profit, star, money)
