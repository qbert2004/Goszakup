"""Выгрузка объявленных закупок: регион, форма заказчика, глубина.

Главное, что здесь легко сломать незаметно — определение региона. У 14% лотов
место поставки пустое, и если не подстраховаться регионом заказчика, тихо
теряется треть западных закупок (замер: 186 из 487).
"""

import pytest

from app import purchases


def lot(place_kato="271010000", home_kato="271010000", kopf="ГУ", end="2026-08-01 10:00:00"):
    return {
        "id": 1,
        "lotNumber": "1-1",
        "nameRu": "Коммутатор сетевой",
        "descriptionRu": "управляемый",
        "amount": 5_000_000,
        "count": 2,
        "customerBin": "123456789012",
        "customerNameRu": 'ГУ "Аппарат акима"',
        "trdBuyId": 100,
        "plnPointKatoList": [place_kato],
        "RefLotsStatus": {"nameRu": "Опубликован (прием заявок)"},
        "Customer": {"katoList": [home_kato], "refKopfCode": kopf},
        "TrdBuy": {
            "numberAnno": "100-1",
            "startDate": "2026-07-20 09:00:00",
            "endDate": end,
            "itogiDatePublic": None,
            "RefTradeMethods": {"nameRu": "Открытый конкурс"},
        },
    }


@pytest.mark.parametrize("kato,region", [
    ("151010000", "Актюбинская область"),
    ("231010000", "Атырауская область"),
    ("271010000", "Западно-Казахстанская область"),
    ("471010000", "Мангистауская область"),
])
def test_region_from_delivery_place(kato, region):
    found = purchases._region_of(lot(place_kato=kato))
    assert found == (region, "место поставки")


def test_falls_back_to_customer_region_when_place_is_empty():
    """Ровно этот случай даёт 36% западных лотов — терять его нельзя."""
    found = purchases._region_of(lot(place_kato="", home_kato="231010000"))
    assert found == ("Атырауская область", "регион заказчика")


def test_delivery_place_wins_over_customer_region():
    """Заказчик из Алматы, поставка в Атырау — это западная закупка."""
    found = purchases._region_of(lot(place_kato="231010000", home_kato="751010000"))
    assert found == ("Атырауская область", "место поставки")


def test_delivery_elsewhere_wins_over_western_customer():
    """Заказчик западный, но товар едет в Алматы — закупка не западная.

    На живых данных таких лотов ноль, но правило должно быть однозначным:
    решает место поставки, регион заказчика — только запасной путь.
    """
    found = purchases._region_of(lot(place_kato="751010000", home_kato="271010000"))
    assert found is None


def test_non_western_lot_is_rejected():
    assert purchases._region_of(lot(place_kato="751210000", home_kato="751210000")) is None


def test_lot_without_any_kato_is_rejected():
    assert purchases._region_of(lot(place_kato="", home_kato="")) is None


def test_columns_cover_what_matters():
    for column in ("ID лота", "Заказчик", "Форма", "Регион", "Сумма",
                   "Окончание приёма", "Приём идёт", "Ссылка"):
        assert column in purchases.COLUMNS, f"нет колонки {column}"


def test_active_flag_column_exists():
    """Без неё в выгрузке не отличить живую закупку от архивной."""
    assert "Приём идёт" in purchases.COLUMNS


def test_export_writes_its_own_columns(tmp_path):
    """Колонки закупок отличаются от планов — общий to_csv не должен их путать."""
    rows = [dict.fromkeys(purchases.COLUMNS, "x")]
    path = purchases.export(rows, tmp_path / "out.csv")

    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "ID лота" in header, "взяты колонки планов вместо закупок"
    assert "Приём идёт" in header
