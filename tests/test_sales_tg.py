"""Telegram-слой: клавиатуры, разбор callback, упоминания."""

from app.sales.tg import mention, main_keyboard, assign_keyboard, parse_callback


def test_mention_prefers_username():
    assert mention({"username": "ivan", "name": "Иван"}) == "@ivan"
    assert mention({"name": "Иван"}) == "Иван"
    assert mention({}) == ""


def test_main_keyboard_structure():
    kb = main_keyboard("88-ЗЦП1")
    rows = kb["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "t:88-ЗЦП1"
    assert rows[1][0]["callback_data"] == "a:88-ЗЦП1"
    assert rows[1][1]["callback_data"] == "s:88-ЗЦП1"


def test_assign_keyboard_has_managers_and_back():
    managers = [{"name": "Иван"}, {"name": "Пётр"}]
    kb = assign_keyboard("L1", managers)
    rows = kb["inline_keyboard"]
    assert rows[0][0]["text"] == "Иван" and rows[0][0]["callback_data"] == "p:L1:0"
    assert rows[1][0]["callback_data"] == "p:L1:1"
    assert rows[2][0]["callback_data"] == "b:L1"  # Назад


def test_parse_callback():
    assert parse_callback("t:88") == ("t", "88", None)
    assert parse_callback("p:88:2") == ("p", "88", "2")
    assert parse_callback("s:88") == ("s", "88", None)
    assert parse_callback("") == ("", None, None)


def test_callback_data_within_64_bytes():
    # Telegram лимит 64 байта на callback_data — самый длинный вариант p:<id>:<idx>
    cb = f"p:{'9' * 10}:99"
    assert len(cb.encode()) <= 64
