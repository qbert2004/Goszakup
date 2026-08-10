"""Список ЕНСТРУ из Google-таблицы.

Заказчик пополняет список прямо в таблице — код при этом не трогаем.
Таблице нужен доступ "Просматривать могут все, у кого есть ссылка".
"""

from __future__ import annotations

import csv
import io
import re

import httpx

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
GID_RE = re.compile(r"[#&?]gid=([0-9]+)")
CODE_RE = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")

CODE_COLUMNS = ("код енстру", "код", "енстру", "code")
NAME_COLUMNS = ("наименование енстру", "наименование", "название", "name")


class SheetError(RuntimeError):
    pass


def csv_url(url: str) -> str:
    """Ссылку вида .../edit?usp=sharing превращает в ссылку на CSV-экспорт."""
    match = SHEET_ID_RE.search(url)
    if not match:
        raise SheetError(
            "Не похоже на ссылку Google-таблицы — нужен адрес вида "
            "https://docs.google.com/spreadsheets/d/<id>/edit"
        )
    export = f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv"
    gid = GID_RE.search(url)
    return f"{export}&gid={gid.group(1)}" if gid else export


def _pick(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    for name in fieldnames:
        if name and name.strip().lower() in candidates:
            return name
    return None


def load_codes(url: str) -> list[dict]:
    """Возвращает [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}, ...]."""
    try:
        response = httpx.get(csv_url(url), timeout=60.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SheetError(
            f"Не удалось скачать таблицу: {exc}. Проверьте, что доступ открыт "
            "по ссылке ('Просматривать могут все, у кого есть ссылка')."
        ) from exc

    if "text/csv" not in response.headers.get("content-type", ""):
        raise SheetError(
            "Google вернул не CSV, а страницу входа — значит таблица закрыта. "
            "Откройте доступ по ссылке на просмотр."
        )

    response.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(response.text))
    if not reader.fieldnames:
        raise SheetError("Таблица пустая — нет даже заголовков.")

    code_column = _pick(reader.fieldnames, CODE_COLUMNS)
    if not code_column:
        raise SheetError(
            f"Не нашёл столбец с кодом ЕНСТРУ. Есть столбцы: {reader.fieldnames}. "
            f"Назовите нужный один из: {', '.join(CODE_COLUMNS)}."
        )
    name_column = _pick(reader.fieldnames, NAME_COLUMNS)

    items: dict[str, str] = {}
    skipped: list[str] = []
    for row in reader:
        code = (row.get(code_column) or "").strip()
        if not code:
            continue
        if not CODE_RE.match(code):
            skipped.append(code)
            continue
        items.setdefault(code, (row.get(name_column) or "").strip() if name_column else "")

    if not items:
        raise SheetError(
            "В таблице нет ни одного кода ЕНСТРУ формата 123456.789.000000."
            + (f" Похожие непонятные значения: {skipped[:5]}" if skipped else "")
        )
    return [{"code": code, "name": name} for code, name in items.items()]
