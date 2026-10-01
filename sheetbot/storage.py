"""The bot's own memory: who can use it, in which language, and the rules.

A single SQLite file in data/: users, rules, reports and analyses, which rows each rule has already
reported, and the last copy of every sheet. Everything survives a restart, so the
bot never repeats old alerts after being switched off and on.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .analysis import Analysis
from .reports import Report
from .rules import Rule

ADMIN = "admin"
MEMBER = "member"
USER_COLUMNS = "chat_id, name, role, lang, team, muted_until"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    role     TEXT NOT NULL,
    lang     TEXT NOT NULL DEFAULT 'en',
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- rows a rule has already reported and that still meet its condition
CREATE TABLE IF NOT EXISTS rule_hits (
    rule_id INTEGER NOT NULL,
    row_key TEXT NOT NULL,
    PRIMARY KEY (rule_id, row_key)
);
-- the previous copy of each sheet, for "changed" rules
CREATE TABLE IF NOT EXISTS snapshots (
    sheet TEXT PRIMARY KEY,
    data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- one-time invite links and the requests that come from them
CREATE TABLE IF NOT EXISTS invites (
    token      TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status     TEXT NOT NULL DEFAULT 'open',   -- open → pending → done
    chat_id    INTEGER,
    name       TEXT NOT NULL DEFAULT ''
);
"""

# columns added after the first release; older databases get them on start
_ADDED_COLUMNS = {
    "users": {"team": "TEXT NOT NULL DEFAULT ''", "muted_until": "TEXT NOT NULL DEFAULT ''"},
}

INVITE_HOURS = 24


@dataclass(frozen=True)
class User:
    chat_id: int
    name: str
    role: str
    lang: str
    team: str = ""
    muted_until: str = ""      # ISO time, "forever", or "" when not muted

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    def muted(self, now: datetime) -> bool:
        if not self.muted_until:
            return False
        return self.muted_until == "forever" or datetime.fromisoformat(self.muted_until) > now


