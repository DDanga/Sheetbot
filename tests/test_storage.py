from sheetbot.storage import Storage


def test_first_user_becomes_admin_and_only_once(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    admin = s.claim_admin(1, "Danga", "en")
    assert admin and admin.is_admin
    assert s.claim_admin(2, "Stranger", "en") is None
    assert s.user(2) is None


def test_language_is_remembered(tmp_path):
    path = tmp_path / "db.sqlite"
    s = Storage(path)
    s.claim_admin(1, "Danga", "en")
    s.set_lang(1, "ru")
    s.close()
    assert Storage(path).user(1).lang == "ru"
