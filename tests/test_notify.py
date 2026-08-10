"""Отправка не должна нарываться на лимит Telegram.

Telegram режет примерно на 20 сообщениях в минуту в одну группу. Первый проход
находит сразу сотню лотов — если слать по одному, половина не дойдёт.
"""

from unittest.mock import patch

import pytest

from app import notify


def lot(n: int, apps: int = 0) -> dict:
    return {
        "name": f"Коммутатор сетевой {n}",
        "applications": apps,
        "hours_left": 30.0,
        "customer": "ГУ Тест",
        "amount": 1_000_000,
        "lot_number": f"{n}-1",
        "number_anno": f"{n}-1",
        "url": f"https://goszakup.gov.kz/ru/announce/index/{n}",
    }


@pytest.fixture
def sent():
    calls = []
    with patch.object(notify, "send", side_effect=lambda t, c, text: calls.append(text)):
        with patch.object(notify.time, "sleep"):  # не ждём в тестах
            yield calls


def test_few_lots_go_one_by_one(sent):
    count = notify.send_lots("tok", "-1", [lot(i) for i in range(3)])

    assert count == 3
    assert len(sent) == 3, "мало лотов — каждый отдельным сообщением"


def test_hundred_lots_do_not_become_hundred_messages(sent):
    """Главное: сотня лотов не должна упереться в лимит Telegram."""
    notify.send_lots("tok", "-1", [lot(i) for i in range(100)])

    assert len(sent) == 10, "100 лотов -> 10 дайджестов, а не 100 сообщений"
    assert len(sent) < 20, "иначе Telegram начнёт отвечать 429"


def test_digest_keeps_every_lot_and_its_link(sent):
    lots = [lot(i) for i in range(25)]
    notify.send_lots("tok", "-1", lots)

    everything = "\n".join(sent)
    for item in lots:
        assert item["url"] in everything, f"лот {item['number_anno']} потерялся"


def test_urgent_lots_come_first(sent):
    """Обход идёт по алфавиту позиций — без сортировки горящий лот уедет в хвост."""
    calm = lot(1)
    calm["hours_left"] = 47.0
    burning = lot(2)
    burning["hours_left"] = 1.5

    notify.send_lots("tok", "-1", [calm, burning])

    assert burning["url"] in sent[0], "первым должен идти лот с 1.5 часа до конца"
    assert calm["url"] in sent[1]


def test_urgent_lot_lands_in_first_digest(sent):
    lots = [lot(i) for i in range(30)]
    for index, item in enumerate(lots):
        item["hours_left"] = 47.0 - index * 0.1
    lots[-1]["hours_left"] = 0.5  # самый горящий — последний по обходу

    notify.send_lots("tok", "-1", lots)

    assert lots[-1]["url"] in sent[0], "горящий лот обязан попасть в первый дайджест"


def test_empty_list_sends_nothing(sent):
    assert notify.send_lots("tok", "-1", []) == 0
    assert not sent


def test_digest_marks_zero_competition():
    text = notify.format_digest([lot(1), lot(2)])
    assert "Лотов без конкурентов: 2" in text


def test_single_lot_message_shows_zero_prominently():
    text = notify.format_lot(lot(1))
    assert "0 заявок" in text
    assert "🎯" in text


def test_single_lot_message_marks_non_zero_differently():
    """Режим низкой конкуренции: лот с заявками не должен выглядеть как чистый ноль."""
    text = notify.format_lot(lot(1, apps=3))
    assert "заявок: 3" in text
    assert "🎯" not in text, "значок нулевой конкуренции только для настоящего нуля"


def test_enstru_code_shown_when_verified():
    item = lot(1)
    item["enstru_code"] = "263023.900.000078"
    assert "263023.900.000078" in notify.format_lot(item)


def test_html_in_lot_name_is_escaped():
    """Имена приходят с площадки — экранируем, иначе Telegram отвергнет разметку."""
    item = lot(1)
    item["name"] = 'Сервер <b>"взлом"</b> & прочее'
    text = notify.format_lot(item)
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
