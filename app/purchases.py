"""Выгрузка объявленных закупок: Западный Казахстан, госорганы, наши позиции.

Не путать с app/plans.py: там планы (что заказчик только собирается покупать),
здесь — уже объявленные лоты, на которые подают заявки.

Как отбираем (проверено на живых данных):

    названия позиций из справочника
      -> Lots(nameDescriptionRu: название)   единственный серверный фильтр по позиции
      -> plnPointKatoList / Customer.katoList  регион
      -> Customer.refKopfCode                правовая форма: ГУ = госорган
      -> TrdBuy.endDate                      глубина выборки

Регион берём из двух источников. Основной — КАТО места поставки
(`plnPointKatoList`), но у 14% лотов оно пустое, и тогда используем регион самого
заказчика (`Customer.katoList`). Замер: без запасного источника терялось 36%
западных лотов.

Фильтр `plnPointKatoList` на сервере работает, но требует ТОЧНЫЙ код места
("271010000"), а не префикс области ("27" и "270000000" возвращают ноль). Кодов
внутри области сотни, поэтому фильтруем у себя по первым двум цифрам.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .goszakup import GoszakupClient, GoszakupError
from .plans import GOV_FORMS, STRICT_GOV_FORMS, WEST_KATO, to_csv

PAGE_LIMIT = 200
MAX_PAGES = 12  # предохранитель: без него уйдём в архив на годы назад

LOTS_QUERY = """
query ($f: LotsFiltersInput, $limit: Int, $after: Int) {
  Lots(filter: $f, limit: $limit, after: $after) {
    id
    lotNumber
    nameRu
    descriptionRu
    amount
    count
    customerBin
    customerNameRu
    trdBuyNumberAnno
    trdBuyId
    plnPointKatoList
    RefLotsStatus { nameRu }
    Customer { katoList refKopfCode }
    TrdBuy {
      numberAnno
      startDate
      endDate
      itogiDatePublic
      refBuyStatusId
      RefTradeMethods { nameRu }
    }
  }
}
"""

COLUMNS = [
    "ID лота",
    "Номер лота",
    "Объявление",
    "Заказчик",
    "БИН",
    "Форма",
    "Регион",
    "Наименование",
    "Характеристика",
    "Способ закупки",
    "Количество",
    "Сумма",
    "Статус лота",
    "Начало приёма",
    "Окончание приёма",
    "Приём идёт",
    "Ссылка",
]

LOT_URL = "https://goszakup.gov.kz/ru/announce/index/{buy}?tab=lots"


@dataclass
class Result:
    rows: list = field(default_factory=list)
    scanned: int = 0
    in_region: int = 0
    dropped_form: int = 0
    by_region: dict = field(default_factory=dict)
    by_form: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    active: int = 0


def _region_of(lot: dict) -> tuple[str, str] | None:
    """Регион лота.

    Решает МЕСТО ПОСТАВКИ: закупка относится к тому региону, куда поедет товар.
    Регион заказчика — только запасной путь, когда место поставки не указано
    (14% лотов; замер: даёт треть западных находок).

    Если место поставки задано и оно не западное — лот не наш, даже когда сам
    заказчик прописан на западе. На живых данных таких случаев ноль, но правило
    должно быть однозначным.
    """
    place = (lot.get("plnPointKatoList") or [""])[0] or ""
    if place:
        if place[:2] in WEST_KATO:
            return WEST_KATO[place[:2]], "место поставки"
        return None

    home = ((lot.get("Customer") or {}).get("katoList") or [""])[0] or ""
    if home[:2] in WEST_KATO:
        return WEST_KATO[home[:2]], "регион заказчика"
    return None


def _fetch(client: GoszakupClient, name: str, since: str) -> list[dict]:
    """Лоты позиции, вглубь до даты since (YYYY-MM-DD).

    Лоты отдаются от новых к старым, поэтому останавливаемся, как только вся
    страница оказалась старше нужной даты. Без этого уходим в архив на годы:
    у популярной позиции больше 1200 лотов, и почти все давно завершены.
    """
    out: list[dict] = []
    after: int | None = None

    for _ in range(MAX_PAGES):
        try:
            page = client.query(
                LOTS_QUERY,
                {"f": {"nameDescriptionRu": name}, "limit": PAGE_LIMIT, "after": after},
            ).get("Lots") or []
        except GoszakupError:
            break
        if not page:
            break
        out.extend(page)

        dates = [
            (lot.get("TrdBuy") or {}).get("endDate") or "" for lot in page
        ]
        newest = max(dates) if dates else ""
        if newest and newest[:10] < since:
            break  # дальше только глубже в прошлое
        if len(page) < PAGE_LIMIT:
            break
        after = int(page[-1]["id"])
    return out


def collect(
    client: GoszakupClient,
    names: list[str],
    since: str,
    forms: set[str] | None = None,
    on_progress=None,
) -> Result:
    forms = forms or GOV_FORMS
    result = Result()
    seen: set[int] = set()

    for index, name in enumerate(sorted(names), 1):
        if on_progress:
            on_progress(index, len(names), name)

        for lot in _fetch(client, name, since):
            lot_id = int(lot["id"])
            if lot_id in seen:
                continue
            seen.add(lot_id)
            result.scanned += 1

            buy = lot.get("TrdBuy") or {}
            end = (buy.get("endDate") or "")[:10]
            if end and end < since:
                continue

            region = _region_of(lot)
            if not region:
                continue
            result.in_region += 1
            region_name, source = region
            result.by_source[source] = result.by_source.get(source, 0) + 1

            kopf = ((lot.get("Customer") or {}).get("refKopfCode") or "").strip()
            result.by_form[kopf or "?"] = result.by_form.get(kopf or "?", 0) + 1
            if kopf not in forms:
                result.dropped_form += 1
                continue

            # Приём идёт: итоги ещё не опубликованы и дедлайн в будущем.
            active = bool(
                not buy.get("itogiDatePublic")
                and buy.get("endDate")
                and buy["endDate"] > datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            if active:
                result.active += 1

            result.by_region[region_name] = result.by_region.get(region_name, 0) + 1
            result.rows.append(
                {
                    "ID лота": lot_id,
                    "Номер лота": lot.get("lotNumber") or "",
                    "Объявление": buy.get("numberAnno") or lot.get("trdBuyNumberAnno") or "",
                    "Заказчик": lot.get("customerNameRu") or "",
                    "БИН": lot.get("customerBin") or "",
                    "Форма": kopf,
                    "Регион": region_name,
                    "Наименование": lot.get("nameRu") or "",
                    "Характеристика": (lot.get("descriptionRu") or "").strip(),
                    "Способ закупки": (buy.get("RefTradeMethods") or {}).get("nameRu", ""),
                    "Количество": lot.get("count"),
                    "Сумма": lot.get("amount"),
                    "Статус лота": (lot.get("RefLotsStatus") or {}).get("nameRu", ""),
                    "Начало приёма": buy.get("startDate") or "",
                    "Окончание приёма": buy.get("endDate") or "",
                    "Приём идёт": "да" if active else "нет",
                    "Ссылка": LOT_URL.format(buy=lot.get("trdBuyId")),
                }
            )
    return result


def export(rows: list[dict], path: Path) -> Path:
    return to_csv(rows, path, columns=COLUMNS)
