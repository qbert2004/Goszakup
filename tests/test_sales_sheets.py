"""Google Sheets: мапперы строки<->конфиг и данные<->строка (без gspread)."""

from app.sales.sheets import (
    filter_config_from_values, funnel_row, plan_row, _hyperlink, CATEGORY_RU,
)


def test_filter_config_from_values():
    values = [
        ["KTRU", "Keywords", "Stop_words", "Min_sum", "Managers"],
        ["0265012", "Сервер", "Ремонт", "2000000", "Иван | @ivan | 111"],
        ["462011", "СХД", "Аренда", "", "Пётр | @petr | 222"],
        ["", "видеонаблюдение", "", "", ""],
    ]
    cfg = filter_config_from_values(values)
    assert cfg.enstru == {"0265012", "462011"}          # ведущий ноль сохранён
    assert cfg.keywords == ["сервер", "схд", "видеонаблюдение"]
    assert cfg.stop_words == ["ремонт", "аренда"]
    assert cfg.min_sum == 2_000_000.0                   # первая непустая ячейка D
    assert len(cfg.managers) == 2
    assert cfg.managers[0]["username"] == "ivan"


def test_filter_config_empty():
    assert filter_config_from_values([]).keywords == []
    assert filter_config_from_values([["KTRU", "Keywords"]]).enstru == set()


def test_funnel_row_shape_and_hyperlink():
    data = {
        "anno": "17900000-1", "lot_number": "88-ЗЦП1",
        "url": "https://goszakup.gov.kz/ru/announce/index/17900000",
        "customer": "ТОО Тест", "customer_bin": "123", "name": "Сервер",
        "enstru": "265012", "amount": 2_091_379, "end_date": "25.11.2026 09:00",
    }
    row = funnel_row(data, "new", manager="@ivan", status="В работе")
    assert len(row) == 11
    assert row[1] == "Новый"                            # категория RU
    assert row[2].startswith("=HYPERLINK(")            # ссылка-формула
    assert "17900000-1 · лот 88-ЗЦП1" in row[2]
    assert row[4] == "123"                              # БИН строкой
    assert row[7] == 2_091_379                          # сумма числом
    assert row[9] == "@ivan" and row[10] == "В работе"


def test_category_ru_map():
    assert CATEGORY_RU == {"new": "Новый", "failed": "Несостоявшийся", "repeat": "Повторный"}


def test_plan_row_shape():
    point = {
        "id": 55, "plnPointYear": 2026, "refMonthsId": 11,
        "RefMonths": {"nameRu": "Ноябрь"}, "subjectNameRu": "ГУ Тест",
        "subjectBiin": "999", "nameRu": "Серверное оборудование",
        "refEnstruCode": "265012", "amount": 10_000_000,
        "RefTradeMethods": {"nameRu": "Открытый конкурс"},
    }
    row = plan_row(point)
    assert row[0] == "55" and row[1] == 2026 and row[2] == "Ноябрь"
    assert row[6] == "265012" and row[7] == 10_000_000
    assert row[8] == "Открытый конкурс"


def test_hyperlink_escapes_quotes():
    assert _hyperlink("http://x", 'A "B"') == '=HYPERLINK("http://x";"A \'B\'")'
    assert _hyperlink("", "text") == "text"
