"""Память об отправленных лотах.

Правило: помним лот, пока идёт приём заявок; как приём закрылся — забываем.

Ломается тихо, и в обе стороны:
  - забыли рано  -> лот придёт менеджерам повторно, пока висит в окне;
  - не забыли    -> база растёт вечно (мёртвый груз, отправлен он уже не будет).
"""

from datetime import datetime, timedelta

import pytest

from app import store
from app.window import ASTANA


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Каждый тест на своей базе: иначе они зависят от локального monitor.db."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


def lot(lot_id: int = 1, hours_to_deadline: float = 20.0) -> dict:
    end = datetime.now(ASTANA) + timedelta(hours=hours_to_deadline)
    return {
        "lot_id": lot_id,
        "trd_buy_id": 10,
        "lot_number": "1-1",
        "name": "Коммутатор сетевой",
        "amount": 5_000_000,
        "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_fresh_lot_is_not_known():
    assert not store.already_notified(1)


def test_sent_lot_is_remembered():
    store.mark_notified(lot())
    assert store.already_notified(1), "повтор недопустим, пока лот открыт"


def test_open_lot_survives_purge():
    """Приём ещё идёт — забывать нельзя, иначе лот придёт второй раз."""
    store.mark_notified(lot(1, hours_to_deadline=20))

    assert store.purge_closed() == 0
    assert store.already_notified(1)


def test_lot_with_monday_deadline_is_not_forgotten_over_the_weekend():
    """Тот случай, ради которого отказались от TTL в 24 часа: лот показан в
    пятницу, дедлайн в понедельник. Через сутки он всё ещё открыт — и повторного
    уведомления быть не должно."""
    store.mark_notified(lot(1, hours_to_deadline=72))

    assert store.purge_closed() == 0
    assert store.already_notified(1), "лот открыт трое суток — помним всё это время"


def test_closed_lot_is_forgotten():
    store.mark_notified(lot(1, hours_to_deadline=-1))

    assert store.purge_closed() == 1
    assert not store.already_notified(1)


def test_purge_touches_only_closed_lots():
    store.mark_notified(lot(1, hours_to_deadline=-2))   # приём закончился
    store.mark_notified(lot(2, hours_to_deadline=10))   # ещё идёт

    forgotten = store.purge_closed()

    assert forgotten == 1
    assert not store.already_notified(1), "закрытый забыт"
    assert store.already_notified(2), "открытый на месте"


def test_lot_without_deadline_is_kept():
    """Дедлайн неизвестен — не выбрасываем: удалить проще, чем спамить."""
    item = lot(1)
    item["end_date"] = None
    store.mark_notified(item)

    assert store.purge_closed() == 0
    assert store.already_notified(1)


def test_purge_is_idempotent():
    store.mark_notified(lot(1, hours_to_deadline=-1))

    assert store.purge_closed() == 1
    assert store.purge_closed() == 0, "повторная чистка ничего не находит"
