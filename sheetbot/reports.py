"""Scheduled reports: totals for a period, optionally split by a column.

Everything here is plain calculation, with no Telegram inside, so it can be
tested on the demo spreadsheet directly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from .rules import Recipients
from .sheets import Table


class Func(str, Enum):
    SUM = "sum"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    COUNT = "count"   # number of rows; needs no column


class Period(str, Enum):
    YESTERDAY = "yesterday"
    TODAY = "today"
    LAST_7 = "last7"            # the 7 days up to yesterday
    THIS_MONTH = "this_month"   # from the 1st up to today
    LAST_MONTH = "last_month"   # the previous calendar month


class Freq(str, Enum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"     # on `weekday` (0 = Monday)
    MONTHLY = "monthly"   # on the 1st


@dataclass
class Metric:
    func: Func
    column: str = ""      # empty for COUNT


@dataclass
class Report:
    sheet: str
    date_column: str
    period: Period = Period.YESTERDAY
    metrics: list[Metric] = field(default_factory=list)
    group_by: str = ""
    freq: Freq = Freq.DAILY
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
        d.update(period=self.period.value, freq=self.freq.value, recipients=self.recipients.value,
                 metrics=[{"func": m.func.value, "column": m.column} for m in self.metrics])
        d.pop("id")
        return d

    @classmethod
    def from_dict(cls, d: dict, report_id: int | None = None) -> "Report":
        return cls(
            sheet=d["sheet"], date_column=d["date_column"], period=Period(d["period"]),
            metrics=[Metric(Func(m["func"]), m.get("column", "")) for m in d.get("metrics", [])],
            group_by=d.get("group_by", ""), freq=Freq(d.get("freq", "daily")),
            weekday=d.get("weekday", 0), at=d.get("at", "09:00"),
            recipients=Recipients(d.get("recipients", "me")), team=d.get("team", ""),
            owner=d.get("owner", 0),
            paused=d.get("paused", False), id=report_id, extra=d.get("extra", {}),
        )

    def columns_used(self) -> set[str]:
        used = {self.date_column} | {m.column for m in self.metrics if m.column}
        if self.group_by:
            used.add(self.group_by)
        return used


# ---------------------------------------------------------------- dates

_DMY = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})")


def parse_date(text: str) -> date | None:
    """'01.09.2026', '2026-09-01', '9/1/2026' (US) or '13/9/2026'; a time after it is ignored."""
    s = str(text).strip()
    try:
        if m := _ISO.match(s):
            y, mo, d = map(int, m.groups())
        elif m := _DMY.match(s):
            d, mo, y = map(int, m.groups())
        elif m := _SLASH.match(s):
            a, b, y = map(int, m.groups())
            # Google shows dates the way the sheet's locale says; for slashes the US
            # order is the common one, unless the first number can't be a month
            mo, d = (b, a) if a > 12 else (a, b)
        else:
            return None
        if y < 100:
            y += 2000
        return date(y, mo, d)
    except ValueError:
        return None


def date_columns(table: Table) -> list[str]:
    """Columns where nearly every filled cell is a date."""
    found = []
    for i, header in enumerate(table.headers):
        cells = [r[i] for r in table.rows if r[i].strip()]
        if cells and sum(parse_date(c) is not None for c in cells) >= 0.8 * len(cells):
            found.append(header)
    return found


def number_columns(table: Table) -> list[str]:
    found = []
    for i, header in enumerate(table.headers):
        cells = [r[i] for r in table.rows if r[i].strip()]
        if cells and sum(table.number(c) is not None for c in cells) >= 0.8 * len(cells):
            if parse_date(cells[0]) is None:
                found.append(header)
    return found


def text_columns(table: Table) -> list[str]:
    skip = set(date_columns(table)) | set(number_columns(table))
    return [h for h in table.headers if h not in skip]


def period_range(period: Period, today: date) -> tuple[date, date]:
    """First and last day (inclusive) of the period, seen from `today`."""
    if period is Period.TODAY:
        return today, today
    if period is Period.YESTERDAY:
        y = today - timedelta(days=1)
        return y, y
    if period is Period.LAST_7:
        return today - timedelta(days=7), today - timedelta(days=1)
    if period is Period.THIS_MONTH:
        return today.replace(day=1), today
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


# ---------------------------------------------------------------- calculation

class ReportError(Exception):
    def __init__(self, missing: str, kind: str = "column"):
        super().__init__(missing)
        self.missing = missing
        self.kind = kind


@dataclass
class Result:
    start: date
    end: date
    rows: int
    totals: list[float | None]
    groups: list[tuple[str, list[float | None]]]   # biggest first
    currency: dict[str, str]                       # column → "₽" / "$" if its cells show one


def _aggregate(func: Func, values: list[float]) -> float | None:
    if func is Func.COUNT:
        return float(len(values))
    if not values:
        return None
    if func is Func.SUM:
        return sum(values)
    if func is Func.AVG:
        return sum(values) / len(values)
    return max(values) if func is Func.MAX else min(values)


_CURRENCY = re.compile(r"[₽$€£¥]")


def currency_of(table: Table, column: str) -> str:
    """'$' if the column's cells show dollars, and so on; '' for plain numbers."""
    ci = table.column(column)
    if ci is None:
        return ""
    return next((m.group(0) for r in table.rows if (m := _CURRENCY.search(r[ci]))), "")


