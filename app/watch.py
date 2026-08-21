"""БИН-трек: мониторинг конкретных заказчиков по БИН.

Отдельный поток, независимый от ЕНСТРУ-скана: следим за списком заказчиков и при
появлении их тендера (сумма >= порога, активный статус) шлём в отдельный чат.
Раннее предупреждение — фильтров по числу заявок и окну дедлайна тут нет.

Изоляция: свой запрос, свой дедуп (store.notified_bin), собственный try/except в
планировщике. Падение этого трека не задевает основной скан.
"""

from __future__ import annotations

from datetime import datetime

from . import config, store
from .goszakup import GoszakupClient, lot_link
from .window import ASTANA

# Активные статусы объявления: 210 Опубликовано · 220 приём заявок ·
# 240 приём ценовых предложений. 350 (Завершено), 190 (черновик/на утверждении) и
# прочие — не «появившийся тендер», по ним не алертим.
ACTIVE_STATUSES = {210, 220, 240}

# Лотов на заказчика за запрос. Активные тендеры свежие (Lots идут по убыванию id),
# так что этого с запасом хватает поймать новые.
PER_BIN_LIMIT = 50

WATCH_QUERY = """
query ($f: LotsFiltersInput, $limit: Int) {
  Lots(filter: $f, limit: $limit) {
    id
    lotNumber
    nameRu
    amount
    customerNameRu
    trdBuyId
    TrdBuy {
      numberAnno
      endDate
      refBuyStatusId
    }
  }
}
"""


def _hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA)
    except (ValueError, TypeError):
        return None
    return (end - datetime.now(ASTANA)).total_seconds() / 3600


def _to_lot(row: dict) -> dict:
    buy = row.get("TrdBuy") or {}
    end_date = buy.get("endDate")
    return {
        "lot_id": row.get("id"),
        "trd_buy_id": row.get("trdBuyId"),
        "lot_number": row.get("lotNumber"),
        "name": row.get("nameRu"),
        "amount": row.get("amount"),
        "customer": row.get("customerNameRu"),
        "number_anno": buy.get("numberAnno"),
        "end_date": end_date,
        "hours_left": _hours_left(end_date),
        "url": lot_link(row.get("trdBuyId")),
    }


def find_new(client: GoszakupClient, settings: config.Settings) -> list[dict]:
    """Новые тендеры отслеживаемых заказчиков: активные, сумма >= порога, ещё не слали.

    По одному БИН на запрос (площадка ждёт строку, не массив). Дедуп — по
    notified_bin, отдельно от основного потока.
    """
    found: list[dict] = []
    seen: set[int] = set()
    for bin_code in settings.watch_bins:
        bin_code = str(bin_code).strip()
        if not bin_code:
            continue
        rows = client.query(
            WATCH_QUERY,
            {"f": {"customerBin": bin_code}, "limit": PER_BIN_LIMIT},
        ).get("Lots") or []
        for row in rows:
            buy = row.get("TrdBuy") or {}
            if buy.get("refBuyStatusId") not in ACTIVE_STATUSES:
                continue
            amount = row.get("amount")
            if not isinstance(amount, (int, float)) or amount < settings.bin_min_amount:
                continue
            lot_id = row.get("id")
            if lot_id is None or lot_id in seen:
                continue
            if store.already_notified_bin(lot_id):
                continue
            seen.add(lot_id)
            found.append(_to_lot(row))
    return found
