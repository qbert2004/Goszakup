"""Карточки Telegram для Модулей 1/2/3 + промпт-блок проверки ТС.

Разметка — Telegram HTML (parse_mode=HTML). Промпт-блок — <blockquote expandable>,
чтобы менеджер в один клик скопировал чек-лист и проверил ТС в любом ИИ.
"""

from __future__ import annotations

import html

from .statuses import CLOSED, SKIP

HEADERS = {
    "new": "🆕 НОВЫЙ ЛОТ",
    "failed": "⚠️ НЕ СОСТОЯЛАСЬ (1-я закупка)",
    "repeat": "🔄 ПОВТОРНАЯ ЗАКУПКА",
}

# Фиксированный чек-лист из ТЗ (раздел 4). Expandable-цитата: свёрнута, копируется.
TS_PROMPT = (
    "<blockquote expandable><b>📋 Чек-лист / Промпт для проверки ТС в ИИ:</b>\n"
    "Проанализируй техническую спецификацию во вложении и ответь строго по 4 пунктам:\n"
    "1) Заточена ли ТС под конкретного вендора/модель или подходят аналоги "
    "(дай вердикт: Заточена / Частично / Открытая и перечисли конкретные требования-заточки);\n"
    "2) Это новая закупка или продление текущей лицензии/системы (укажи какой);\n"
    "3) Требуется ли авторизационное письмо от производителя (MAF) или партнёрский "
    "статус (приведи цитату из ТС);\n"
    "4) Главные риски по срокам, сертификатам (СТ-KZ, ИБ и т.д.) или условиям поставки.</blockquote>"
)


def fmt_sum(value) -> str:
    """Сумма с разделителями разрядов: 2 091 379 ₸."""
    try:
        return f"{float(value):,.0f} ₸".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def responsible_line(manager: str | None, status: str | None = None) -> str:
    """Строка ответственного и статуса (её бот редактирует при нажатии кнопок)."""
    if not manager:
        return "👤 Ответственный: Не назначен"
    if status in (SKIP, "skip"):
        return f"🚫 Не наш / Скип ({manager})"
    status = status or "Взят в работу"
    icon = "🔴" if status in CLOSED else "🟢"
    return f"👤 {manager} · {icon} {status}"


def plans_menu_text(selected: str = "", cnt: int = 0, total: float = 0.0, url: str = "") -> str:
    """Текст закреплённого меню планов (Модуль 5)."""
    import html as _html
    lines = ["📅 <b>Утверждённые планы закупок</b>", "", "Выберите месяц ниже:"]
    if selected:
        lines += [
            "",
            f"<b>{_html.escape(selected)}</b>",
            f"• Позиций: {cnt}",
            f"• Общая плановая сумма: {fmt_sum(total)}",
        ]
        if url:
            lines.append(f'\n📊 <a href="{_html.escape(url)}">Открыть полный список в Google Sheets</a>')
    return "\n".join(lines)


def build_card(data: dict, category: str) -> str:
    """Собрать HTML-текст карточки.

    Ожидаемые ключи data: url, anno, lot_number, customer, customer_bin, name,
    enstru(опц.), amount, method, end_date(опц.), parent_url(опц., для repeat),
    ts_url(опц.), manager(опц.), status(опц.).
    """
    h = html.escape
    lines = [f"<b>{HEADERS.get(category, '📌 ЛОТ')}</b>", ""]
    lines.append(
        f'📄 <a href="{h(data.get("url", ""))}">'
        f'{h(data.get("anno", ""))} · лот {h(data.get("lot_number", ""))}</a>'
    )
    lines.append(
        f'🏢 Заказчик: {h(data.get("customer", ""))} '
        f'(БИН {h(data.get("customer_bin", ""))})'
    )
    enstru = data.get("enstru")
    lines.append(
        f'📦 {h(data.get("name", ""))}'
        + (f' · ЕНСТРУ {h(str(enstru))}' if enstru else "")
    )
    lines.append(f'💰 {fmt_sum(data.get("amount"))}')
    if data.get("method"):
        lines.append(f'🧰 Способ: {h(data["method"])}')
    if data.get("end_date"):
        lines.append(f'⏳ Окончание приёма: {h(data["end_date"])}')
    if category == "repeat" and data.get("parent_url"):
        lines.append(f'🔗 Предыдущая закупка: <a href="{h(data["parent_url"])}">открыть</a>')
    if data.get("ts_url"):
        lines.append(f'📎 Документация / ТС: <a href="{h(data["ts_url"])}">скачать</a>')
    lines += ["", TS_PROMPT, ""]
    lines.append(responsible_line(data.get("manager"), data.get("status")))
    return "\n".join(lines)