def run(report: Report, table: Table | None, today: date) -> Result:
    if table is None:
        raise ReportError(report.sheet, kind="sheet")
    for col in report.columns_used():
        if table.column(col) is None:
            raise ReportError(col)

    start, end = period_range(report.period, today)
    di = table.column(report.date_column)
    gi = table.column(report.group_by) if report.group_by else None
    rows = [r for r in table.rows if (d := parse_date(r[di])) is not None and start <= d <= end]

    def column_values(subset: list[list[str]], column: str) -> list[float]:
        ci = table.column(column)
        return [v for r in subset if (v := table.number(r[ci])) is not None]

    def totals_for(subset: list[list[str]]) -> list[float | None]:
        return [_aggregate(m.func, [0.0] * len(subset) if m.func is Func.COUNT else column_values(subset, m.column))
                for m in report.metrics]

    groups: list[tuple[str, list[float | None]]] = []
    if gi is not None:
        buckets: dict[str, list[list[str]]] = defaultdict(list)
        for r in rows:
            buckets[r[gi].strip() or "—"].append(r)
        groups = [(name, totals_for(subset)) for name, subset in buckets.items()]
        groups.sort(key=lambda g: g[1][0] if g[1] and g[1][0] is not None else float("-inf"), reverse=True)

    currency = {}
    for m in report.metrics:
        if m.column and m.func is not Func.COUNT and (sym := currency_of(table, m.column)):
            currency[m.column] = sym
    return Result(start, end, len(rows), totals_for(rows), groups, currency)


# ---------------------------------------------------------------- schedule

def last_due(report: Report, now: datetime) -> datetime | None:
    """The most recent moment the report was due at or before `now` (both in local time)."""
    hh, mm = (int(x) for x in report.at.split(":"))
    for back in range(0, 32):
        day = (now - timedelta(days=back)).date()
        moment = datetime.combine(day, time(hh, mm), tzinfo=now.tzinfo)
        if moment > now or not _runs_on(report, day):
            continue
        return moment
    return None


def _runs_on(report: Report, day: date) -> bool:
    if report.freq is Freq.DAILY:
        return True
    if report.freq is Freq.WEEKDAYS:
        return day.weekday() < 5
    if report.freq is Freq.WEEKLY:
        return day.weekday() == report.weekday
    return day.day == 1


def valid_time(text: str) -> str | None:
    """'9:00', '09.30', '18 00' → '09:00', '09:30', '18:00'."""
    m = re.match(r"^\s*(\d{1,2})\s*[:.\s]\s*(\d{2})\s*$", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None

