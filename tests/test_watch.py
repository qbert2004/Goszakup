"""БИН-трек: отбор активных крупных тендеров отслеживаемых заказчиков."""

from unittest.mock import patch

from app import watch
from app.config import Settings


class FakeClient:
    def __init__(self, rows_by_bin):
        self.rows_by_bin = rows_by_bin

    def query(self, _query, variables):
        return {"Lots": self.rows_by_bin.get(variables["f"]["customerBin"], [])}


def _row(lot_id, number, amount, status):
    return {
        "id": lot_id, "lotNumber": number, "nameRu": "Тендер", "amount": amount,
        "customerNameRu": "ТОО Тест", "trdBuyId": 100 + lot_id,
        "TrdBuy": {"numberAnno": str(1000 + lot_id),
                   "endDate": "2026-09-01 09:00:00", "refBuyStatusId": status},
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
        found = watch.find_new(client, s)
    assert [f["lot_number"] for f in found] == ["A-1"]
    assert found[0]["customer"] == "ТОО Тест"


def test_find_new_skips_already_notified():
    client = FakeClient({"111": [_row(1, "A-1", 5_000_000, 210)]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=True):
        assert watch.find_new(client, s) == []


def test_find_new_dedups_same_lot_across_scan():
    # один и тот же lot_id не должен уехать дважды за проход
    client = FakeClient({"111": [
        _row(1, "A-1", 5_000_000, 210),
        _row(1, "A-1", 5_000_000, 220),
    ]})
    s = Settings(watch_bins=["111"], bin_min_amount=2_000_000)
    with patch("app.store.already_notified_bin", return_value=False):
        found = watch.find_new(client, s)
    assert len(found) == 1
