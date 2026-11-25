"""Модуль 4: сбор сделок (победитель, снижение) + строка «База_Сделок» + дедуп."""

import pytest

from app.sales import scan, sheets, store
from app.sales.filters import FilterConfig


class FakeClient:
    def __init__(self, lots, contracts):
        self.lots = lots
        self.contracts = contracts  # {trd_buy_id: [contract,...]}

    def query(self, q, variables=None):
        v = variables or {}
        if "Lots(" in q:
            return {"Lots": [] if "after" in v else self.lots}
        if "Contract(" in q:
            bid = v.get("f", {}).get("trdBuyId")
            return {"Contract": self.contracts.get(bid, [])}
        return {}


def _lot(lid, name="Сервер", itogi="2026-05-01 10:00:00", status="Завершено",
         amount=10_000_000, enstru="265"):
    return {
        "id": lid, "lotNumber": f"{lid}-ЗЦП1", "nameRu": name, "amount": amount,
        "customerNameRu": "ГУ Тест", "customerBin": "111", "trdBuyId": 1000 + lid,
        "enstruList": [enstru], "pointList": [],
        "TrdBuy": {"id": 1000 + lid, "numberAnno": str(1000 + lid), "itogiDatePublic": itogi,
                   "refBuyStatusId": 350, "RefBuyStatus": {"nameRu": status},
                   "parentId": None, "finYear": 2026},
    }


def _contract(csum=8_000_000, winner="ТОО Победитель", biin="999"):
    return {"id": 1, "contractSum": csum, "faktSum": csum, "supplierBiin": biin,
            "supplierFio": "", "Supplier": {"nameRu": winner},
            "RefContractStatus": {"nameRu": "Исполнен"}}


CODES = lambda lot: {str(lot["enstruList"][0])} if lot.get("enstruList") else set()


def test_build_deal_reduction_and_winner():
    d = scan.build_deal(_lot(5), _contract(csum=8_000_000), "265")
    assert d["winner"] == "ТОО Победитель" and d["winner_bin"] == "999"
    assert d["plan_sum"] == 10_000_000 and d["contract_sum"] == 8_000_000
    assert d["reduction"] == 20.0          # (10-8)/10*100
    assert d["second"] == ""               # 2-е место недоступно


def test_scan_deals_filters_and_contracts():
    lots = [
        _lot(1),                                   # состоялась + договор + профиль -> сделка
        _lot(2, status="Не состоялась"),            # не состоялась -> нет
        _lot(3, itogi="2025-01-01 10:00:00"),       # итоги прошлого года -> нет
        _lot(4, name="Мебель офисная"),             # не профиль -> нет
        _lot(7),                                    # состоялась, но БЕЗ договора -> нет
    ]
    contracts = {1001: [_contract()], 1004: [_contract()]}  # у lot7 (buy 1007) договора нет
    client = FakeClient(lots, contracts)
    cfg = FilterConfig(keywords=["сервер"], min_sum=1_000_000)
    deals = scan.scan_deals(client, cfg, 2026, search_terms=["сервер"], code_resolver=CODES)
    assert [d["lot_number"] for d in deals] == ["1-ЗЦП1"]
    assert deals[0]["reduction"] == 20.0


def test_deal_row_shape():
    row = sheets.deal_row(scan.build_deal(_lot(5), _contract(), "265"))
    assert len(row) == 12
    assert row[1].startswith("=HYPERLINK(")
    assert row[6] == 10_000_000 and row[7] == 8_000_000 and row[8] == 20.0
    assert row[9] == "ТОО Победитель" and row[10] == "999"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sales.db"))


def test_deal_dedup(db):
    assert not store.deal_written(1)
    store.mark_deal(1)
    store.mark_deal(2)
    assert store.deal_written(1)
    assert store.deal_seen_ids() == {1, 2}
