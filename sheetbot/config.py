"""Settings from the .env file, checked once at startup.

Every problem is reported in plain words, because the person reading the error
is a shop owner, not a developer.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# .../spreadsheets/d/<ID>/edit#gid=0  →  <ID>
_SHEET_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_SHEET_ID = re.compile(r"^[a-zA-Z0-9_-]{20,}$")


class ConfigError(Exception):
    """A setting is missing or wrong. The message is shown to the user as is."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    key_file: Path
    spreadsheet_id: str
    timezone: ZoneInfo
    data_dir: Path
    poll_minutes: int = 5


def spreadsheet_id_from(value: str) -> str:
    """Accept a full Google Sheets link or a bare ID."""
    value = value.strip()
    match = _SHEET_URL.search(value)
    if match:
        return match.group(1)
    if _SHEET_ID.match(value):
        return value
    raise ConfigError(
        "SPREADSHEET: this doesn't look like a Google Sheets link.\n"
        "Open the sheet in the browser and copy the whole address, "
        "it starts with https://docs.google.com/spreadsheets/d/…"
    )


def _find_key_file(raw: str, base: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    if path.is_file():
        return path
    # Windows hides extensions, so "google-key.json" often ends up as "google-key.json.json"
    doubled = path.with_name(path.name + ".json")
    if doubled.is_file():
        return doubled
    raise ConfigError(
        f"GOOGLE_KEY_FILE: file not found: {path}\n"
        "Put the JSON key you downloaded from Google Cloud into the bot folder "
        "and check that the name matches."
    )


def load_config(base: Path | None = None) -> Config:
    base = base or Path.cwd()
    env_file = base / ".env"
    if not env_file.is_file():
        raise ConfigError(
            f"No .env file in {base}.\n"
            "Copy .env.example to .env and fill in the values."
        )
    load_dotenv(env_file, override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is empty. Paste the token from @BotFather into .env.")
    if ":" not in token:
        raise ConfigError("TELEGRAM_BOT_TOKEN doesn't look like a bot token (it should contain ':').")

    sheet = os.getenv("SPREADSHEET", "").strip()
    if not sheet or "XXXXXXXX" in sheet:
        raise ConfigError("SPREADSHEET is empty. Paste the link to your Google Sheet into .env.")

    tz_name = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"TIMEZONE: unknown time zone '{tz_name}'. "
            "Use names like Europe/Moscow, Asia/Tokyo or UTC."
        ) from None

    poll_raw = os.getenv("POLL_MINUTES", "5").strip() or "5"
    if not poll_raw.isdigit() or not 1 <= int(poll_raw) <= 1440:
        raise ConfigError("POLL_MINUTES: how often to check the sheet, in whole minutes from 1 to 1440.")

    data_dir = base / "data"
    data_dir.mkdir(exist_ok=True)

    return Config(
        bot_token=token,
        key_file=_find_key_file(os.getenv("GOOGLE_KEY_FILE", "google-key.json"), base),
        spreadsheet_id=spreadsheet_id_from(sheet),
        timezone=tz,
        data_dir=data_dir,
        poll_minutes=int(poll_raw),
    )
