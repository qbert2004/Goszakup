"""Оркестратор: классификация, фильтрация, дедуп, данные карточек."""

from app.sales import scan
from app.sales.filters import FilterConfig


class FakeClient:
    """Отдаёт лоты один раз на термин (как одна страница < PAGE)."""

    def __init__(self, lots_by_term):
        self.lots_by_term = lots_by_term

    def query(self, q, variables=None):
        if "Lots(" in q:
            term = (variables or {}).get("f", {}).get("nameDescriptionRu")
            if "after" in (variables or {}):
                return {"Lots": []}
            return {"Lots": self.lots_by_term.get(term, [])}
        return {}


def _lot(lid, name, status=220, parent=None, amount=5_000_000, enstru=None, failed=False):
    buy = {
        "id": 1000 + lid, "numberAnno": str(1000 + lid),
        "endDate": "2026-12-01 09:00:00", "parentId": parent, "finYear": 2026,
        "refBuyStatusId": None if failed else status,
        "RefTradeMethods": {"nameRu": "ЗЦП"},
        "RefBuyStatus": {"nameRu": "Не состоялась" if failed else "Прием заявок"},
    }
    return {
        "id": lid, "lotNumber": f"{lid}-ЗЦП1", "nameRu": name, "amount": amount,
        "customerNameRu": "ТОО Тест", "customerBin": "123456789012",
        "trdBuyId": 1000 + lid, "pointList": [], "enstruList": [enstru] if enstru else [],
        "TrdBuy": buy,
    }


CODES = lambda lot: {str(lot["enstruList"][0])} if lot.get("enstruList") else set()


def test_scan_classifies_filters_dedups():
    lots = [
        _lot(1, "Сервер стоечный"),                      # new (по слову)
        _lot(2, "Сервер БД", parent=99),                 # repeat
        _lot(3, "Мебель офисная"),                        # отсев: ни слова, ни кода
        _lot(4, "Резервный сервер", failed=True),         # failed (первичный, 2026)
        _lot(1, "Сервер стоечный"),                       # дубль id=1 -> игнор
    ]
    cfg = FilterConfig(keywords=["сервер"], min_sum=2_000_000)
    client = FakeClient({"сервер": lots})
    res = scan.scan_lots(client, cfg, 2026, search_terms=["сервер"], code_resolver=CODES)

    assert [d["lot_number"] for d in res["new"]] == ["1-ЗЦП1"]
    assert [d["lot_number"] for d in res["repeat"]] == ["2-ЗЦП1"]
    assert [d["lot_number"] for d in res["failed"]] == ["4-ЗЦП1"]
    # «Мебель» отфильтрована
    assert all(d["name"] != "Мебель офисная" for lst in res.values() for d in lst)
    # у повторного есть ссылка на родителя
    assert "parent_url" in res["repeat"][0] and "99" in res["repeat"][0]["parent_url"]


def test_scan_match_by_enstru_code_only():
    # имя без ключевого слова, но код совпадает -> проходит
    lots = [_lot(5, "Поставка оборудования", enstru="265012")]
    cfg = FilterConfig(enstru={"265012"}, keywords=["сервер"], min_sum=1_000_000)
    client = FakeClient({"265012": lots})
    res = scan.scan_lots(client, cfg, 2026, search_terms=["265012"], code_resolver=CODES)
    assert len(res["new"]) == 1
    assert res["new"][0]["enstru"] == "265012"


def test_scan_respects_seen():
    lots = [_lot(1, "Сервер стоечный")]
    cfg = FilterConfig(keywords=["сервер"], min_sum=0)
    client = FakeClient({"сервер": lots})
    res = scan.scan_lots(client, cfg, 2026, search_terms=["сервер"], seen={1}, code_resolver=CODES)
    assert res == {"new": [], "failed": [], "repeat": []}


def test_fmt_deadline():
    assert scan.fmt_deadline("2026-12-01 09:00:00") == "01.12.2026 09:00"
    assert scan.fmt_deadline("") == ""
    assert scan.fmt_deadline("мусор") == "мусор"


def test_plans_by_month():
    pts = [
        {"refMonthsId": 11, "amount": 1_000_000, "RefMonths": {"nameRu": "Ноябрь"}},
        {"refMonthsId": 11, "amount": 2_000_000, "RefMonths": {"nameRu": "Ноябрь"}},
        {"refMonthsId": 12, "amount": 5_000_000, "RefMonths": {"nameRu": "Декабрь"}},
    ]
    g = scan.plans_by_month(pts)
    assert g[11]["count"] == 2 and g[11]["sum"] == 3_000_000 and g[11]["name"] == "Ноябрь"
    assert g[12]["count"] == 1 and g[12]["sum"] == 5_000_000
