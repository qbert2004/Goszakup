"""Чтение публичной страницы объявления.

Количество поданных заявок нельзя получить из API: TrdApp отдаёт заявки только по
объявлениям с опубликованными итогами, а у активных лотов там всегда ноль (см. README).
Зато сам портал показывает счётчик открыто, во время приёма — и для ЗЦП, и для
открытого конкурса. Поэтому единственный источник числа заявок — эта страница.

Парсинг по своей природе хрупкий: изменится вёрстка — счётчик перестанет находиться.
Поэтому "не нашли" (None) строго отличается от "нашли ноль" (0). Ноль — повод
уведомить, None — повод промолчать и пожаловаться, а не выдать отсутствие данных
за отсутствие конкурентов.
"""

from __future__ import annotations

import re

import httpx

# 2026-09: goszakup.gov.kz мигрировал на новый SPA zakup.gov.kz, где число заявок
# доступно только за авторизацией (см. README). Но СТАРАЯ серверная версия жива на
# old.goszakup.gov.kz — те же страницы, тот же счётчик, для активных лотов, без
# редиректа. Пока old жив — берём счётчик оттуда (проверено: 200, полная страница,
# "Кол-во поданных заявок" парсится). Если old закроют — счётчик снова пропадёт.
ANNOUNCE_URL = "https://old.goszakup.gov.kz/ru/announce/index/{id}"
LOTS_URL = "https://old.goszakup.gov.kz/ru/announce/index/{id}?tab=lots"

COUNTER_RE = re.compile(r"Кол-во поданных заявок:\s*(\d+)")


def lot_link(trd_buy_id: int) -> str:
    return LOTS_URL.format(id=trd_buy_id)


def application_count(trd_buy_id: int, timeout: float = 30.0) -> int | None:
    """Число поданных заявок по объявлению.

    None — счётчика на странице нет либо страница недоступна. Это НЕ ноль.
    """
    try:
        response = httpx.get(
            ANNOUNCE_URL.format(id=trd_buy_id),
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "goszakup-monitor/1.0"},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None

    match = COUNTER_RE.search(response.text)
    return int(match.group(1)) if match else None
