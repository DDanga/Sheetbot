"""Reading the Google Sheet.

gspread is synchronous, so the bot calls these methods through asyncio.to_thread
to keep Telegram responsive while Google answers.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import gspread
from google.auth.exceptions import GoogleAuthError, TransportError

from .numbers import decimal_for, parse_number

log = logging.getLogger(__name__)


class Problem(Enum):
    BAD_KEY = "bad_key"          # the JSON key is broken or revoked
    NOT_SHARED = "not_shared"    # the sheet isn't shared with the service account
    NOT_FOUND = "not_found"      # wrong link, or the sheet was deleted
    NETWORK = "network"          # Google didn't answer; worth retrying


class SheetsError(Exception):
    def __init__(self, problem: Problem, detail: str = ""):
        super().__init__(f"{problem.value}: {detail}")
        self.problem = problem
        self.detail = detail


@dataclass(frozen=True)
class SheetInfo:
    name: str
    headers: list[str]
    rows: int  # data rows under the header, empty rows at the bottom not counted


@dataclass(frozen=True)
class SpreadsheetInfo:
    title: str
    sheets: list[SheetInfo]


@dataclass(frozen=True)
class Table:
    """One sheet as the rules see it: column names and data rows of equal length."""
    name: str
    headers: list[str]
    rows: list[list[str]]
    decimal: str | None = None     # the sheet's decimal separator, when Google told us

    def column(self, name: str) -> int | None:
        try:
            return self.headers.index(name)
        except ValueError:
            return None

    def number(self, text: str) -> float | None:
        return parse_number(text, self.decimal)


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _unique_headers(raw: list[str]) -> list[str]:
    """Every column gets a name a button can show and a rule can point at.

    A column without a title is called by its letter, «[C]»; a repeated title gets
    a number, «Price (2)», so both columns can be picked.
    """
    headers, seen = [], set()
    for i, h in enumerate(raw):
        name = h or f"[{column_letter(i)}]"
        base, n = name, 1
        while name in seen:
            n += 1
            name = f"{base} ({n})"
        seen.add(name)
        headers.append(name)
    return headers


def table_from(name: str, values: list[list[str]], decimal: str | None = None) -> Table:
    raw = [str(h).strip() for h in values[0]] if values else []
    while raw and not raw[-1]:
        raw.pop()
    width = len(raw)
    rows = [[str(c) for c in r[:width]] + [""] * (width - len(r)) for r in values[1:]]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return Table(name=name, headers=_unique_headers(raw), rows=rows, decimal=decimal)


def service_account_email(key_file: Path) -> str:
    """The robot's address — the owner shares the sheet with it."""
    try:
        return json.loads(key_file.read_text(encoding="utf-8")).get("client_email", "")
    except (OSError, ValueError):
        return ""


def summarize(title: str, grids: dict[str, list[list[str]]]) -> SpreadsheetInfo:
    """Turn raw cell values into sheet names, column names and row counts."""
    sheets = []
    for name, values in grids.items():
        table = table_from(name, values)
        sheets.append(SheetInfo(name=name, headers=table.headers, rows=len(table.rows)))
    return SpreadsheetInfo(title=title, sheets=sheets)


def quote_sheet(name: str) -> str:
    """A sheet name as Google's range syntax wants it: 'O''Brien' for O'Brien."""
    return "'" + name.replace("'", "''") + "'"


# read-only access: the bot can't change the sheet, even by mistake
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetsClient:
    # the rule builder asks for the sheet on every button press; within this many
    # seconds the last read is reused instead of calling Google again
    CACHE_SECONDS = 30
    TIMEOUT_SECONDS = 30
    # a hiccup (no network, Google busy or rate-limiting) is retried after these pauses
    RETRY_PAUSES = (2, 5)

    def __init__(self, key_file: Path, spreadsheet_id: str, sleep=time.sleep):
        self.key_file = key_file
        self.spreadsheet_id = spreadsheet_id
        self._sleep = sleep
        self._book: gspread.Spreadsheet | None = None
        self._cache: tuple[float, str, dict[str, list[list[str]]]] | None = None
        self.decimal: str | None = None   # from the spreadsheet's locale, after the first read

    def _open(self) -> gspread.Spreadsheet:
        if self._book is None:
            try:
                client = gspread.service_account(filename=str(self.key_file), scopes=SCOPES)
                if hasattr(client, "set_timeout"):
                    client.set_timeout(self.TIMEOUT_SECONDS)
                self._book = client.open_by_key(self.spreadsheet_id)
            except gspread.exceptions.SpreadsheetNotFound:
                # Google answers "not found" both for a wrong link and for a sheet
                # that exists but isn't shared with the robot
                raise SheetsError(Problem.NOT_SHARED) from None
            except TransportError as e:
                # no internet while getting a Google token — not the key's fault
                raise SheetsError(Problem.NETWORK, str(e)) from None
            except (GoogleAuthError, ValueError, KeyError) as e:
                raise SheetsError(Problem.BAD_KEY, str(e)) from None
            except gspread.exceptions.APIError as e:
                raise _api_problem(e) from None
            except OSError as e:
                raise SheetsError(Problem.NETWORK, str(e)) from None
        return self._book

    def read_all(self, max_age: float = 0) -> SpreadsheetInfo:
        """Names, columns and row counts of every sheet."""
        return summarize(*self.read_grids(max_age))

    def table(self, name: str, max_age: float = CACHE_SECONDS) -> Table | None:
        _, grids = self.read_grids(max_age)
        return table_from(name, grids[name], self.decimal) if name in grids else None

    def tables(self, max_age: float = CACHE_SECONDS) -> dict[str, Table]:
        _, grids = self.read_grids(max_age)
        return {name: table_from(name, values, self.decimal) for name, values in grids.items()}

    def read_grids(self, max_age: float = 0) -> tuple[str, dict[str, list[list[str]]]]:
        """All cell values of all sheets in one request (reused for max_age seconds).

        Network trouble is retried a couple of times before giving up; a broken key
        or a missing share is not, because waiting won't fix those.
        """
        if self._cache and time.monotonic() - self._cache[0] <= max_age:
            return self._cache[1], self._cache[2]
        for pause in (*self.RETRY_PAUSES, None):
            try:
                return self._read_once()
            except SheetsError as err:
                if err.problem is not Problem.NETWORK or pause is None:
                    raise
                log.info("Google didn't answer (%s), retrying in %s s", err.detail[:80], pause)
                self._sleep(pause)
        raise AssertionError("unreachable")

    def _read_once(self) -> tuple[str, dict[str, list[list[str]]]]:
        book = self._open()
        try:
            names = [ws.title for ws in book.worksheets()]
            ranges = [quote_sheet(n) for n in names]
            result = book.values_batch_get(ranges) if ranges else {"valueRanges": []}
        except gspread.exceptions.APIError as e:
            self._book = None
            raise _api_problem(e) from None
        except OSError as e:
            self._book = None
            raise SheetsError(Problem.NETWORK, str(e)) from None
        grids = {
            name: vr.get("values", [])
            for name, vr in zip(names, result.get("valueRanges", []))
        }
        self.decimal = decimal_for(getattr(book, "locale", None))
        self._cache = (time.monotonic(), book.title, grids)
        return book.title, grids


def _api_problem(e: gspread.exceptions.APIError) -> SheetsError:
    code = getattr(e, "code", None) or getattr(getattr(e, "response", None), "status_code", 0)
    if code == 403:
        return SheetsError(Problem.NOT_SHARED, str(e))
    if code == 404:
        return SheetsError(Problem.NOT_FOUND, str(e))
    return SheetsError(Problem.NETWORK, str(e))
