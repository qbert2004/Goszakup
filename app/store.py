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

-- Отдельная память для БИН-трека (мониторинг конкретных заказчиков). Своя таблица,
-- чтобы отправка в БИН-чат не мешала дедупу основного ЕНСТРУ-потока и наоборот.
CREATE TABLE IF NOT EXISTS notified_bin (
    lot_id      INTEGER PRIMARY KEY,
    trd_buy_id  INTEGER,
    lot_number  TEXT,
    name        TEXT,
    amount      REAL,
    end_date    TEXT,
    notified_at TEXT NOT NULL
);

-- Лоты, отправленные как "0 заявок": ждём их дедлайна, чтобы перепроверить, не
-- подали ли заявки в последний момент. Живёт ПОСЛЕ дедлайна (в отличие от notified,
-- который purge_closed чистит), пока аудит их не проверит.
CREATE TABLE IF NOT EXISTS zero_pending (
    lot_id      INTEGER PRIMARY KEY,
    trd_buy_id  INTEGER,
    lot_number  TEXT,
    name        TEXT,
    customer    TEXT,
    amount      REAL,
    end_date    TEXT,
    url         TEXT,
    added_at    TEXT NOT NULL
);

-- Лоты, которые закрылись реально с 0 участников. Копятся здесь, раз в сутки
-- уходят дайджестом в отдельный чат и очищаются.
CREATE TABLE IF NOT EXISTS closed_zero (
    lot_id      INTEGER PRIMARY KEY,
    trd_buy_id  INTEGER,
    lot_number  TEXT,
    name        TEXT,
    customer    TEXT,
    amount      REAL,
    end_date    TEXT,
    url         TEXT,
    closed_at   TEXT NOT NULL
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
    отсекает закрытые), поэтому память о нём — мёртвый груз. Чистим обе памяти —
    и основную, и БИН-трека.

    Возвращает число забытых лотов.
    """
    now = datetime.now(ASTANA).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        # end_date хранится строкой площадки "ГГГГ-ММ-ДД ЧЧ:ММ:СС" по Астане —
        # такой формат сравнивается лексикографически.
        forgotten = conn.execute(
            "DELETE FROM notified WHERE end_date IS NOT NULL AND end_date <= ?",
            (now,),
        ).rowcount
        forgotten += conn.execute(
            "DELETE FROM notified_bin WHERE end_date IS NOT NULL AND end_date <= ?",
            (now,),
        ).rowcount
        return forgotten


def already_notified_bin(lot_id: int) -> bool:
    """Слали ли уже этот лот в БИН-трек. Отдельно от основного дедупа."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM notified_bin WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        return row is not None


def mark_notified_bin(lot: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notified_bin"
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


def add_zero_pending(lot: dict) -> None:
    """Запомнить отправленный «0 заявок» лот для перепроверки после дедлайна."""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO zero_pending"
            " (lot_id, trd_buy_id, lot_number, name, customer, amount, end_date, url, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lot["lot_id"], lot.get("trd_buy_id"), lot.get("lot_number"),
                lot.get("name"), lot.get("customer"), lot.get("amount"),
                lot.get("end_date"), lot.get("url"), _now(),
            ),
        )


def zero_pending_due(now: str) -> list[dict]:
    """0-лоты, у которых приём уже закрылся (пора перепроверить счётчик)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM zero_pending WHERE end_date IS NOT NULL AND end_date <= ?",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]


def remove_zero_pending(lot_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM zero_pending WHERE lot_id = ?", (lot_id,))


def purge_stale_zero_pending(cutoff: str) -> int:
    """Забыть лоты, чей дедлайн давно прошёл, а счётчик так и не прочитался —
    чтобы не перепроверять их вечно. Возвращает число забытых."""
    with connect() as conn:
        return conn.execute(
            "DELETE FROM zero_pending WHERE end_date IS NOT NULL AND end_date < ?",
            (cutoff,),
        ).rowcount


def add_closed_zero(lot: dict) -> None:
    """Лот закрылся реально с 0 участников — в копилку для суточного дайджеста."""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO closed_zero"
            " (lot_id, trd_buy_id, lot_number, name, customer, amount, end_date, url, closed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lot["lot_id"], lot.get("trd_buy_id"), lot.get("lot_number"),
                lot.get("name"), lot.get("customer"), lot.get("amount"),
                lot.get("end_date"), lot.get("url"), _now(),
            ),
        )


def list_closed_zero() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_zero ORDER BY end_date"
        ).fetchall()
        return [dict(row) for row in rows]


def clear_closed_zero() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM closed_zero")


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
