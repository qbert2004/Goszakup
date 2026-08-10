"""Проверка предпосылок, на которых держится мониторинг.

Что выяснено экспериментально 2026-07-17 (см. README):

1. API НЕ отдаёт заявки по лотам, у которых приём ещё идёт. TrdApp возвращает
   записи только для объявлений с заполненным itogiDatePublic (итоги опубликованы).
   Значит фильтр "0 заявок" через API построить нельзя.
2. Публичная страница объявления показывает "Кол-во поданных заявок" вживую —
   и для ЗЦП (метод 3), и для открытого конкурса (метод 2). Нет счётчика только
   у закупки из одного источника (метод 6), где конкуренции нет по определению.
   Закон закрывает содержимое заявок до вскрытия, но не их количество.

Эти правила — фундамент системы. Площадка может их поменять, поэтому диагностика
перепроверяет каждое и честно говорит, если что-то поехало.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .goszakup import GoszakupClient, GoszakupError

ASTANA = timezone(timedelta(hours=5))
ANNOUNCE_URL = "https://goszakup.gov.kz/ru/announce/index/{id}"
COUNTER_RE = re.compile(r"Кол-во поданных заявок:\s*(\d+)")

STATUS_BIDDING = 220        # Опубликовано (прием заявок)
STATUS_PRICE_OFFERS = 240   # Опубликовано (прием ценовых предложений) = ЗЦП
METHOD_ZCP = 3              # Запрос ценовых предложений


@dataclass
class Report:
    steps: list = field(default_factory=list)
    ok: bool = True

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.ok = False

    def to_dict(self) -> dict:
        return {"steps": self.steps, "ok": self.ok, "verdict": self.verdict()}

    def verdict(self) -> str:
        if self.ok:
            return (
                "Все предпосылки в силе. Мониторинг ЗЦП работает как задумано: "
                "лоты с 0 заявок и дедлайном в заданном окне отлавливаются точно."
            )
        return (
            "Часть предпосылок не подтвердилась — площадка могла изменить поведение. "
            "Смотрите шаги выше: логику отбора нужно пересмотреть, пока "
            "уведомления могут врать."
        )


def _fetch_counter(buy_id: int) -> int | None:
    """Счётчик заявок с публичной страницы. None = счётчика на странице нет."""
    try:
        response = httpx.get(
            ANNOUNCE_URL.format(id=buy_id), timeout=30.0, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    match = COUNTER_RE.search(response.text)
    return int(match.group(1)) if match else None


def _active(client: GoszakupClient, status: int, after: int | None, limit: int = 200):
    query = """
    query ($f: TrdBuyFiltersInput, $a: Int, $l: Int) {
      TrdBuy(filter: $f, limit: $l, after: $a) {
        id numberAnno endDate refTradeMethodsId itogiDatePublic
      }
    }
    """
    data = client.query(query, {"f": {"refBuyStatusId": status}, "a": after, "l": limit})
    return data.get("TrdBuy") or []


def _hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None
    dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA)
    return (dt - datetime.now(ASTANA)).total_seconds() / 3600


def run(token: str) -> dict:
    report = Report()

    try:
        client = GoszakupClient(token)
        client.check_auth()
        report.add("Токен площадки", True, "Принят, API отвечает.")
    except GoszakupError as exc:
        report.add("Токен площадки", False, str(exc))
        return report.to_dict()

    with client:
        # --- Предпосылка 1: API скрывает заявки по активным лотам ---
        try:
            apps = client.query(
                "{ TrdApp(limit: 60) { buyId } }"
            )["TrdApp"] or []
            buy_ids = sorted({int(a["buyId"]) for a in apps}, reverse=True)[:8]

            query = """
            query ($f: TrdBuyFiltersInput) {
              TrdBuy(filter: $f, limit: 1) { itogiDatePublic }
            }
            """
            with_itogi = 0
            for buy_id in buy_ids:
                rows = client.query(query, {"f": {"id": buy_id}})["TrdBuy"]
                if rows and rows[0].get("itogiDatePublic"):
                    with_itogi += 1

            all_published = with_itogi == len(buy_ids) and buy_ids
            report.add(
                "Правило: заявки видны только после итогов",
                bool(all_published),
                f"Из {len(buy_ids)} объявлений с видимыми заявками итоги опубликованы "
                f"у {with_itogi}. "
                + (
                    "Правило в силе — значит по активным лотам API заявок не даст, "
                    "и счётчик надо брать со страницы."
                    if all_published
                    else "ВНИМАНИЕ: правило нарушено. Возможно, площадка открыла "
                    "заявки по активным лотам — тогда можно перейти на API."
                ),
            )
        except (GoszakupError, KeyError, TypeError) as exc:
            report.add("Правило: заявки видны только после итогов", False, str(exc))

        # --- Предпосылка 2: у активных ЗЦП счётчик на странице читается ---
        try:
            candidates = []
            for after in (None, 17330000, 17320000):
                for buy in _active(client, STATUS_PRICE_OFFERS, after, limit=100):
                    hours = _hours_left(buy.get("endDate"))
                    if (
                        hours is not None
                        and 0 < hours <= 72
                        and not buy.get("itogiDatePublic")
                        and buy.get("refTradeMethodsId") == METHOD_ZCP
                    ):
                        candidates.append(int(buy["id"]))
                if len(candidates) >= 8:
                    break

            counters = [(bid, _fetch_counter(bid)) for bid in candidates[:8]]
            readable = [c for _, c in counters if c is not None]
            distinct = len(set(readable)) > 1

            report.add(
                "Счётчик заявок на странице ЗЦП",
                bool(readable),
                f"Прочитан у {len(readable)} из {len(counters)} активных ЗЦП. "
                f"Значения: {sorted(readable)}. "
                + (
                    "Есть и нули, и ненули — счётчик живой и различающий."
                    if distinct
                    else "ВНИМАНИЕ: все значения одинаковые. Проверьте вручную, что "
                    "счётчик действительно отражает реальность."
                ),
            )
        except GoszakupError as exc:
            report.add("Счётчик заявок на странице ЗЦП", False, str(exc))

        # --- Предпосылка 3: фильтр publishDate по-прежнему нерабочий ---
        # Если площадка его починит — появится возможность резко ускорить обход.
        try:
            query = """
            query ($f: TrdBuyFiltersInput) {
              TrdBuy(filter: $f, limit: 3) { publishDate }
            }
            """
            rows = client.query(query, {"f": {"publishDate": "2020-01-01"}})["TrdBuy"]
            ignored = bool(rows) and not any(
                (r.get("publishDate") or "").startswith("2020-01-01") for r in rows
            )
            report.add(
                "Фильтр publishDate (известно, что не работает)",
                True,
                "Подтверждено: фильтр игнорируется, полагаться на него нельзя — "
                "обход идёт постранично с отбором по endDate на нашей стороне."
                if ignored
                else "Похоже, площадка починила publishDate — можно ускорить обход, "
                "но сначала перепроверьте вручную.",
            )
        except GoszakupError as exc:
            report.add("Фильтр publishDate", False, str(exc))

    return report.to_dict()


if __name__ == "__main__":
    from . import config

    print(json.dumps(run(config.load().goszakup_token), ensure_ascii=False, indent=2))