@dataclass(frozen=True)
class Invite:
    token: str
    created_by: int
    created_at: str
    status: str
    chat_id: int | None
    name: str


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        # write-ahead log: a crash or power cut mid-write can't corrupt the database
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            have = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        self.db.commit()

    def user(self, chat_id: int) -> User | None:
        row = self.db.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return User(*row) if row else None

    def has_admin(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM users WHERE role = ? LIMIT 1", (ADMIN,)
        ).fetchone() is not None

    def claim_admin(self, chat_id: int, name: str, lang: str) -> User | None:
        """The first person to press /start becomes the admin. Returns None if taken."""
        with self.db:
            if self.has_admin():
                return None
            self.db.execute(
                "INSERT OR REPLACE INTO users (chat_id, name, role, lang) VALUES (?, ?, ?, ?)",
                (chat_id, name, ADMIN, lang),
            )
        return self.user(chat_id)

    def set_lang(self, chat_id: int, lang: str) -> None:
        with self.db:
            self.db.execute("UPDATE users SET lang = ? WHERE chat_id = ?", (lang, chat_id))

    # ---- rules ----

    def save_rule(self, rule: Rule, kind: str = "notify") -> Rule:
        data = json.dumps(rule.to_dict(), ensure_ascii=False)
        with self.db:
            if rule.id is None:
                cur = self.db.execute("INSERT INTO rules (kind, data) VALUES (?, ?)", (kind, data))
                rule.id = cur.lastrowid
            else:
                self.db.execute("UPDATE rules SET data = ? WHERE id = ?", (data, rule.id))
        return rule

    def rule(self, rule_id: int) -> Rule | None:
        row = self.db.execute("SELECT id, data FROM rules WHERE id = ? AND kind = 'notify'",
                              (rule_id,)).fetchone()
        return Rule.from_dict(json.loads(row[1]), row[0]) if row else None

    def rules(self) -> list[Rule]:
        rows = self.db.execute("SELECT id, data FROM rules WHERE kind = 'notify' ORDER BY id").fetchall()
        return [Rule.from_dict(json.loads(d), i) for i, d in rows]

    # ---- reports and analyses: the same table, told apart by kind ----

    def _save(self, kind: str, item) -> None:
        data = json.dumps(item.to_dict(), ensure_ascii=False)
        with self.db:
            if item.id is None:
                cur = self.db.execute("INSERT INTO rules (kind, data) VALUES (?, ?)", (kind, data))
                item.id = cur.lastrowid
            else:
                self.db.execute("UPDATE rules SET data = ? WHERE id = ?", (data, item.id))

    def _one(self, kind: str, item_id: int, cls):
        row = self.db.execute("SELECT id, data FROM rules WHERE id = ? AND kind = ?", (item_id, kind)).fetchone()
        return cls.from_dict(json.loads(row[1]), row[0]) if row else None

    def _all(self, kind: str, cls) -> list:
        rows = self.db.execute("SELECT id, data FROM rules WHERE kind = ? ORDER BY id", (kind,)).fetchall()
        return [cls.from_dict(json.loads(d), i) for i, d in rows]

    def save_report(self, report: Report) -> Report:
        self._save("report", report)
        return report

    def report(self, report_id: int) -> Report | None:
        return self._one("report", report_id, Report)

    def reports(self) -> list[Report]:
        return self._all("report", Report)

    def save_analysis(self, an: Analysis) -> Analysis:
        self._save("analysis", an)
        return an

    def analysis(self, an_id: int) -> Analysis | None:
        return self._one("analysis", an_id, Analysis)

    def analyses(self) -> list[Analysis]:
        return self._all("analysis", Analysis)

    def delete_rule(self, rule_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self.db.execute("DELETE FROM rule_hits WHERE rule_id = ?", (rule_id,))

    # ---- what each rule has already reported ----

    def hits(self, rule_id: int) -> set[str]:
        rows = self.db.execute("SELECT row_key FROM rule_hits WHERE rule_id = ?", (rule_id,))
        return {r[0] for r in rows}

    def set_hits(self, rule_id: int, keys: set[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM rule_hits WHERE rule_id = ?", (rule_id,))
            self.db.executemany("INSERT INTO rule_hits (rule_id, row_key) VALUES (?, ?)",
                                [(rule_id, k) for k in keys])

    def forget_hits(self, rule_id: int) -> None:
        """After the condition changes, the rule starts over and reports everything that matches."""
        self.set_hits(rule_id, set())

    # ---- sheet copies ----

    def snapshot(self, sheet: str) -> list[list[str]] | None:
        row = self.db.execute("SELECT data FROM snapshots WHERE sheet = ?", (sheet,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_snapshot(self, sheet: str, values: list[list[str]]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO snapshots (sheet, data) VALUES (?, ?)",
                            (sheet, json.dumps(values, ensure_ascii=False)))

    # ---- small key-value facts (last check time, is the sheet down) ----

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def users(self) -> list[User]:
        rows = self.db.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY added_at").fetchall()
        return [User(*r) for r in rows]

    def add_user(self, chat_id: int, name: str, role: str, team: str, lang: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO users (chat_id, name, role, lang, team) VALUES (?, ?, ?, ?, ?)",
                (chat_id, name, role, lang, team))

    def update_user(self, chat_id: int, **fields) -> None:
        allowed = {"role", "team", "muted_until", "name"}
        assert set(fields) <= allowed, fields
        with self.db:
            for key, value in fields.items():
                self.db.execute(f"UPDATE users SET {key} = ? WHERE chat_id = ?", (value, chat_id))

    def remove_user(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))

    def admins(self) -> list[User]:
        return [u for u in self.users() if u.is_admin]

    def teams(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT team FROM users WHERE team != '' ORDER BY team")
        return [r[0] for r in rows]

    # ---- invites ----

    def create_invite(self, token: str, created_by: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO invites (token, created_by) VALUES (?, ?)", (token, created_by))

    def invite(self, token: str) -> Invite | None:
        row = self.db.execute(
            "SELECT token, created_by, created_at, status, chat_id, name FROM invites WHERE token = ?",
            (token,)).fetchone()
        return Invite(*row) if row else None

    def invite_fresh(self, inv: Invite) -> bool:
        created = datetime.fromisoformat(inv.created_at).replace(tzinfo=UTC)
        return datetime.now(UTC) - created < timedelta(hours=INVITE_HOURS)

    def set_invite(self, token: str, status: str, chat_id: int | None = None, name: str = "") -> None:
        with self.db:
            if chat_id is None:
                self.db.execute("UPDATE invites SET status = ? WHERE token = ?", (status, token))
            else:
                self.db.execute("UPDATE invites SET status = ?, chat_id = ?, name = ? WHERE token = ?",
                                (status, chat_id, name, token))

    def backup(self, folder: Path, keep: int = 7, today: str | None = None) -> Path | None:
        """One copy a day into folder/, the last `keep` days kept. Returns the new file, if any."""
        folder.mkdir(parents=True, exist_ok=True)
        today = today or datetime.now().astimezone().strftime("%Y-%m-%d")   # local date
        target = folder / f"sheetbot-{today}.sqlite"
        if target.exists():
            return None
        copy = sqlite3.connect(target)
        with copy:
            self.db.backup(copy)
        copy.close()
        for old in sorted(folder.glob("sheetbot-*.sqlite"))[:-keep]:
            old.unlink()
        return target

    def close(self) -> None:
        self.db.close()
