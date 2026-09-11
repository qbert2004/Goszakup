"""БИН-трек: отбор активных крупных тендеров отслеживаемых заказчиков."""

from datetime import datetime, timedelta
from unittest.mock import patch

from app import watch
from app.config import Settings
from app.window import ASTANA

# Дедлайн должен быть в будущем, иначе find_new (правильно) отсеет лот как
# просроченный. Считаем от «сейчас», чтобы тест не протухал со временем.
FUTURE = (datetime.now(ASTANA) + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
PAST = "2000-01-01 09:00:00"


class FakeClient:
    def __init__(self, rows_by_bin):
        self.rows_by_bin = rows_by_bin

    def query(self, _query, variables):
        return {"Lots": self.rows_by_bin.get(variables["f"]["customerBin"], [])}


def _row(lot_id, number, amount, status, end_date=FUTURE):
    return {
        "id": lot_id, "lotNumber": number, "nameRu": "Тендер", "amount": amount,
        "customerNameRu": "ТОО Тест", "trdBuyId": 100 + lot_id,
        "TrdBuy": {"numberAnno": str(1000 + lot_id),
                   "endDate": end_date, "refBuyStatusId": status},
    }


def test_find_new_filters_status_amount_and_dedup():
    client = FakeClient({"111": [
        _row(1, "A-1", 5_000_000, 210),   # активный, крупный -> да
        _row(2, "A-2", 100_000, 220),     # мелкий -> нет
        _row(3, "A-3", 9_000_000, 350),   # завершён -> нет
        _row(4, "A-4", 9_000_000, 190),   # черновик/на утверждении -> нет
    ]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=False):
        found = watch.find_new(client, s, ["111"])
    assert [f["lot_number"] for f in found] == ["A-1"]
    assert found[0]["customer"] == "ТОО Тест"


def test_find_new_skips_already_notified():
    client = FakeClient({"111": [_row(1, "A-1", 5_000_000, 210)]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=True):
        assert watch.find_new(client, s, ["111"]) == []


def test_find_new_skips_past_deadline():
    # Приём закрылся: лот ещё числится активным, но повторно слать его нельзя —
    # именно на этой дыре (purge_closed вычистил дедуп у истёкшего лота) лот уходил
    # второй раз с «осталось -0 ч». Просроченный не отправляем даже с чистым дедупом.
    client = FakeClient({"111": [_row(1, "A-1", 5_000_000, 220, end_date=PAST)]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=False):
        assert watch.find_new(client, s, ["111"]) == []


def test_find_new_keyword_filter_code_and_name():
    # Фильтр по ключевым словам (список ЕНСТРУ): код решает, имя — запасное.
    rows = [
        {**_row(1, "A-1", 5_000_000, 210), "pointList": [10]},  # код наш -> да
        {**_row(2, "A-2", 5_000_000, 210), "pointList": [20]},  # код чужой -> нет
        {**_row(3, "A-3", 5_000_000, 210), "pointList": []},    # кода нет -> имя
    ]
    client = FakeClient({"111": rows})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)

    def fake_code(_cl, lot):
        pl = lot.get("point_list") or []
        return {10: "OUR", 20: "FOREIGN"}.get(pl[0] if pl else None)

    with patch("app.store.already_notified_bin", return_value=False), \
         patch("app.monitor._enstru_code_of", side_effect=fake_code):
        found = watch.find_new(client, s, ["111"], our_codes={"OUR"}, our_names={"тендер"})
    nums = [f["lot_number"] for f in found]
    assert nums == ["A-1", "A-3"]  # наш код + имя-фолбэк; чужой код отсеян


def test_find_new_dedups_same_lot_across_scan():
    # один и тот же lot_id не должен уехать дважды за проход
    client = FakeClient({"111": [
        _row(1, "A-1", 5_000_000, 210),
        _row(1, "A-1", 5_000_000, 220),
    ]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=False):
        found = watch.find_new(client, s, ["111"])
    assert len(found) == 1
