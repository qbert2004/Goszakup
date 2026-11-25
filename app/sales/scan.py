"""Оркестратор поиска (Модули 1/2/3/5), без Google/Telegram — чистая логика.

Поиск идёт по двум наборам терминов (условие «ИЛИ» из ТЗ): ключевые слова и
официальные названия позиций по целевым кодам ЕНСТРУ (через них ловим совпадение
по коду, т.к. LotsFiltersInput ищет по названию/описанию, а не по refEnstruCode).
Каждый кандидат классифицируется (new/failed/repeat), пропускается через фильтр и
дедуплицируется по ID лота. Дедуп против уже отправленных — через `seen`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from . import queries
from .queries import announce_link, category, is_approved_plan, _has_parent

PAGE = 200
CAP = 10  # предохранитель: максимум страниц на один термин

# Код позиции плана по его id (для ЕНСТРУ-кода лота через пункт плана).
POINT_QUERY = "query ($f: PlansFiltersInput) { Plans(filter: $f, limit: 1) { refEnstruCode } }"


def fmt_deadline(s) -> str:
    """Дата площадки «ГГГГ-ММ-ДД ЧЧ:ММ:СС» -> «ДД.ММ.ГГГГ ЧЧ:ММ»."""
    if not s:
        return ""
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(s)


def default_code_resolver(client):
    """Резолвер кодов ЕНСТРУ лота: enstruList (ненулевые) + пункт плана."""

    def resolve(lot: dict) -> set[str]:
        codes = {str(c) for c in (lot.get("enstruList") or []) if c and str(c) != "0"}
        points = lot.get("pointList") or []
        if points:
            try:
                rows = client.query(POINT_QUERY, {"f": {"id": points[0]}}).get("Plans") or []
            except Exception:  # noqa: BLE001 — сеть/площадка не должны ронять скан
                rows = []
            if rows and rows[0].get("refEnstruCode"):
                codes.add(str(rows[0]["refEnstruCode"]))
        return codes

    return resolve


def lot_card_data(lot: dict, cat: str, code: str | None = None) -> dict:
    """Данные лота для карточки/записи в «Воронка_Лотов»."""
    buy = lot.get("TrdBuy") or {}
    data = {
        "lot_id": lot.get("id"),
        "trd_buy_id": buy.get("id") or lot.get("trdBuyId"),
        "url": announce_link(buy.get("id") or lot.get("trdBuyId")),
        "anno": buy.get("numberAnno") or "",
        "lot_number": lot.get("lotNumber") or "",
        "customer": lot.get("customerNameRu") or "",
        "customer_bin": lot.get("customerBin") or "",
        "name": lot.get("nameRu") or "",
        "enstru": code or "",
        "amount": lot.get("amount"),
        "method": (buy.get("RefTradeMethods") or {}).get("nameRu") or "",
        "end_date": fmt_deadline(buy.get("endDate")),
        "end_date_raw": buy.get("endDate"),
    }
    if cat == "repeat" and _has_parent(buy):
        data["parent_url"] = announce_link(buy.get("parentId"))
    return data


def scan_lots(client, cfg, year, search_terms, seen=None, code_resolver=None) -> dict:
    """Собрать и классифицировать лоты. Возвращает {new|failed|repeat: [card_data]}."""
    seen = set(seen or [])
    resolve = code_resolver or default_code_resolver(client)
    results: dict[str, list] = {"new": [], "failed": [], "repeat": []}
    local_seen: set = set()

    for term in search_terms:
        after = None
        pages = 0
        while pages < CAP:
            variables = {"f": {"nameDescriptionRu": term}, "limit": PAGE}
            if after is not None:
                variables["after"] = after
            try:
                lots = client.query(queries.LOTS_QUERY, variables).get("Lots") or []
            except Exception:  # noqa: BLE001 — один термин не должен ронять весь скан
                break
            if not lots:
                break
            for lot in lots:
                lid = lot.get("id")
                if lid in local_seen or lid in seen:
                    continue
                local_seen.add(lid)
                cat = category(lot.get("TrdBuy") or {}, year)
                if cat not in results:
                    continue
                codes = resolve(lot)
                if not cfg.passes(lot.get("nameRu"), codes, lot.get("amount")):
                    continue
                code = next(iter(codes & cfg.enstru), None) or next(iter(codes), None)
                results[cat].append(lot_card_data(lot, cat, code))
            pages += 1
            if len(lots) < PAGE:
                break
            after = lots[-1].get("id")
    return results


def build_search_terms(client, cfg) -> list[str]:
    """Термины поиска: ключевые слова + официальные названия по кодам ЕНСТРУ."""
    terms = set(cfg.keywords)
    if cfg.enstru:
        try:
            from ..monitor import official_names  # ленивый импорт: тянет store/portal
            names, _ = official_names(client, [{"code": c} for c in cfg.enstru])
            terms |= {n.lower() for n in names}
        except Exception:  # noqa: BLE001
            pass
    return [t for t in terms if t]


def scan_plans(client, cfg, year, search_terms=None) -> list[dict]:
    """Утверждённые пункты планов по профилю (Модуль 5)."""
    terms = search_terms if search_terms is not None else build_search_terms(client, cfg)
    seen: set = set()
    out: list[dict] = []
    for term in terms:
        after = None
        pages = 0
        while pages < CAP:
            variables = {"f": {"nameDescriptionRu": term, "plnPointYear": year}, "limit": PAGE}
            if after is not None:
                variables["after"] = after
            try:
                rows = client.query(queries.PLANS_QUERY, variables).get("Plans") or []
            except Exception:  # noqa: BLE001
                break
            if not rows:
                break
            for p in rows:
                pid = p.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                if not is_approved_plan(p):
                    continue
                codes = {str(p["refEnstruCode"])} if p.get("refEnstruCode") else set()
                if not cfg.passes(p.get("nameRu"), codes, p.get("amount")):
                    continue
                out.append(p)
            pages += 1
            if len(rows) < PAGE:
                break
            after = rows[-1].get("id")
    return out


def fetch_contract(client, trd_buy_id) -> dict | None:
    """Договор по закупке (победитель). Берём первый с поставщиком. None — нет договора."""
    try:
        rows = client.query(
            queries.CONTRACT_BY_BUY, {"f": {"trdBuyId": trd_buy_id}, "limit": 5}
        ).get("Contract") or []
    except Exception:  # noqa: BLE001
        return None
    for c in rows:
        if c.get("supplierBiin") or c.get("supplierFio") or (c.get("Supplier") or {}).get("nameRu"):
            return c
    return rows[0] if rows else None


def build_deal(lot: dict, contract: dict, code: str | None = None) -> dict:
    """Строка сделки: план/итог/снижение/победитель."""
    buy = lot.get("TrdBuy") or {}
    plan = float(lot.get("amount") or 0)
    csum = float(contract.get("contractSum") or 0)
    reduction = round((plan - csum) / plan * 100, 2) if (plan > 0 and csum > 0) else ""
    winner = (contract.get("Supplier") or {}).get("nameRu") or contract.get("supplierFio") or ""
    return {
        "lot_id": lot.get("id"),
        "itogi": buy.get("itogiDatePublic") or contract.get("signDate") or "",
        "anno": buy.get("numberAnno") or "",
        "lot_number": lot.get("lotNumber") or "",
        "url": announce_link(buy.get("id") or lot.get("trdBuyId")),
        "customer": lot.get("customerNameRu") or "",
        "customer_bin": lot.get("customerBin") or "",
        "name": lot.get("nameRu") or "",
        "enstru": code or "",
        "plan_sum": plan,
        "contract_sum": csum,
        "reduction": reduction,
        "winner": winner,
        "winner_bin": str(contract.get("supplierBiin") or ""),
        "second": "",  # TrdApp закрыт токену — 2-е место недоступно (как в ТЗ)
    }


def scan_deals(client, cfg, year, search_terms=None, code_resolver=None, seen_ids=None) -> list[dict]:
    """Состоявшиеся профильные закупки года с договором (Модуль 4). seen_ids — дедуп."""
    terms = search_terms if search_terms is not None else build_search_terms(client, cfg)
    resolve = code_resolver or default_code_resolver(client)
    seen_ids = set(seen_ids or [])
    local: set = set()
    deals: list[dict] = []
    for term in terms:
        after = None
        pages = 0
        while pages < CAP:
            variables = {"f": {"nameDescriptionRu": term}, "limit": PAGE}
            if after is not None:
                variables["after"] = after
            try:
                lots = client.query(queries.LOTS_QUERY, variables).get("Lots") or []
            except Exception:  # noqa: BLE001
                break
            if not lots:
                break
            for lot in lots:
                lid = lot.get("id")
                if lid in local or lid in seen_ids:
                    continue
                local.add(lid)
                buy = lot.get("TrdBuy") or {}
                if (buy.get("itogiDatePublic") or "")[:4] != str(year):
                    continue
                status = ((buy.get("RefBuyStatus") or {}).get("nameRu") or "").lower()
                if "не состоял" in status:
                    continue
                codes = resolve(lot)
                if not cfg.passes(lot.get("nameRu"), codes, lot.get("amount")):
                    continue
                contract = fetch_contract(client, buy.get("id"))
                if not contract:
                    continue  # нет договора — не считаем состоявшейся сделкой
                code = next(iter(codes & cfg.enstru), None) or next(iter(codes), None)
                deals.append(build_deal(lot, contract, code))
            pages += 1
            if len(lots) < PAGE:
                break
            after = lots[-1].get("id")
    return deals


def plans_by_month(points: list[dict]) -> dict:
    """Сводка планов по месяцам: {refMonthsId: {count, sum, name}}."""
    agg: dict = defaultdict(lambda: {"count": 0, "sum": 0.0, "name": ""})
    for p in points:
        m = p.get("refMonthsId")
        agg[m]["count"] += 1
        agg[m]["sum"] += float(p.get("amount") or 0)
        agg[m]["name"] = (p.get("RefMonths") or {}).get("nameRu") or agg[m]["name"]
    return dict(agg)
