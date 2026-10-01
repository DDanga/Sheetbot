"""Notification rules: what they are, how they check a sheet, how they read aloud.

A rule never mentions cell addresses. It names a sheet and columns, so inserting
or moving columns in the spreadsheet doesn't break it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum

from .numbers import parse_number
from .sheets import Table


class Op(str, Enum):
    LT = "lt"
    LE = "le"
    EQ = "eq"
    NE = "ne"
    GE = "ge"
    GT = "gt"
    CHANGED = "changed"
    EMPTY = "empty"
    CONTAINS = "contains"

    @property
    def symbol(self) -> str:
        return {"lt": "<", "le": "≤", "eq": "=", "ne": "≠", "ge": "≥", "gt": ">"}.get(self.value, "")

    @property
    def needs_target(self) -> bool:
        """Does the condition compare against something (a number, text or another column)?"""
        return self not in (Op.CHANGED, Op.EMPTY)

    @property
    def numeric(self) -> bool:
        return self in (Op.LT, Op.LE, Op.GE, Op.GT)


class Target(str, Enum):
    VALUE = "value"     # a number or text typed by the user
    COLUMN = "column"   # another column of the same row


class Recipients(str, Enum):
    ME = "me"
    ALL = "all"
    TEAM = "team"   # everyone in the team named in the rule's `team`


@dataclass
class Rule:
    sheet: str
    column: str
    op: Op
    target: Target | None = None
    target_value: str = ""          # the typed value, or the other column's name
    label: str = ""                 # column that names the row in messages; "" = row number
    recipients: Recipients = Recipients.ME
    team: str = ""
    template: str = ""              # custom message; "" = the default one
    notify_resolved: bool = True    # also say when a row is back to normal
    owner: int = 0
    paused: bool = False
    id: int | None = None
    extra: dict = field(default_factory=dict)

    # ---- storage ----

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(op=self.op.value, recipients=self.recipients.value,
                 target=self.target.value if self.target else None)
        d.pop("id")
        return d

    @classmethod
    def from_dict(cls, d: dict, rule_id: int | None = None) -> "Rule":
        return cls(
            sheet=d["sheet"], column=d["column"], op=Op(d["op"]),
            target=Target(d["target"]) if d.get("target") else None,
            target_value=d.get("target_value", ""), label=d.get("label", ""),
            recipients=Recipients(d.get("recipients", "me")), team=d.get("team", ""),
            template=d.get("template", ""),
            notify_resolved=d.get("notify_resolved", True),
            owner=d.get("owner", 0), paused=d.get("paused", False), id=rule_id,
            extra=d.get("extra", {}),
        )

    def columns_used(self) -> set[str]:
        used = {self.column}
        if self.target is Target.COLUMN:
            used.add(self.target_value)
        if self.label:
            used.add(self.label)
        used.update(placeholders(self.template))
        return used


@dataclass(frozen=True)
class Match:
    row: int                     # 1-based row number as seen in Google Sheets (header is row 1)
    name: str                    # how the row is called in messages
    values: dict[str, str]       # the whole row by column name
    key: str = ""                # stable identity of the row between checks
    old: str = ""                # previous value, for "changed" rules


class RuleError(Exception):
    """The sheet no longer fits the rule (a column or the sheet itself is gone)."""

    def __init__(self, missing: str, kind: str = "column"):
        super().__init__(missing)
        self.missing = missing
        self.kind = kind


def _compare(op: Op, left: str, right: str, decimal: str | None = None) -> bool:
    ln, rn = parse_number(left, decimal), parse_number(right, decimal)
    if op.numeric:
        if ln is None or rn is None:
            return False                # text can't be "less than": skip such rows
        return {Op.LT: ln < rn, Op.LE: ln <= rn, Op.GE: ln >= rn, Op.GT: ln > rn}[op]
    if op in (Op.EQ, Op.NE):
        same = (ln == rn) if (ln is not None and rn is not None) else \
            left.strip().casefold() == right.strip().casefold()
        return same if op is Op.EQ else not same
    if op is Op.CONTAINS:
        return right.strip().casefold() in left.casefold()
    return False


def check(rule: Rule, table: Table | None, previous: Table | None = None) -> list[Match]:
    """Rows of the table that meet the rule's condition."""
    if table is None:
        raise RuleError(rule.sheet, kind="sheet")
    for col in rule.columns_used():
        if table.column(col) is None:
            raise RuleError(col)

    ci = table.column(rule.column)
    ti = table.column(rule.target_value) if rule.target is Target.COLUMN else None
    li = table.column(rule.label) if rule.label else None
    before = _index_rows(previous, rule) if rule.op is Op.CHANGED else {}

    matches = []
    keys = _row_keys(table, li)
    for n, row in enumerate(table.rows, start=2):
        if not any(c.strip() for c in row):
            continue
        cell = row[ci]
        key = keys[n]
        old = ""
        if rule.op is Op.EMPTY:
            hit = not cell.strip()
        elif rule.op is Op.CHANGED:
            old = before.get(key, cell)
            hit = key in before and old != cell
        else:
            if not cell.strip():
                continue
            right = row[ti] if ti is not None else rule.target_value
            if not str(right).strip():
                continue
            hit = _compare(rule.op, cell, right, table.decimal)
        if hit:
            name = row[li].strip() if li is not None else ""
            matches.append(Match(row=n, name=name or f"#{n}", values=dict(zip(table.headers, row)),
                                 key=key, old=old))
    return matches


def _row_keys(table: Table, label_index: int | None) -> dict[int, str]:
    """A stable name for every row, by sheet row number.

    Rows are recognized by their name when there is one, so sorting the sheet
    doesn't look like "everything changed". Repeated names get a counter.
    """
    keys, seen = {}, {}
    for n, row in enumerate(table.rows, start=2):
        name = row[label_index].strip() if label_index is not None else ""
        if not name:
            keys[n] = f"#{n}"
            continue
        seen[name] = seen.get(name, 0) + 1
        keys[n] = name if seen[name] == 1 else f"{name} ·{seen[name]}"
    return keys


def _index_rows(table: Table | None, rule: Rule) -> dict[str, str]:
    if table is None or table.column(rule.column) is None:
        return {}
    ci = table.column(rule.column)
    li = table.column(rule.label) if rule.label else None
    keys = _row_keys(table, li)
    return {keys[n]: r[ci] for n, r in enumerate(table.rows, start=2)}


# ---- message text ----

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def placeholders(template: str) -> set[str]:
    return {m.strip() for m in _PLACEHOLDER.findall(template or "")}


def default_template(rule: Rule) -> str:
    """'⚠ {Товар}: Остаток {Остаток} (Минимум {Минимум})'"""
    head = f"{{{rule.label}}}" if rule.label else "{#}"
    body = f"{rule.column} {{{rule.column}}}"
    if rule.target is Target.COLUMN:
        body += f" ({rule.target_value} {{{rule.target_value}}})"
    return f"⚠ {head}: {body}"


def render(rule: Rule, match: Match) -> str:
    if rule.op is Op.CHANGED and not rule.template:
        return f"✏ {match.name}: {rule.column} {match.old or '—'} → {match.values.get(rule.column) or '—'}"
    template = rule.template or default_template(rule)
    values = dict(match.values, **{"#": match.name})

    def sub(m: re.Match) -> str:
        key = m.group(1).strip()
        return values.get(key, m.group(0))
    return _PLACEHOLDER.sub(sub, template)
