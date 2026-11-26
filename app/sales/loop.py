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
from datetime import datetime, timedelta

from .. import config
from ..goszakup import GoszakupClient
from ..window import ASTANA
from . import cards, runner, scan, sheets, store
from .sheets import Workbook
from .tg import Telegram, month_menu_keyboard

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
    chg = runner.notify_changes(results, tg)  # reply об изменении сроков по старым карточкам
    sent = runner.dispatch(results, tg, wb, s.sales_chat_id, _topics(s), send_delay=SEND_DELAY)
    rem = runner.send_reminders(tg)
    log.info(
        "скан: new=%s failed=%s repeat=%s | отправлено %s, изменений %s, напоминаний %s",
        len(results["new"]), len(results["failed"]), len(results["repeat"]), sent, chg, rem,
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


def daily_once() -> tuple[int, int]:
    """Суточный сбор (Модуль 4 + 5): сделки в «База_Сделок», синхронизация планов."""
    s = config.load()
    wb = Workbook(s.sheets_key_path, s.sheets_id)
    cfg = wb.read_filters()
    year = datetime.now().year
    all_points = []
    with GoszakupClient(s.goszakup_token) as client:
        terms = scan.build_search_terms(client, cfg)
        deals = scan.scan_deals(client, cfg, year, terms, seen_ids=store.deal_seen_ids())
        # Планы — текущий И следующий год (по ТЗ): к концу года актуальны планы 2027.
        for yr in (year, year + 1):
            pts = scan.scan_plans(client, cfg, yr, terms)
            store.save_plan_months(yr, scan.plans_by_month(pts))  # свод по месяцам за год
            all_points += pts
    if deals:
        wb.append_deals([sheets.deal_row(d) for d in deals])
        for d in deals:
            store.mark_deal(d["lot_id"])
    wb.sync_plans([sheets.plan_row(p) for p in all_points])
    store.set_meta("plans_url", wb.tab_url(sheets.PLANS_TAB))
    log.info("суточный сбор: сделок +%s, планов %s (годы %s, %s)",
             len(deals), len(all_points), year, year + 1)
    return len(deals), len(all_points)


def _seconds_until(hour: int, minute: int) -> float:
    now = datetime.now(ASTANA)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _next_scan_delay(s) -> float:
    """Пауза до следующего скана. Интервал >= суток -> раз в день в sales_scan_at."""
    if s.sales_scan_interval_minutes >= 1440 and s.sales_scan_at:
        try:
            hour, minute = (int(x) for x in s.sales_scan_at.split(":"))
            return _seconds_until(hour, minute)
        except (ValueError, TypeError):
            pass
    return max(s.sales_scan_interval_minutes, 5) * 60


def _daily_loop() -> None:
    try:
        daily_once()  # прогон на старте: планы/сделки появляются сразу, не ждут ночи
    except Exception as exc:  # noqa: BLE001
        log.warning("суточный сбор (старт) упал: %s", exc)
    while True:
        time.sleep(_seconds_until(2, 0))  # 02:00 по Астане
        try:
            daily_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("суточный сбор упал: %s", exc)


def _ensure_plans_menu(tg, s) -> None:
    """Опубликовать меню месяцев в ветке «Планы» один раз (id запоминаем)."""
    if not s.sales_topic_plans or store.get_meta("plans_menu_msg"):
        return
    try:
        msg = tg.send_message(
            s.sales_chat_id, cards.plans_menu_text(),
            thread_id=s.sales_topic_plans, reply_markup=month_menu_keyboard(),
        )
        store.set_meta("plans_menu_msg", str(msg.get("message_id")))
        log.info("меню планов опубликовано (msg %s)", msg.get("message_id"))
    except Exception as exc:  # noqa: BLE001
        log.warning("не опубликовал меню планов: %s", exc)


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
        first = True
        while True:
            s0 = config.load()
            # В суточном режиме НЕ сканируем на старте — ждём ближайших sales_scan_at,
            # чтобы карточки шли строго раз в день в заданное время (а не при рестарте).
            if first and s0.sales_scan_interval_minutes >= 1440:
                time.sleep(_next_scan_delay(s0))
            first = False
            try:
                managers_box["m"] = scan_once(config.load(), tg, wb)
            except Exception as exc:  # noqa: BLE001 — сбой прохода не роняет петлю
                log.warning("скан упал: %s", exc)
            time.sleep(_next_scan_delay(config.load()))

    try:
        tg.set_my_commands([
            {"command": "my", "description": "Мои закреплённые лоты"},
            {"command": "list", "description": "Все закреплённые лоты"},
        ])
    except Exception as exc:  # noqa: BLE001
        log.warning("не задал команды бота: %s", exc)

    threading.Thread(target=scan_loop, name="sales-scan", daemon=True).start()
    threading.Thread(target=_daily_loop, name="sales-daily", daemon=True).start()
    _ensure_plans_menu(tg, s)
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
            try:
                if u.get("callback_query"):
                    runner.handle_callback(u["callback_query"], tg, wb, managers_box["m"])
                elif u.get("message"):
                    runner.handle_message(u["message"], tg)
            except Exception as exc:  # noqa: BLE001 — одно событие не роняет петлю
                log.warning("обработка обновления упала: %s", exc)
