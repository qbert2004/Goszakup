"""Фильтр-движок отдела продаж: ЕНСТРУ ИЛИ ключевые слова, стоп-слова, мин.сумма."""

from app.sales.filters import FilterConfig, parse_managers


def _cfg(**kw):
    base = dict(
        enstru={"265012", "462011"},
        keywords=["сервер", "видеонаблюден"],
        stop_words=["ремонт", "аренда"],
        min_sum=2_000_000,
    )
    base.update(kw)
    return FilterConfig(**base)


def test_match_by_enstru_code():
    cfg = _cfg()
    # код совпал — проходит, даже если слова нет
    assert cfg.passes("Поставка оборудования", ["265012"], 5_000_000)


def test_match_by_keyword():
    cfg = _cfg()
    # кода нет/чужой, но слово в наименовании — проходит
    assert cfg.passes("Сервер стоечный", ["999999"], 5_000_000)


def test_stop_word_excludes():
    cfg = _cfg()
    # слово «сервер» есть, но «ремонт» в стоп-листе — мимо
    assert not cfg.passes("Ремонт сервера", ["999999"], 5_000_000)


def test_min_sum_excludes():
    cfg = _cfg()
    assert not cfg.passes("Сервер стоечный", ["265012"], 1_000_000)
    assert not cfg.passes("Сервер стоечный", ["265012"], None)


def test_no_match_when_neither():
    cfg = _cfg()
    assert not cfg.passes("Поставка мебели", ["999999"], 5_000_000)


def test_from_columns_keeps_leading_zeros_and_parses():
    cfg = FilterConfig.from_columns(
        ktru_col=["0265012", "462011", ""],
        keywords_col=["Сервер", "", "СХД"],
        stop_col=["Ремонт"],
        min_sum="2 000 000",
        managers_col=["Иван | @ivan | 111", "Пётр|@petr|222", ""],
    )
    assert "0265012" in cfg.enstru          # ведущий ноль сохранён
    assert cfg.keywords == ["сервер", "схд"]  # нижний регистр, пустые убраны
    assert cfg.stop_words == ["ремонт"]
    assert cfg.min_sum == 2_000_000.0
    assert cfg.managers[0] == {"name": "Иван", "username": "ivan", "user_id": "111"}
    assert cfg.managers[1] == {"name": "Пётр", "username": "petr", "user_id": "222"}


def test_parse_managers_partial():
    rows = ["Только имя", "Имя | @nick", "  "]
    out = parse_managers(rows)
    assert out == [
        {"name": "Только имя", "username": "", "user_id": ""},
        {"name": "Имя", "username": "nick", "user_id": ""},
    ]


def test_parse_managers_bare_username_and_any_order():
    out = parse_managers(["@Swsuga", "111 | @ivan | Иван", "Пётр | 222"])
    assert out[0] == {"name": "@Swsuga", "username": "Swsuga", "user_id": ""}
    assert out[1] == {"name": "Иван", "username": "ivan", "user_id": "111"}  # порядок любой
    assert out[2] == {"name": "Пётр", "username": "", "user_id": "222"}
