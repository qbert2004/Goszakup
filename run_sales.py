#!/usr/bin/env python3
"""Точка входа бота отдела продаж (отдельный процесс/служба со своим bot-токеном).

Запуск службы:  cd /opt/goszakup-monitor && .venv/bin/python run_sales.py
Разовый посев:  .venv/bin/python run_sales.py seed
  (пометить текущие совпадения отправленными без рассылки — чтобы первый запуск
   не завалил группу бэклогом; дальше бот шлёт только новые лоты)
"""
import logging
import sys

from app.sales.loop import daily_once, run, seed_once

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        n = seed_once()
        print(f"Посев завершён: {n} лотов помечено отправленными (в Telegram не слали).")
    elif cmd == "daily":
        deals, plans = daily_once()
        print(f"Суточный сбор: сделок +{deals}, планов синхронизировано {plans}.")
    elif cmd == "summary":
        from app import config
        from app.sales import runner
        from app.sales.tg import Telegram
        s = config.load()
        Telegram(s.sales_bot_token).send_message(
            s.sales_chat_id, runner.daily_summary_text(), thread_id=s.sales_topic_new or None)
        print("Итоги дня отправлены в ветку «Новые».")
    else:
        print("Бот отдела продаж запускается…")
        run()
