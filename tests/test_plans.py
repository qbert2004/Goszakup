"""Отбор планов: регион, правовая форма заказчика, год.

Ломается тихо: неверный префикс КАТО — и в выгрузку попадает пол-страны;
слишком узкий набор форм — и заказчик недосчитается половины госзаказа.
"""

import pytest

from app import plans


def point(kato="270000000", year=2026, name="Коммутатор сетевой"):
    return {
        "id": 1,
        "subjectBiin": "123456789012",
        "subjectNameRu": 'ГУ "Аппарат акима"',
        "nameRu": name,
        "descRu": "управляемый",
        "refEnstruCode": "263023.900.000076",
        "count": 2,
        "price": 500000,
        "amount": 1000000,
        "plnPointYear": year,
        "RefUnits": {"nameRu": "Штука"},
        "RefTradeMethods": {"nameRu": "Открытый конкурс"},
        "RefMonths": {"nameRu": "Август"},
        "RefPlnPointStatus": {"nameRu": "Утвержден"},
        "PlansKato": [{"refKatoCode": kato, "fullDeliveryPlaceNameRu": "ЗКО, Уральск"}],
    }


@pytest.mark.parametrize("kato,region", [
    ("151010000", "Актюбинская область"),
    ("231010000", "Атырауская область"),
    ("270000000", "Западно-Казахстанская область"),
    ("471010000", "Мангистауская область"),
])
def test_western_regions_are_recognised(kato, region):
    found = plans._region_of(point(kato=kato))
    assert found and found[0] == region


@pytest.mark.parametrize("kato", ["751210000", "351010000", "710000000", "431010000"])
def test_other_regions_are_rejected(kato):
    """Алматы, Караганда, Астана, Кызылорда — не Западный Казахстан."""
    assert plans._region_of(point(kato=kato)) is None


def test_point_without_kato_is_rejected():
    item = point()
    item["PlansKato"] = []
    assert plans._region_of(item) is None


def test_gu_is_a_government_body():
    """ГУ — аппараты акимов, департаменты, управления. Это и есть госорганы."""
    assert "ГУ" in plans.STRICT_GOV_FORMS
    assert "ГУ" in plans.GOV_FORMS


def test_state_enterprises_are_in_wide_set_only():
    """ГП/ГКП — больницы, водоканалы: государственные, но не органы власти.

    На живых данных их больше, чем ГУ (118 против 86), поэтому в широкий набор
    они входят, а в строгий — нет. Решает заказчик, фильтруя колонку «Форма».
    """
    for form in ("ГП", "ГКП", "КГКП"):
        assert form in plans.GOV_FORMS, f"{form} должен быть в широком наборе"
        assert form not in plans.STRICT_GOV_FORMS, f"{form} не орган власти"


def test_private_forms_are_never_included():
    for form in ("ТОО", "АО", "НАО", "ИП"):
        assert form not in plans.GOV_FORMS, f"{form} — частник, не госзаказчик"


def test_columns_match_the_requested_table():
    """Колонки со скрина заказчика должны быть на месте."""
    for column in ("ID плана", "Заказчик", "Наименование ЕНСТРУ", "Способ закупки",
                   "Ед. измерения", "Количество", "Цена за единицу", "Сумма",
                   "Месяц", "Статус"):
        assert column in plans.COLUMNS, f"нет колонки {column}"


def test_form_and_region_columns_exist():
    """Без них заказчик не сможет отфильтровать госпредприятия и области сам."""
    assert "Форма" in plans.COLUMNS
    assert "Регион" in plans.COLUMNS


def test_csv_is_written_for_google_sheets(tmp_path):
    rows = [dict.fromkeys(plans.COLUMNS, "x")]
    path = plans.to_csv(rows, tmp_path / "out.csv")

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "нужен BOM, иначе Sheets ломает кириллицу"
    assert b";" in raw, "разделитель ; — Sheets и Excel так понимают колонки"


def test_csv_keeps_every_row(tmp_path):
    rows = [dict.fromkeys(plans.COLUMNS, str(i)) for i in range(50)]
    path = plans.to_csv(rows, tmp_path / "out.csv")

    lines = path.read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 51, "50 строк + заголовок"
