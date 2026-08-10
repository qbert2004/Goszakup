"""Локальная база: что уже отправляли и кэш соответствий ЕНСТРУ.

Дедупликация обязательна: обход идёт каждый час, и без неё менеджеры получали бы
один и тот же лот десятки раз, пока он висит в окне уведомления.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# Время площадки — Астана, UTC+5. end_date хранится в её формате.
ASTANA = timezone(timedelta(hours=5))
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notified (
    lot_id      INTEGER PRIMARY KEY,
    trd_buy_id  INTEGER,
    lot_number  TEXT,
    name        TEXT,
    amount      REAL,
    end_date    TEXT,
    notified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enstru_cache (
    code        TEXT PRIMARY KEY,
    enstru_id   INTEGER,
    name        TEXT,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    checked    INTEGER DEFAULT 0,
    matched    INTEGER DEFAULT 0,
    notified   INTEGER DEFAULT 0,
    error      TEXT
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


def already_notified(lot_id: int) -> bool:
    """Слали ли уже этот лот.

    Память живёт ровно столько, сколько живёт сам лот: пока приём открыт, лот
    попадает в каждый проход, и без памяти менеджеры получали бы его каждые два
    часа. Как приём закрылся — запись бесполезна и удаляется purge_closed().

    Привязка к дедлайну лота, а не к фиксированному сроку: TTL в 24 часа переслал
    бы ещё открытый лот повторно (лот с понедельничным дедлайном, показанный в
    пятницу, вернулся бы в субботу).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM notified WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        return row is not None


def purge_closed() -> int:
    """Забывает лоты, у которых приём заявок уже закончился.

    Такой лот всё равно никогда не будет отправлен снова (window.should_alert
    отсекает закрытые), поэтому память о нём — мёртвый груз.

    Возвращает число забытых лотов.
    """
    now = datetime.now(ASTANA).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        cursor = conn.execute(
            # end_date хранится строкой площадки "ГГГГ-ММ-ДД ЧЧ:ММ:СС" по Астане —
            # такой формат сравнивается лексикографически.
            "DELETE FROM notified WHERE end_date IS NOT NULL AND end_date <= ?",
            (now,),
        )
        return cursor.rowcount


def mark_notified(lot: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notified"
            " (lot_id, trd_buy_id, lot_number, name, amount, end_date, notified_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lot["lot_id"],
                lot.get("trd_buy_id"),
                lot.get("lot_number"),
                lot.get("name"),
                lot.get("amount"),
                lot.get("end_date"),
                _now(),
            ),
        )


def cached_enstru(code: str) -> dict | None:
    """Что мы знаем про код. None — не спрашивали ещё.

    enstru_id < 0 означает "спрашивали, в справочнике площадки нет".
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT enstru_id, name FROM enstru_cache WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def cache_enstru(code: str, enstru_id: int | None, name: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO enstru_cache (code, enstru_id, name, resolved_at)"
            " VALUES (?, ?, ?, ?)",
            (code, enstru_id if enstru_id is not None else -1, name, _now()),
        )


def start_run() -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (_now(),)
        )
        return int(cursor.lastrowid)


def finish_run(
    run_id: int, checked: int, matched: int, notified: int, error: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, checked = ?, matched = ?,"
            " notified = ?, error = ? WHERE id = ?",
            (_now(), checked, matched, notified, error, run_id),
        )


def last_runs(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def recent_notifications(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notified ORDER BY notified_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
