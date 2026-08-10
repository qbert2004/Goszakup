"""Выгрузка планов закупок: Западный Казахстан, госорганы, наши позиции.

Отдельная задача от мониторинга лотов: там мы ловим объявления с 0 заявок,
здесь — смотрим планы, то есть что заказчики только собираются закупать.

Как отбираем (каждое звено проверено на живых данных):

    коды ЕНСТРУ из таблицы
      -> Plans(refEnstruCode: код)         единственный серверный фильтр по позиции
      -> PlansKato.refKatoCode[:2]         регион: фильтра по нему в API НЕТ,
                                            отбираем у себя
      -> Subject.refKopfCode               правовая форма заказчика: ГУ = госорган
      -> plnPointYear                      год плана

Почему регион фильтруем у себя: в PlansFiltersInput нет ни КАТО, ни региона —
только позиция, заказчик, год, месяц, статус и способ закупки.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .goszakup import GoszakupClient, GoszakupError
from .store import connect

# Первые две цифры КАТО. Подтверждено живыми данными: у каждого пункта плана
# место поставки называется прямо ("Атырауская область" при 23...).
WEST_KATO = {
    "15": "Актюбинская область",
    "23": "Атырауская область",
    "27": "Западно-Казахстанская область",
    "47": "Мангистауская область",
}

# Правовые формы, которые считаем госорганами. ГУ — государственное учреждение:
# аппараты акимов, департаменты, управления, отделы. Остальные формы (ТОО, АО,
# НАО) — не госорганы, даже если они заказчики.
GOV_FORMS = {"ГУ", "КГУ", "РГУ", "ГП", "ГККП", "КГКП", "РГКП", "ГКП", "КГП", "РГП"}
STRICT_GOV_FORMS = {"ГУ", "КГУ", "РГУ"}  # только органы власти, без предприятий

PAGE_LIMIT = 200

PLANS_QUERY = """
query ($f: PlansFiltersInput, $limit: Int, $after: Int) {
  Plans(filter: $f, limit: $limit, after: $after) {
    id
    subjectBiin
    subjectNameRu
    nameRu
    descRu
    refEnstruCode
    count
    price
    amount
    plnPointYear
    refUnitsCode
    RefUnits { nameRu }
    RefTradeMethods { nameRu }
    RefMonths { nameRu }
    RefPlnPointStatus { nameRu }
    PlansKato { refKatoCode fullDeliveryPlaceNameRu }
  }
}
"""

SUBJECT_QUERY = """
query ($f: SubjectFiltersInput) {
  Subjects(filter: $f, limit: 1) { bin nameRu refKopfCode }
}
"""

COLUMNS = [
    "ID плана",
    "Заказчик",
    "БИН",
    "Форма",
    "Регион",
    "Наименование ЕНСТРУ",
    "Код ЕНСТРУ",
    "Характеристика",
    "Способ закупки",
    "Ед. измерения",
    "Количество",
    "Цена за единицу",
    "Сумма",
    "Месяц",
    "Статус",
    "Год",
    "Ссылка",
]

PLAN_URL = "https://goszakup.gov.kz/ru/egzplans/index?filter[plan_number]={id}"


@dataclass
class PlansResult:
    rows: list = field(default_factory=list)
    scanned: int = 0          # всего пунктов плана просмотрено
    in_region: int = 0        # из них по Западному Казахстану
    dropped_form: int = 0     # отсеяно: заказчик не госорган
    dropped_year: int = 0     # отсеяно: не тот год
    customers: dict = field(default_factory=dict)   # БИН -> форма
    by_region: dict = field(default_factory=dict)
    by_form: dict = field(default_factory=dict)


def _ensure_kopf_cache() -> None:
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subject_form ("
            " bin TEXT PRIMARY KEY, name TEXT, kopf TEXT)"
        )


def _kopf_of(client: GoszakupClient, bin_: str) -> str | None:
    """Правовая форма заказчика. Кэшируем: форма не меняется, а БИНов сотни."""
    if not bin_:
        return None
    _ensure_kopf_cache()
    with connect() as conn:
        row = conn.execute(
            "SELECT kopf FROM subject_form WHERE bin = ?", (bin_,)
        ).fetchone()
        if row:
            return row["kopf"] or None

    try:
        found = client.query(SUBJECT_QUERY, {"f": {"bin": bin_}}).get("Subjects") or []
    except GoszakupError:
        return None

    kopf = (found[0].get("refKopfCode") or "").strip() if found else ""
    name = (found[0].get("nameRu") or "") if found else ""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subject_form (bin, name, kopf) VALUES (?,?,?)",
            (bin_, name, kopf),
        )
    return kopf or None


def _region_of(point: dict) -> tuple[str, str] | None:
    """Регион пункта плана по КАТО места поставки."""
    for kato in point.get("PlansKato") or []:
        code = str(kato.get("refKatoCode") or "")
        if code[:2] in WEST_KATO:
            return WEST_KATO[code[:2]], kato.get("fullDeliveryPlaceNameRu") or ""
    return None


def _fetch_points(client: GoszakupClient, code: str, year: int) -> list[dict]:
    """Все пункты плана по коду ЕНСТРУ за год, с пагинацией.

    Год фильтруем на сервере: планы копятся с 2019-го, и без этого мы тянули бы
    десятки тысяч устаревших пунктов ради сотни актуальных.

    Пагинация обязательна: на популярную позицию приходится больше 200 пунктов,
    а без неё мы молча брали бы только первую страницу.
    """
    out: list[dict] = []
    after: int | None = None
    for _ in range(40):  # предохранитель от бесконечной прокрутки
        try:
            page = client.query(
                PLANS_QUERY,
                {
                    "f": {"refEnstruCode": code, "plnPointYear": year},
                    "limit": PAGE_LIMIT,
                    "after": after,
                },
            ).get("Plans") or []
        except GoszakupError:
            break
        if not page:
            break
        out.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        after = int(page[-1]["id"])
    return out


def collect(
    client: GoszakupClient,
    codes: list[dict],
    year: int,
    forms: set[str] | None = None,
    on_progress=None,
) -> PlansResult:
    """Собирает планы по нашим позициям: регион + госорганы + год."""
    forms = forms or STRICT_GOV_FORMS
    result = PlansResult()
    seen_points: set[int] = set()

    for index, item in enumerate(codes, 1):
        code = item["code"]
        if on_progress:
            on_progress(index, len(codes), code)

        for point in _fetch_points(client, code, year):
            point_id = int(point["id"])
            if point_id in seen_points:
                continue
            seen_points.add(point_id)
            result.scanned += 1

            region = _region_of(point)
            if not region:
                continue
            result.in_region += 1

            if point.get("plnPointYear") != year:
                result.dropped_year += 1
                continue

            bin_ = point.get("subjectBiin") or ""
            kopf = _kopf_of(client, bin_)
            result.customers[bin_] = kopf or "?"
            result.by_form[kopf or "?"] = result.by_form.get(kopf or "?", 0) + 1
            if kopf not in forms:
                result.dropped_form += 1
                continue

            region_name, place = region
            result.by_region[region_name] = result.by_region.get(region_name, 0) + 1
            result.rows.append(
                {
                    "ID плана": point_id,
                    "Заказчик": point.get("subjectNameRu") or "",
                    "БИН": bin_,
                    "Форма": kopf,
                    "Регион": region_name,
                    "Наименование ЕНСТРУ": point.get("nameRu") or "",
                    "Код ЕНСТРУ": point.get("refEnstruCode") or "",
                    "Характеристика": (point.get("descRu") or "").strip(),
                    "Способ закупки": (point.get("RefTradeMethods") or {}).get("nameRu", ""),
                    "Ед. измерения": (point.get("RefUnits") or {}).get("nameRu", ""),
                    "Количество": point.get("count"),
                    "Цена за единицу": point.get("price"),
                    "Сумма": point.get("amount"),
                    "Месяц": (point.get("RefMonths") or {}).get("nameRu", ""),
                    "Статус": (point.get("RefPlnPointStatus") or {}).get("nameRu", ""),
                    "Год": point.get("plnPointYear"),
                    "Ссылка": PLAN_URL.format(id=point_id),
                }
            )
    return result


def to_csv(rows: list[dict], path: Path, columns: list[str] | None = None) -> Path:
    """CSV для импорта в Google Sheets.

    utf-8-sig — иначе Excel и Google Sheets ломают кириллицу при открытии файла.
    columns задаётся явно, когда набор колонок другой (например, выгрузка закупок).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns or COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path
