#!/usr/bin/env python3
"""Точка входа бота отдела продаж (отдельный процесс/служба со своим bot-токеном).

Запуск службы:  cd /opt/goszakup-monitor && .venv/bin/python run_sales.py
Разовый посев:  .venv/bin/python run_sales.py seed
  (пометить текущие совпадения отправленными без рассылки — чтобы первый запуск
   не завалил группу бэклогом; дальше бот шлёт только новые лоты)
"""
import logging
import sys

from app.sales.loop import run, seed_once

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        n = seed_once()
        print(f"Посев завершён: {n} лотов помечено отправленными (в Telegram не слали).")
    else:
        print("Бот отдела продаж запускается…")
        run()
