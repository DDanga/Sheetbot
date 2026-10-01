"""Reading numbers the way people and Google Sheets write them.

A cell arrives as the text the sheet shows, so "1 234,5 ₽", "1,234.50", "(300)"
and "12%" all have to become numbers, while "01.09.2026", "#N/A" or "NaN" must not.
"""

from __future__ import annotations

import math
import re

_JUNK = re.compile(r"[\s  '’₽$€£¥%]|руб\.?|шт\.?", re.IGNORECASE)
_PLAIN = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_MINUSES = str.maketrans({"−": "-", "–": "-", "‒": "-"})

# languages whose spreadsheets write 1.5 with a dot; everyone else (ru, de, fr, es…) uses a comma
_DOT_LANGUAGES = {"en", "ja", "zh", "ko", "he", "th", "hi", "ms", "fil", "ga", "mt", "ta", "te", "ur"}
_DOT_LOCALES = {"de_CH", "it_CH", "fr_CH", "es_MX", "es_US", "es_PR"}


def decimal_for(locale: str | None) -> str | None:
    """The decimal separator of a spreadsheet locale like 'ru_RU' or 'en_US'; None if unknown."""
    if not locale:
        return None
    if locale in _DOT_LOCALES or locale.split("_")[0] in _DOT_LANGUAGES:
        return "."
    return ","


def _grouped(text: str, sep: str) -> bool:
    """'1,234,567' is thousands grouping; '1,2,3' is not a number at all."""
    return re.fullmatch(r"\d{1,3}(?:" + re.escape(sep) + r"\d{3})+", text) is not None


def parse_number(text: str, decimal: str | None = None) -> float | None:
    """'1 234,5 ₽' → 1234.5, '1,234.5' → 1234.5, '(300)' → -300, '12%' → 12. Not a number → None.

    `decimal` is the sheet's decimal separator when known. It only matters for a
    lone separator before exactly three digits: '1,234' is 1234 in an American
    sheet and 1.234 in a Russian one. Without it, a lone separator is a decimal point.
    """
    s = _JUNK.sub("", str(text)).translate(_MINUSES)
    negative = len(s) > 2 and s[0] == "(" and s[-1] == ")"   # accounting style: (300) = -300
    if negative:
        s = s[1:-1]
    if s[:1] in ("-", "+"):
        negative, s = negative ^ (s[0] == "-"), s[1:]
    if not s:
        return None

    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        whole, _, frac = s.rpartition(dec)
        group = "." if dec == "," else ","
        if group in whole and not _grouped(whole, group):
            return None
        s = whole.replace(group, "") + "." + frac
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        if s.count(sep) > 1:
            if not _grouped(s, sep):
                return None
            s = s.replace(sep, "")
        else:
            whole, frac = s.split(sep)
            thousands = (decimal is not None and sep != decimal and len(frac) == 3
                         and whole.isdigit() and 1 <= len(whole) <= 3)
            s = whole + frac if thousands else whole + "." + frac

    if not _PLAIN.fullmatch(s):
        return None
    value = float(s)
    if not math.isfinite(value):
        return None
    return -value if negative else value
