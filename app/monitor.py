"""Обход площадки: найти лоты по нашим ЕНСТРУ, у которых 0 заявок и скоро дедлайн.

Как ищем (каждое звено проверено вживую, см. README):

    коды ЕНСТРУ из таблицы
      -> Plans(refEnstruCode).RefEnstru.nameRu ОФИЦИАЛЬНОЕ название позиции
      -> Lots(nameDescriptionRu: название)     ищем кандидатов (сервер-фильтр только такой)
      -> Lots.TrdBuy.endDate                   дедлайн (фильтра по нему в API нет)
      -> window.should_alert() + сумма         пора ли уведомлять и стоит ли того
      -> pointList -> Plans.refEnstruCode      РЕШАЕТ КОД: наш -> берём, чужой -> мимо
      -> (код не сверился) точное имя          запасной критерий, он же отсев мусора
      -> portal.application_count()            число заявок (только со страницы)
      -> заявок <= порога  =>  в Telegram      порог 0 = нулевая конкуренция (ТЗ)

Название — способ найти кандидатов, а не критерий отбора. Когда было наоборот,
терялись лоты, названные на портале иначе, чем позиция в таблице: замер показал
5 потерь на 256 лотов ("Комплекс оборудования" вместо "Комплекс оборудования
видеонаблюдения", код при этом наш).

Почему не через Lots.enstruList, хотя поле для этого и создано: оно не заполнено.
Замер: из 200 лотов "Коммутатор сетевой" у 199 там [0], реальный код проставлен у
одного. Отбор по нему находил 2 лота на все 128 кодов вместо десятков. Настоящая
связь лота с ЕНСТРУ идёт через пункт плана (pointList -> Plans.refEnstruCode).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import portal, store, window
from .config import Settings
from .goszakup import GoszakupClient, GoszakupError

# Из одного источника по несостоявшимся закупкам: конкуренции нет по определению,
# и счётчика заявок у таких объявлений на странице тоже нет.
METHOD_SINGLE_SOURCE = 6

# "Опубликовано" — объявление есть, но приём заявок ещё не открыт. Счётчик на
# странице появляется только с началом приёма (статусы 220 и 240), поэтому такие
# лоты пропускаем: подать заявку всё равно нельзя, а нуль там бессмысленный.
STATUS_NOT_STARTED = 210

PAGE_LIMIT = 200

# Сколько позиций подряд должны упасть (при нуле успешных), чтобы счесть площадку
# лежачей и прервать проход, а не ждать ~45 минут ретраев по всем 89 позициям.
PLATFORM_DOWN_STREAK = 5

LOTS_QUERY = """
query ($f: LotsFiltersInput, $limit: Int) {
  Lots(filter: $f, limit: $limit) {
    id
    lotNumber
    nameRu
    amount
    customerNameRu
    trdBuyId
    pointList
    TrdBuy {
      id
      numberAnno
      endDate
      itogiDatePublic
      refTradeMethodsId
      refBuyStatusId
      RefTradeMethods { nameRu }
    }
  }
}
"""

POINT_QUERY = """
query ($f: PlansFiltersInput) {
  Plans(filter: $f, limit: 1) { id refEnstruCode descRu }
}
"""

ENSTRU_NAME_QUERY = """
query ($f: PlansFiltersInput) {
  Plans(filter: $f, limit: 1) {
    refEnstruCode
    RefEnstru { id nameRu }
  }
}
"""


def official_names(client: GoszakupClient, codes: list[dict]) -> tuple[set[str], list[dict]]:
    """Официальные названия позиций по кодам из таблицы.

    Искать надо именно по ним, а не по названиям из таблицы: заказчик пишет их
    как удобно и сокращает. Замер: 7 кодов из 128 названы в таблице иначе, чем на
    площадке ("Услуги консультационные в области ИТ" против "...в области
    информационных технологий"), и по этим названиям на портале не находится ничего.
    Таблица — источник кодов; как позиция называется, знает только площадка.

    Возвращает (названия для поиска, список расхождений для отчёта).
    """
    names: set[str] = set()
    mismatches: list[dict] = []

    for item in codes:
        code = item["code"]
        cached = store.cached_enstru(code)
        if cached is None:
            try:
                rows = client.query(
                    ENSTRU_NAME_QUERY, {"f": {"refEnstruCode": code}}
                ).get("Plans") or []
            except GoszakupError:
                continue
            ref = (rows[0].get("RefEnstru") or {}) if rows else {}
            official = (ref.get("nameRu") or "").strip()
            store.cache_enstru(code, ref.get("id"), official)
            cached = {"enstru_id": ref.get("id"), "name": official}

        official = (cached.get("name") or "").strip()
        if not official:
            # Справочник промолчал — ищем хотя бы по названию из таблицы.
            if item.get("name"):
                names.add(item["name"].strip())
            continue

        names.add(official)
        ours = (item.get("name") or "").strip()
        if ours and ours.lower() != official.lower():
            mismatches.append({"code": code, "sheet": ours, "official": official})

    return names, mismatches


@dataclass
class Result:
    matched: int = 0            # лотов в окне уведомления
    passed: int = 0             # из них прошли порог по числу заявок
    notified: int = 0           # реально отправлено (без дублей)
    wrong_code: int = 0         # код сверен и он не из нашей таблицы
    unverified: int = 0         # код не сверился, взяли по точному имени
    name_mismatch: int = 0      # код не сверился и имя не наше — мусор из поиска
    unreadable: list = field(default_factory=list)  # счётчик не прочитался
    name_fixes: list = field(default_factory=list)  # в таблице имя не как у площадки
    lots: list = field(default_factory=list)


def _enstru_code_of(client: GoszakupClient, lot: dict) -> str | None:
    """Точный код ЕНСТРУ лота через его пункт плана. None — сверить не удалось.

    Сверяется примерно половина лотов: пункты планов свежих лотов в индексе Plans
    ещё отсутствуют. Поэтому сверка — уточнение, а не основа отбора.
    """
    points = lot.get("point_list") or lot.get("pointList") or []
    if not points:
        return None
    try:
        rows = client.query(POINT_QUERY, {"f": {"id": points[0]}}).get("Plans") or []
    except GoszakupError:
        return None
    return rows[0].get("refEnstruCode") if rows else None


def _in_window(lot: dict, settings: Settings) -> dict | None:
    buy = lot.get("TrdBuy") or {}

    amount = lot.get("amount")
    if settings.min_amount:
        if amount is None:
            return None  # сумма неизвестна — при заданном пороге не рискуем шуметь
        if amount < settings.min_amount:
            return None  # мелочь: дёргать менеджера ради неё не стоит

    if buy.get("itogiDatePublic"):
        return None  # итоги подведены — поезд ушёл
    if buy.get("refTradeMethodsId") == METHOD_SINGLE_SOURCE:
        return None  # неконкурентная процедура, счётчика всё равно нет
    if buy.get("refBuyStatusId") == STATUS_NOT_STARTED:
        return None  # приём ещё не открыт — поймаем, когда откроется
    if not buy.get("endDate"):
        return None

    end = window.parse(buy["endDate"])
    moment = window.now()
    if not window.should_alert(
        end,
        moment,
        lead_hours=settings.window_hours_min,
        respect_working_hours=settings.respect_working_hours,
    ):
        return None

    return {
        "lot_id": int(lot["id"]),
        "trd_buy_id": int(buy["id"]),
        "lot_number": lot.get("lotNumber"),
        "name": lot.get("nameRu"),
        "customer": lot.get("customerNameRu"),
        "amount": lot.get("amount"),
        "number_anno": buy.get("numberAnno"),
        "end_date": buy["endDate"],
        "hours_left": round(window.hours_left(end, moment), 1),
        "method": (buy.get("RefTradeMethods") or {}).get("nameRu"),
        "url": portal.lot_link(int(buy["id"])),
        "point_list": lot.get("pointList") or [],
    }


def candidates(client: GoszakupClient, name: str, settings: Settings) -> list[dict]:
    """Кандидаты по названию позиции: всё, чему пора уведомлять.

    Название здесь — только способ найти кандидатов, а не критерий отбора: решает
    код ЕНСТРУ в run(). Иначе теряются лоты, названные на портале иначе, чем
    позиция в таблице ("Комплекс оборудования" вместо "Комплекс оборудования
    видеонаблюдения") — замер показал 5 таких потерь на 256 лотов.

    Хватает одной страницы: лоты отдаются от новых к старым, а у всех активных
    объявлений id свежие. Замер: на второй странице будущих дедлайнов уже ноль.
    """
    lots = client.query(
        LOTS_QUERY, {"f": {"nameDescriptionRu": name}, "limit": PAGE_LIMIT}
    ).get("Lots") or []

    out = []
    for lot in lots:
        found = _in_window(lot, settings)
        if found:
            found["searched_name"] = name
            out.append(found)
    return out


def run(
    settings: Settings, codes: list[dict], send=None, persist: bool = True
) -> Result:
    """Один проход. send(lot) вызывается для каждого лота, который надо отправить."""
    result = Result()
    our_codes = {item["code"] for item in codes}
    seen: set[int] = set()

    with GoszakupClient(settings.goszakup_token) as client:
        names, result.name_fixes = official_names(client, codes)
        our_names = {name.lower() for name in names}

        # Предохранитель. Клиент ретраит каждый запрос (4 попытки с backoff), и при
        # массовом HTTP 500 у площадки один проход по 89 позициям превращается в
        # ~45 минут долбёжки. Если сбои идут подряд и НИ ОДНА позиция ещё не прошла —
        # площадка легла, продолжать бессмысленно: прерываемся, повторим на след.
        # тике. Единичный сбой (площадка моргнула на одной позиции) не в счёт —
        # счётчик сбрасывается первой же успешной позицией.
        consecutive_failures = 0
        any_success = False

        for name in sorted(names):
            try:
                found = candidates(client, name, settings)
                consecutive_failures = 0
                any_success = True
            except GoszakupError:
                consecutive_failures += 1
                if not any_success and consecutive_failures >= PLATFORM_DOWN_STREAK:
                    raise GoszakupError(
                        f"Площадка недоступна: {consecutive_failures} запросов подряд "
                        "с ошибкой, ни одного успешного. Проход прерван, повтор позже."
                    )
                continue  # площадка моргнула на одной позиции — остальные не роняем

            for lot in found:
                if lot["lot_id"] in seen:
                    continue
                seen.add(lot["lot_id"])
                result.matched += 1

                if persist and store.already_notified(lot["lot_id"]):
                    continue

                # Решает код ЕНСТРУ, а не название: на портале лот может называться
                # иначе, чем позиция в таблице, и по имени мы бы его потеряли.
                code = _enstru_code_of(client, lot)
                if code is not None:
                    if code not in our_codes:
                        result.wrong_code += 1
                        continue
                else:
                    # Код не сверился (пункт плана ещё не в индексе Plans) — падаем
                    # на точное совпадение названия: поиск идёт и по описанию, так
                    # что в выдачу попадают лоты чужих позиций, просто упомянувшие
                    # наше слово в характеристике.
                    if (lot.get("name") or "").strip().lower() not in our_names:
                        result.name_mismatch += 1
                        continue
                    result.unverified += 1
                lot["enstru_code"] = code

                count = portal.application_count(lot["trd_buy_id"])
                if count is None:
                    # Счётчик не прочитался. Молчим: "не знаем" — это не "ноль".
                    result.unreadable.append(lot["number_anno"])
                    continue
                if not settings.applications_ok(count):
                    continue

                lot["applications"] = count
                result.passed += 1
                result.lots.append(lot)

    # Отправляем одной пачкой в конце: Telegram режет на ~20 сообщениях в минуту,
    # а первый проход находит сразу сотню лотов.
    if send and result.lots:
        send(result.lots)
        result.notified = len(result.lots)

    # Отметку в базе ставим только после успешной отправки: если Telegram упал,
    # лот должен попасть в следующий проход, а не потеряться навсегда.
    if persist:
        for lot in result.lots:
            store.mark_notified(lot)

    return result


def dry_run(settings: Settings, codes: list[dict]) -> Result:
    """Сухой прогон: ищет и показывает, но ничего не шлёт и не отмечает в базе."""
    return run(settings, codes, send=None, persist=False)
