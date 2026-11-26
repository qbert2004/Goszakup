"""Карточки Telegram: заголовки, формат суммы, поля по категориям."""

from app.sales.cards import build_card, fmt_sum, responsible_line, TS_PROMPT


def _data(**kw):
    base = dict(
        url="https://goszakup.gov.kz/ru/announce/index/17900000",
        anno="17900000-1", lot_number="88888888-ЗЦП1",
        customer='ТОО "Тест"', customer_bin="123456789012",
        name="Сервер стоечный", enstru="265012",
        amount=2_091_379, method="Запрос ценовых предложений",
        end_date="25.11.2026 09:00",
    )
    base.update(kw)
    return base


def test_fmt_sum_spaces():
    assert fmt_sum(2_091_379) == "2 091 379 ₸"
    assert fmt_sum(None) == "—"


def test_new_card_header_and_fields():
    card = build_card(_data(), "new")
    assert "🆕 НОВЫЙ ЛОТ" in card
    assert "2 091 379 ₸" in card
    assert "БИН 123456789012" in card
    assert "ЕНСТРУ 265012" in card
    assert TS_PROMPT in card
    assert "Ответственный: Не назначен" in card
    # у «нового» нет строки про родителя
    assert "Предыдущая закупка" not in card


def test_repeat_card_has_parent_link():
    card = build_card(_data(parent_url="https://goszakup.gov.kz/ru/announce/index/17800000"), "repeat")
    assert "🔄 ПОВТОРНАЯ ЗАКУПКА" in card
    assert "Предыдущая закупка" in card
    assert "17800000" in card


def test_failed_card_header():
    assert "НЕ СОСТОЯЛАСЬ" in build_card(_data(), "failed")


def test_responsible_line_assigned_and_skip():
    assert responsible_line("@ivan") == "👤 @ivan · 🟢 Взят в работу"
    assert responsible_line("@ivan", "Заявка подана") == "👤 @ivan · 🟢 Заявка подана"
    assert responsible_line("@ivan", "Тендер проигран") == "👤 @ivan · 🔴 Тендер проигран"
    assert responsible_line(None) == "👤 Ответственный: Не назначен"
    assert responsible_line("@ivan", status="skip") == "🚫 Не наш / Скип (@ivan)"


def test_card_escapes_html_in_customer():
    card = build_card(_data(customer="ООО <Рога & Копыта>"), "new")
    assert "&lt;Рога &amp; Копыта&gt;" in card
    assert "<Рога" not in card  # сырой угловой скобки нет
