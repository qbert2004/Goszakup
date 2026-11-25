"""Техническая память бота продаж (SQLite): отправленные карточки и их состояние.

Отдельная таблица в общей базе monitor.db. Нужна, чтобы:
  * не отправлять один лот дважды (дедуп при регулярном опросе);
  * знать message_id карточки — для редактирования при назначении менеджера и
    для reply-напоминания за 24 часа до дедлайна;
  * хранить назначенного ответственного/статус и отметку об уже посланном напоминании.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from ..store import DB_PATH  # та же база, что у легаси-мониторинга

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_cards (
    lot_id     INTEGER PRIMARY KEY,
    category   TEXT,
    chat_id    TEXT,
    thread_id  TEXT,
    message_id INTEGER,
    marker     TEXT,         -- «№ объявления · лот» для поиска строки в Воронке
    name       TEXT,
    end_date   TEXT,         -- дедлайн приёма (формат площадки) для напоминания
    manager    TEXT DEFAULT '',
    status     TEXT DEFAULT 'Не назначен',
    reminded   INTEGER DEFAULT 0,
    data_json  TEXT,          -- полные данные карточки для перерисовки
    sent_at    TEXT NOT NULL
);

-- Лоты, уже записанные в «База_Сделок» (Модуль 4) — чтобы ночной сбор не дублировал.
CREATE TABLE IF NOT EXISTS sales_deals (
    lot_id     INTEGER PRIMARY KEY,
    written_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def already_sent(lot_id) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM sales_cards WHERE lot_id=?", (lot_id,)).fetchone() is not None


def record_card(card: dict, category: str, chat_id, thread_id, message_id: int) -> None:
    """Запомнить отправленную карточку."""
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO sales_cards"
            " (lot_id, category, chat_id, thread_id, message_id, marker, name, end_date,"
            "  manager, status, reminded, data_json, sent_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card.get("lot_id"), category, str(chat_id), str(thread_id) if thread_id else "",
                message_id,
                f'{card.get("anno", "")} · лот {card.get("lot_number", "")}',
                card.get("name"), card.get("end_date_raw"),
                "", "Не назначен", 0, json.dumps(card, ensure_ascii=False), _now(),
            ),
        )


def get_card(lot_id) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM sales_cards WHERE lot_id=?", (lot_id,)).fetchone()
        return dict(row) if row else None


def set_status(lot_id, manager: str, status: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE sales_cards SET manager=?, status=? WHERE lot_id=?",
            (manager, status, lot_id),
        )


def due_for_reminder(now_str: str, plus24_str: str) -> list[dict]:
    """Карточки «В работе», у которых до дедлайна <= 24ч и напоминание ещё не слали."""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM sales_cards WHERE status='В работе' AND reminded=0"
            " AND end_date IS NOT NULL AND end_date > ? AND end_date <= ?",
            (now_str, plus24_str),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminded(lot_id) -> None:
    with connect() as c:
        c.execute("UPDATE sales_cards SET reminded=1 WHERE lot_id=?", (lot_id,))


def deal_written(lot_id) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM sales_deals WHERE lot_id=?", (lot_id,)).fetchone() is not None


def mark_deal(lot_id) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO sales_deals (lot_id, written_at) VALUES (?, ?)", (lot_id, _now()))


def deal_seen_ids() -> set:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT lot_id FROM sales_deals").fetchall()}
