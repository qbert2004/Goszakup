"""Петля бота продаж: периодический скан + непрерывный опрос кнопок.

Два занятия в одном процессе:
  * фоновый поток — раз в sales_scan_interval_minutes читает фильтры из Google Sheets,
    сканирует площадку, рассылает карточки в ветки, шлёт напоминания 24ч;
  * главный поток — long-polling getUpdates, обрабатывает нажатия кнопок.

Отдельный процесс/служба со СВОИМ bot-токеном: поллить токен может только один
процесс, поэтому легаси-бота это не задевает.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .. import config
from ..goszakup import GoszakupClient
from . import runner, scan
from .sheets import Workbook
from .tg import Telegram

log = logging.getLogger("sales")


def _topics(s) -> dict:
    return {
        "new": s.sales_topic_new or None,
        "failed": s.sales_topic_failed or None,
        "repeat": s.sales_topic_repeat or None,
    }


# Пауза между отправками карточек, сек — чтобы не упираться в лимиты Telegram.
SEND_DELAY = 2.0


def _scan(s, wb):
    cfg = wb.read_filters()
    with GoszakupClient(s.goszakup_token) as client:
        terms = scan.build_search_terms(client, cfg)
        results = scan.scan_lots(client, cfg, datetime.now().year, terms)
    return cfg, results


def scan_once(s, tg, wb) -> list[dict]:
    """Один проход: фильтры -> скан -> рассылка -> напоминания. Возвращает менеджеров."""
    cfg, results = _scan(s, wb)
    sent = runner.dispatch(results, tg, wb, s.sales_chat_id, _topics(s), send_delay=SEND_DELAY)
    rem = runner.send_reminders(tg)
    log.info(
        "скан: new=%s failed=%s repeat=%s | отправлено %s, напоминаний %s",
        len(results["new"]), len(results["failed"]), len(results["repeat"]), sent, rem,
    )
    return cfg.managers


def seed_once() -> int:
    """Посев: пометить текущие совпадения отправленными без рассылки (первый запуск)."""
    s = config.load()
    wb = Workbook(s.sheets_key_path, s.sheets_id)
    _cfg, results = _scan(s, wb)
    n = runner.seed(results, wb, s.sales_chat_id, _topics(s))
    log.info("посев: помечено отправленными %s лотов (в Telegram не слали)", n)
    return n


def run() -> None:
    s = config.load()
    missing = [n for n, v in (
        ("sales_bot_token", s.sales_bot_token),
        ("sales_chat_id", s.sales_chat_id),
        ("sheets_id", s.sheets_id),
        ("sheets_key_path", s.sheets_key_path),
        ("goszakup_token", s.goszakup_token),
    ) if not v]
    if missing:
        raise RuntimeError("Бот продаж не настроен, пусто: " + ", ".join(missing))

    wb = Workbook(s.sheets_key_path, s.sheets_id)
    tg = Telegram(s.sales_bot_token)
    managers_box = {"m": []}

    def scan_loop():
        while True:
            try:
                managers_box["m"] = scan_once(config.load(), tg, wb)
            except Exception as exc:  # noqa: BLE001 — сбой прохода не роняет петлю
                log.warning("скан упал: %s", exc)
            time.sleep(max(config.load().sales_scan_interval_minutes, 5) * 60)

    threading.Thread(target=scan_loop, name="sales-scan", daemon=True).start()
    log.info("бот продаж запущен, опрос кнопок начат")

    offset = None
    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=25)
        except Exception as exc:  # noqa: BLE001
            log.warning("getUpdates: %s", exc)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            cbq = u.get("callback_query")
            if not cbq:
                continue
            try:
                runner.handle_callback(cbq, tg, wb, managers_box["m"])
            except Exception as exc:  # noqa: BLE001 — одно нажатие не роняет петлю
                log.warning("обработка нажатия упала: %s", exc)
