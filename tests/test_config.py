import pytest

from sheetbot.config import ConfigError, load_config, spreadsheet_id_from

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-ab"


def write_env(tmp_path, **values):
    base = {
        "TELEGRAM_BOT_TOKEN": "123456:ABC-token",
        "GOOGLE_KEY_FILE": "google-key.json",
        "SPREADSHEET": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
        "TIMEZONE": "Asia/Tokyo",
    }
    base.update(values)
    (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in base.items()), encoding="utf-8")


def test_id_from_link_and_bare_id():
    assert spreadsheet_id_from(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit") == SHEET_ID
    assert spreadsheet_id_from(SHEET_ID) == SHEET_ID


def test_bad_link_is_explained():
    with pytest.raises(ConfigError, match="Google Sheets link"):
        spreadsheet_id_from("https://example.com/table")


def test_full_config(tmp_path):
    (tmp_path / "google-key.json").write_text("{}")
    write_env(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.spreadsheet_id == SHEET_ID
    assert cfg.timezone.key == "Asia/Tokyo"
    assert (tmp_path / "data").is_dir()


def test_doubled_json_extension_is_found(tmp_path):
    # Windows hides extensions, so people end up with google-key.json.json
    (tmp_path / "google-key.json.json").write_text("{}")
    write_env(tmp_path)
    assert load_config(tmp_path).key_file.name == "google-key.json.json"


def test_missing_env(tmp_path):
    with pytest.raises(ConfigError, match=".env.example"):
        load_config(tmp_path)


@pytest.mark.parametrize("field,value,message", [
    ("TELEGRAM_BOT_TOKEN", "", "TELEGRAM_BOT_TOKEN is empty"),
    ("TELEGRAM_BOT_TOKEN", "nonsense", "doesn't look like a bot token"),
    ("SPREADSHEET", "", "SPREADSHEET is empty"),
    ("TIMEZONE", "Mars/Olympus", "unknown time zone"),
    ("GOOGLE_KEY_FILE", "nope.json", "file not found"),
    ("POLL_MINUTES", "0", "whole minutes"),
    ("POLL_MINUTES", "often", "whole minutes"),
])
def test_each_problem_has_a_clear_message(tmp_path, monkeypatch, field, value, message):
    for key in ("TELEGRAM_BOT_TOKEN", "GOOGLE_KEY_FILE", "SPREADSHEET", "TIMEZONE", "POLL_MINUTES"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "google-key.json").write_text("{}")
    write_env(tmp_path, **{field: value})
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)
