"""Модуль 5: меню планов по месяцам — клавиатура, память свода, нажатие месяца."""

from datetime import datetime

import pytest

from app.sales import cards, runner, store
from app.sales.tg import month_menu_keyboard


class FakeTG:
    def __init__(self):
        self.edits = []
        self.answers = []

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "kb": reply_markup})

    def answer_callback_query(self, cb_id, text=None):
        self.answers.append(text)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sales.db"))


def test_month_menu_keyboard():
    kb = month_menu_keyboard()
    rows = kb["inline_keyboard"]
    assert len(rows) == 4 and all(len(r) == 3 for r in rows)
    assert rows[0][0]["text"] == "Январь" and rows[0][0]["callback_data"] == "pm:1"
    assert rows[3][2]["text"] == "Декабрь" and rows[3][2]["callback_data"] == "pm:12"


def test_save_and_get_plan_months(db):
    store.save_plan_months(2026, {11: {"count": 5, "sum": 3_000_000, "name": "Ноябрь"}})
    md = store.get_plan_month(2026, 11)
    assert md["cnt"] == 5 and md["total"] == 3_000_000 and md["name"] == "Ноябрь"
    assert store.get_plan_month(2026, 1) is None
    # перезапись
    store.save_plan_months(2026, {1: {"count": 2, "sum": 1.0, "name": "Январь"}})
    assert store.get_plan_month(2026, 11) is None
    assert store.get_plan_month(2026, 1)["cnt"] == 2


def test_meta_kv(db):
    assert store.get_meta("x") is None
    store.set_meta("plans_url", "http://sheet")
    assert store.get_meta("plans_url") == "http://sheet"


def test_plans_menu_text():
    t = cards.plans_menu_text("Ноябрь", 5, 3_000_000, "http://sheet")
    assert "Ноябрь" in t and "Позиций: 5" in t and "3 000 000" in t
    assert "http://sheet" in t


def test_plans_menu_press_shows_month(db):
    year = datetime.now().year
    store.save_plan_months(year, {11: {"count": 7, "sum": 9_000_000, "name": "Ноябрь"}})
    store.set_meta("plans_url", "http://sheet/plans")
    tg = FakeTG()
    cbq = {"id": "c1", "data": "pm:11", "message": {"chat": {"id": -100}, "message_id": 5}}
    runner.handle_callback(cbq, tg, None, [])
    assert tg.edits, "меню должно отредактироваться"
    text = tg.edits[-1]["text"]
    assert "Ноябрь" in text and "Позиций: 7" in text and "9 000 000" in text
    assert "http://sheet/plans" in text
    assert tg.edits[-1]["kb"]["inline_keyboard"][0][0]["callback_data"] == "pm:1"  # клавиатура осталась
