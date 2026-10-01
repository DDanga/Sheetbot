"""Entry point: python -m sheetbot"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram.error import InvalidToken, NetworkError

from . import __version__
from .bot import build_app
from .config import ConfigError, load_config
from .sheets import SheetsClient, SheetsError
from .storage import Storage


def setup_logging(data_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(data_dir / "sheetbot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file])
    # the HTTP client logs every request to Telegram — too noisy for the console
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def main() -> int:
    try:
        config = load_config(Path.cwd())
    except ConfigError as e:
        print(f"\n⚠ Settings problem:\n{e}\n")
        return 1

    setup_logging(config.data_dir)
    log = logging.getLogger("sheetbot")
    log.info("Sheetbot %s starting", __version__)

    sheets = SheetsClient(config.key_file, config.spreadsheet_id)
    try:
        info = sheets.read_all()
        log.info("Spreadsheet “%s”: %s", info.title, ", ".join(s.name for s in info.sheets))
    except SheetsError as e:
        # not fatal: the bot starts anyway and explains the problem in /status
        log.warning("Spreadsheet not reachable yet (%s). Send /status to the bot for details.", e.problem.value)

    storage = Storage(config.data_dir / "sheetbot.sqlite")
    try:
        app = build_app(config, storage, sheets)
        log.info("Connecting to Telegram…")
        app.run_polling(allowed_updates=["message", "callback_query"])
    except InvalidToken:
        print("\n⚠ Telegram rejected the bot token. Copy it again from @BotFather into .env.\n")
        return 1
    except NetworkError:
        print("\n⚠ Can't reach Telegram. Check the internet connection and start the bot again.\n")
        return 1
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
