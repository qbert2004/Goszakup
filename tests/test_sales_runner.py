"""Рантайм: рассылка карточек, кнопки (take/pick/skip/assign), напоминания."""

from datetime import datetime, timedelta

import pytest

from app.sales import runner, store
from app.window import ASTANA


class FakeTG:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._mid = 100

    def send_message(self, chat_id, text, thread_id=None, reply_markup=None, reply_to=None):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id,
                          "reply_to": reply_to, "kb": reply_markup})
        return {"message_id": self._mid}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"message_id": message_id, "text": text, "kb": reply_markup})
        return {}

    def answer_callback_query(self, cb_id, text=None):
        self.answers.append(text)
        return {}


class FakeWB:
    def __init__(self):
        self.funnel = []
        self.updates = []

    def append_funnel(self, rows):
        self.funnel += rows

    def set_funnel_responsible(self, marker, manager, status):
        self.updates.append((marker, manager, status))
        return True


def _card(lot_id, name="Сервер", end_raw="2026-12-01 09:00:00"):
    return {
        "lot_id": lot_id, "trd_buy_id": 1000 + lot_id, "url": "http://x",
        "anno": str(1000 + lot_id), "lot_number": f"{lot_id}-ЗЦП1", "customer": "ТОО",
        "customer_bin": "1", "name": name, "enstru": "265", "amount": 5_000_000,
        "method": "ЗЦП", "end_date": "01.12.2026 09:00", "end_date_raw": end_raw,
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sales.db"))


def test_dispatch_sends_records_dedups(db):
    tg, wb = FakeTG(), FakeWB()
    results = {"new": [_card(1), _card(2)], "failed": [], "repeat": []}
    n = runner.dispatch(results, tg, wb, "-100g", {"new": 839})
    assert n == 2
    assert len(tg.sent) == 2 and tg.sent[0]["thread_id"] == 839
    assert len(wb.funnel) == 2
    assert store.already_sent(1) and store.already_sent(2)
    # повторная рассылка тех же лотов — дедуп
    assert runner.dispatch(results, tg, wb, "-100g", {"new": 839}) == 0


def test_callback_take(db):
    tg, wb = FakeTG(), FakeWB()
    runner.dispatch({"new": [_card(5)], "failed": [], "repeat": []}, tg, wb, "-100g", {"new": 839})
    runner.handle_callback({"id": "c", "data": "t:5", "from": {"id": 1, "username": "ivan"}}, tg, wb, [])
    card = store.get_card(5)
    assert card["status"] == "В работе" and card["manager"] == "@ivan"
    assert "Ответственный: @ivan" in tg.edits[-1]["text"]
    assert wb.updates[-1] == (card["marker"], "@ivan", "В работе")
    assert tg.answers[-1] == "Взято в работу"


def test_callback_assign_pick(db):
    tg, wb = FakeTG(), FakeWB()
    managers = [{"name": "A", "username": "a"}, {"name": "B", "username": "b"}]
    runner.dispatch({"new": [_card(6)], "failed": [], "repeat": []}, tg, wb, "-100g", {"new": 839})
    # открыть меню «Назначить» -> клавиатура со списком
    runner.handle_callback({"id": "c", "data": "a:6", "from": {}}, tg, wb, managers)
    assert any(btn[0]["callback_data"] == "b:6" for btn in tg.edits[-1]["kb"]["inline_keyboard"])
    # выбрать второго менеджера
    runner.handle_callback({"id": "c", "data": "p:6:1", "from": {}}, tg, wb, managers)
    assert store.get_card(6)["manager"] == "@b"


def test_callback_skip(db):
    tg, wb = FakeTG(), FakeWB()
    runner.dispatch({"new": [_card(8)], "failed": [], "repeat": []}, tg, wb, "-100g", {"new": 839})
    runner.handle_callback({"id": "c", "data": "s:8", "from": {"id": 2, "first_name": "Пётр"}}, tg, wb, [])
    card = store.get_card(8)
    assert card["status"] == "Скип" and card["manager"] == "Пётр"
    assert "Пропущен" in tg.edits[-1]["text"]


def test_callback_stale_card(db):
    tg, wb = FakeTG(), FakeWB()
    runner.handle_callback({"id": "c", "data": "t:999", "from": {}}, tg, wb, [])
    assert tg.answers[-1] == "Карточка устарела"
    assert not tg.edits


def test_send_reminders(db):
    tg, wb = FakeTG(), FakeWB()
    soon = (datetime.now(ASTANA) + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    runner.dispatch({"new": [_card(7, end_raw=soon)], "failed": [], "repeat": []}, tg, wb, "-100g", {"new": 839})
    store.set_status(7, "@ivan", "В работе")
    assert runner.send_reminders(tg) == 1
    assert any("24 часа" in s["text"] for s in tg.sent)
    # второй раз — уже напомнили
    assert runner.send_reminders(tg) == 0


def test_seed_marks_without_sending(db):
    wb = FakeWB()
    results = {"new": [_card(10), _card(11)], "failed": [], "repeat": []}
    n = runner.seed(results, wb, "-100g", {"new": 839})
    assert n == 2
    assert store.already_sent(10) and store.already_sent(11)
    assert len(wb.funnel) == 2            # в Воронку легли
    # после посева рассылка тех же лотов ничего не шлёт
    tg = FakeTG()
    assert runner.dispatch(results, tg, wb, "-100g", {"new": 839}) == 0
    assert tg.sent == []


def test_reminder_skips_unassigned(db):
    tg, wb = FakeTG(), FakeWB()
    soon = (datetime.now(ASTANA) + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    runner.dispatch({"new": [_card(9, end_raw=soon)], "failed": [], "repeat": []}, tg, wb, "-100g", {"new": 839})
    # статус «Не назначен» -> напоминания быть не должно
    assert runner.send_reminders(tg) == 0
