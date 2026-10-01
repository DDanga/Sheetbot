import string

from sheetbot.i18n import TEXTS, next_lang, t


def fields(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_both_languages_have_the_same_keys_and_placeholders():
    assert TEXTS["en"].keys() == TEXTS["ru"].keys()
    for key in TEXTS["en"]:
        assert fields(TEXTS["en"][key]) == fields(TEXTS["ru"][key]), key


def test_switching_cycles():
    assert next_lang("en") == "ru"
    assert next_lang("ru") == "en"
    assert next_lang("xx") == "ru"


def test_unknown_language_falls_back_to_english():
    assert t("de", "lang_set") == TEXTS["en"]["lang_set"]
