"""Слой Google Sheets: одна таблица, 4 вкладки (ТЗ раздел 2).

gspread импортируется ЛЕНИВО — модуль грузится и тестируется без установленной
библиотеки; она нужна только при реальном обращении к таблице. Чистые мапперы
(строки вкладки -> FilterConfig, данные лота -> строка воронки) вынесены отдельно
и покрыты тестами; тонкая обёртка `Workbook` делает ввод-вывод через gspread.

Внутренняя БД (SQLite) остаётся технической памятью от дублей; Google Sheets —
рабочая и аналитическая база команды.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .filters import FilterConfig

ASTANA = timezone(timedelta(hours=5))

# Названия вкладок
FILTER_TAB = "Настройки_Фильтров"
FUNNEL_TAB = "Воронка_Лотов"
DEALS_TAB = "База_Сделок"
PLANS_TAB = "Планы_Закупок"

# Человекочитаемая категория для воронки
CATEGORY_RU = {"new": "Новый", "failed": "Несостоявшийся", "repeat": "Повторный"}

FUNNEL_HEADERS = [
    "Дата публикации", "Категория", "№ объявления и лота", "Заказчик", "БИН заказчика",
    "Предмет закупки", "КТРУ", "Сумма", "Дедлайн приёма", "Ответственный", "Статус",
]
DEALS_HEADERS = [
    "Дата итогов/договора", "№ объявления и лота", "Заказчик", "БИН заказчика",
    "Предмет закупки", "Код КТРУ", "Плановая сумма", "Итоговая сумма", "Снижение %",
    "Победитель", "БИН победителя", "Второе место",
]
PLANS_HEADERS = [
    "ID пункта плана", "Год", "Планируемый месяц", "Заказчик", "БИН заказчика",
    "Предмет закупки", "Код КТРУ", "Плановая сумма", "Способ закупки", "Ссылка",
]


def _hyperlink(url: str, text: str) -> str:
    """Формула-гиперссылка для ячейки (value_input_option=USER_ENTERED)."""
    if not url:
        return text or ""
    safe = (text or url).replace('"', "'")
    return f'=HYPERLINK("{url}";"{safe}")'


def _now_astana() -> str:
    return datetime.now(ASTANA).strftime("%Y-%m-%d %H:%M")


# ------------------------- чистые мапперы (тестируемые) -------------------------

def filter_config_from_values(values: list[list]) -> FilterConfig:
    """FilterConfig из «сырых» строк вкладки «Настройки_Фильтров».

    Колонки: A=KTRU, B=Keywords, C=Stop_words, D=Min_sum, E=Managers. Первая строка —
    заголовки, пропускаем. Min_sum берём из первой непустой ячейки колонки D.
    """
    rows = values[1:] if values else []

    def col(i):
        return [r[i] for r in rows if len(r) > i and str(r[i]).strip()]

    min_sum = 0
    for r in rows:
        if len(r) > 3 and str(r[3]).strip():
            min_sum = r[3]
            break
    return FilterConfig.from_columns(
        ktru_col=col(0),
        keywords_col=col(1),
        stop_col=col(2),
        min_sum=min_sum,
        managers_col=col(4),
    )


def funnel_row(data: dict, category: str, manager: str = "", status: str = "В работе") -> list:
    """Строка для вкладки «Воронка_Лотов» из данных лота."""
    marker = f'{data.get("anno", "")} · лот {data.get("lot_number", "")}'
    return [
        _now_astana(),
        CATEGORY_RU.get(category, category),
        _hyperlink(data.get("url", ""), marker),
        data.get("customer", ""),
        str(data.get("customer_bin", "")),
        data.get("name", ""),
        str(data.get("enstru", "")),
        data.get("amount") or 0,
        data.get("end_date", ""),
        manager,
        status,
    ]


def deal_row(deal: dict) -> list:
    """Строка для вкладки «База_Сделок» (Модуль 4)."""
    marker = f'{deal.get("anno", "")} · лот {deal.get("lot_number", "")}'
    return [
        deal.get("itogi", ""),
        _hyperlink(deal.get("url", ""), marker),
        deal.get("customer", ""),
        str(deal.get("customer_bin", "")),
        deal.get("name", ""),
        str(deal.get("enstru", "")),
        deal.get("plan_sum") or 0,
        deal.get("contract_sum") or 0,
        deal.get("reduction", ""),
        deal.get("winner", ""),
        str(deal.get("winner_bin", "")),
        deal.get("second", ""),
    ]


def plan_row(point: dict) -> list:
    """Строка для вкладки «Планы_Закупок» из пункта плана (PlnPoint)."""
    from .queries import announce_link  # ссылка на закупку пока через announce id нет — на план
    month = (point.get("RefMonths") or {}).get("nameRu") or point.get("refMonthsId")
    method = (point.get("RefTradeMethods") or {}).get("nameRu") or ""
    return [
        str(point.get("id", "")),
        point.get("plnPointYear", ""),
        month,
        point.get("subjectNameRu", ""),
        str(point.get("subjectBiin", "")),
        point.get("nameRu", ""),
        str(point.get("refEnstruCode", "")),
        point.get("amount") or 0,
        method,
        "",  # прямой ссылки на пункт плана в API нет — оставляем пустой/добавим позже
    ]


# ------------------------------ обёртка gspread --------------------------------

class Workbook:
    """Тонкая обёртка над одной Google-таблицей. gspread грузится лениво."""

    def __init__(self, keyfile_path: str, spreadsheet_id: str):
        import gspread  # ленивый импорт

        self._gc = gspread.service_account(filename=keyfile_path)
        self._sh = self._gc.open_by_key(spreadsheet_id)
        self._cache: dict = {}

    def _ws(self, title: str, headers: list | None = None):
        if title in self._cache:
            return self._cache[title]
        try:
            ws = self._sh.worksheet(title)
        except Exception:  # noqa: BLE001 — вкладки нет -> создаём
            ws = self._sh.add_worksheet(title=title, rows=1000, cols=max(12, len(headers or [])))
            if headers:
                ws.append_row(headers, value_input_option="USER_ENTERED")
        self._cache[title] = ws
        return ws

    def read_filters(self) -> FilterConfig:
        return filter_config_from_values(self._ws(FILTER_TAB).get_all_values())

    def append_funnel(self, rows: list[list]) -> None:
        if rows:
            self._ws(FUNNEL_TAB, FUNNEL_HEADERS).append_rows(rows, value_input_option="USER_ENTERED")

    def append_deals(self, rows: list[list]) -> None:
        if rows:
            self._ws(DEALS_TAB, DEALS_HEADERS).append_rows(rows, value_input_option="USER_ENTERED")

    def sync_plans(self, rows: list[list]) -> None:
        """Перезаписать вкладку планов (шапка + свежие строки)."""
        ws = self._ws(PLANS_TAB, PLANS_HEADERS)
        ws.clear()
        ws.append_row(PLANS_HEADERS, value_input_option="USER_ENTERED")
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    def set_funnel_responsible(self, marker: str, manager: str, status: str) -> bool:
        """Найти строку воронки по маркеру (№ объявления · лот) и проставить ответственного.

        Возвращает True, если строка найдена и обновлена. Нужна Telegram-слою при
        назначении менеджера кнопкой.
        """
        ws = self._ws(FUNNEL_TAB, FUNNEL_HEADERS)
        try:
            cell = ws.find(marker)
        except Exception:  # noqa: BLE001
            cell = None
        if not cell:
            return False
        ws.update_cell(cell.row, 10, manager)  # «Ответственный»
        ws.update_cell(cell.row, 11, status)   # «Статус»
        return True
