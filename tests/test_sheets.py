import json

from sheetbot.sheets import service_account_email, summarize


def test_summary_counts_data_rows_and_trims_blanks():
    grids = {
        "Склад": [
            ["Товар", "Остаток", "Минимум", ""],
            ["Молоко", "3", "10"],
            ["Хлеб", "2", "12"],
            ["", "", ""],  # trailing empty row
        ],
        "Пустой": [],
    }
    info = summarize("Магазин", grids)
    stock, empty = info.sheets
    assert info.title == "Магазин"
    assert stock.headers == ["Товар", "Остаток", "Минимум"]
    assert stock.rows == 2
    assert empty.headers == [] and empty.rows == 0


def test_service_account_email(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com"}))
    assert service_account_email(key) == "bot@proj.iam.gserviceaccount.com"
    assert service_account_email(tmp_path / "missing.json") == ""
