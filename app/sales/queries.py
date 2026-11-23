"""GraphQL-запросы и классификация лотов/планов для Модулей 1/2/3/5.

Классификация по данным, которые API отдаёт нашим токеном (проверено интроспекцией):
  * статус закупки   — TrdBuy.refBuyStatusId / RefBuyStatus.nameRu;
  * повторность      — TrdBuy.parentId (ссылка на родительскую закупку);
  * несостоявшаяся   — статус содержит «не состоял»;
  * планы            — Plans -> PlnPoint (refMonthsId = месяц, refPlnPointStatusId = статус).

Счётчик заявок (TrdApp) НЕ используется — закрыт для токена.
"""

from __future__ import annotations

# Статусы, когда идёт приём: 210 Опубликовано · 220 приём заявок · 240 приём ц/п.
RECEIVING_STATUSES = {210, 220, 240}

# Страница объявления. old.goszakup убран; основной домен редиректит на новую
# платформу — ссылку держим здесь одной точкой, чтобы поменять, когда выяснится
# формат URL на zakup.gov.kz.
ANNOUNCE_URL = "https://goszakup.gov.kz/ru/announce/index/{id}"


def announce_link(trd_buy_id) -> str:
    return ANNOUNCE_URL.format(id=trd_buy_id)


# Лоты: всё нужное для карточек и классификации одним запросом.
LOTS_QUERY = """
query ($f: LotsFiltersInput, $limit: Int, $after: Int) {
  Lots(filter: $f, limit: $limit, after: $after) {
    id
    lotNumber
    nameRu
    amount
    customerNameRu
    customerBin
    trdBuyId
    pointList
    enstruList
    TrdBuy {
      id
      numberAnno
      startDate
      endDate
      itogiDatePublic
      refBuyStatusId
      refTradeMethodsId
      parentId
      finYear
      RefTradeMethods { nameRu }
      RefBuyStatus { nameRu }
    }
  }
}
"""

# Пункты планов (Модуль 5). refMonthsId -> меню месяцев, refPlnPointStatusId -> «Утверждён».
PLANS_QUERY = """
query ($f: PlansFiltersInput, $limit: Int, $after: Int) {
  Plans(filter: $f, limit: $limit, after: $after) {
    id
    nameRu
    amount
    subjectNameRu
    subjectBiin
    refEnstruCode
    refMonthsId
    plnPointYear
    refTradeMethodsId
    refPlnPointStatusId
    RefMonths { nameRu }
    RefTradeMethods { nameRu }
    RefPlnPointStatus { nameRu }
  }
}
"""


def _has_parent(buy: dict) -> bool:
    """Есть ли родительская закупка (признак повторной)."""
    pid = buy.get("parentId")
    return bool(pid) and str(pid) not in ("0", "")


def category(buy: dict, year) -> str | None:
    """Категория закупки по её TrdBuy: new | failed | repeat | None.

      * failed — статус «не состоялась», ПЕРВИЧНАЯ (без родителя), текущего года;
      * repeat — идёт приём и есть родительская закупка;
      * new    — идёт приём, родителя нет;
      * None   — нам не интересна (итоги подведены, отменена, не тот год и т.п.).
    """
    status_name = ((buy.get("RefBuyStatus") or {}).get("nameRu") or "").lower()
    if "не состоял" in status_name:
        if not _has_parent(buy) and str(buy.get("finYear")) == str(year):
            return "failed"
        return None
    if buy.get("refBuyStatusId") in RECEIVING_STATUSES:
        return "repeat" if _has_parent(buy) else "new"
    return None


def is_approved_plan(point: dict) -> bool:
    """Утверждён ли пункт плана (статус содержит «утвержд»)."""
    name = ((point.get("RefPlnPointStatus") or {}).get("nameRu") or "").lower()
    return "утвержд" in name
