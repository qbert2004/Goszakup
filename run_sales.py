#!/usr/bin/env python3
"""Точка входа бота отдела продаж (отдельный процесс/служба со своим bot-токеном).

Запуск:  cd /opt/goszakup-monitor && .venv/bin/python run_sales.py
"""
import logging

from app.sales.loop import run

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("Бот отдела продаж запускается…")
    run()
