"""Рантайм бота продаж: отправка карточек, обработка кнопок, напоминания.

Связывает scan -> cards -> Telegram-ветки -> Воронка_Лотов и обрабатывает нажатия
(Взять/Назначить/Скип), редактируя и карточку, и строку в таблице. `tg` и `workbook`
инъектируются, поэтому логика тестируется на заглушках. Петля опроса и периодический
скан — в loop.py (тонкий слой поверх этого).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import cards, sheets, store
from .tg import assign_keyboard, main_keyboard, mention, parse_callback
from ..window import ASTANA

log = logging.getLogger("sales")

CATEGORIES = ("new", "failed", "repeat")


def _mention_user(u: dict) -> str:
    """Как записать нажавшего кнопку: @username, иначе имя, иначе id."""
    un = (u or {}).get("username")
    return f"@{un}" if un else ((u or {}).get("first_name") or str((u or {}).get("id", "")))


def _render(data: dict, category: str, manager: str, status_db: str) -> str:
    """Перерисовать карточку с текущим ответственным/статусом."""
    d = dict(data)
    d["manager"] = manager
    d["status"] = "skip" if status_db == "Скип" else None
    return cards.build_card(d, category)


def dispatch(results: dict, tg, workbook, chat_id, topics: dict) -> int:
    """Разослать найденные лоты по веткам, записать в память и в «Воронка_Лотов».

    topics: {"new": thread_id, "failed": ..., "repeat": ...}. Возвращает число отправленных.
    """
    sent = 0
    for category in CATEGORIES:
        thread = topics.get(category)
        if not thread:
            continue
        rows_funnel = []
        for data in results.get(category, []):
            lot_id = data.get("lot_id")
            if lot_id is None or store.already_sent(lot_id):
                continue
            text = cards.build_card(data, category)
            try:
                msg = tg.send_message(chat_id, text, thread_id=thread, reply_markup=main_keyboard(lot_id))
            except Exception as exc:  # noqa: BLE001 — один лот не должен ронять рассылку
                log.warning("не отправил карточку %s: %s", lot_id, exc)
                continue
            store.record_card(data, category, chat_id, thread, msg.get("message_id"))
            rows_funnel.append(sheets.funnel_row(data, category, manager="", status="Не назначен"))
            sent += 1
        if rows_funnel and workbook is not None:
            try:
                workbook.append_funnel(rows_funnel)
            except Exception as exc:  # noqa: BLE001
                log.warning("не записал в Воронку (%s): %s", category, exc)
    return sent


def handle_callback(cbq: dict, tg, workbook, managers: list[dict]) -> None:
    """Обработать нажатие инлайн-кнопки."""
    cb_id = cbq.get("id")
    action, lot_s, extra = parse_callback(cbq.get("data", ""))
    card = store.get_card(int(lot_s)) if (lot_s and lot_s.isdigit()) else None
    if not card:
        tg.answer_callback_query(cb_id, "Карточка устарела")
        return
    lot_id = card["lot_id"]
    category = card["category"]
    data = json.loads(card.get("data_json") or "{}")

    # Меню «Назначить» / «Назад» — только меняем клавиатуру.
    if action == "a":
        tg.edit_message_text(
            card["chat_id"], card["message_id"],
            _render(data, category, card["manager"], card["status"]),
            reply_markup=assign_keyboard(lot_id, managers),
        )
        tg.answer_callback_query(cb_id)
        return
    if action == "b":
        tg.edit_message_text(
            card["chat_id"], card["message_id"],
            _render(data, category, card["manager"], card["status"]),
            reply_markup=main_keyboard(lot_id),
        )
        tg.answer_callback_query(cb_id)
        return

    # Смена ответственного: take / pick / skip.
    if action == "t":
        manager, status_db, note = _mention_user(cbq.get("from", {})), "В работе", "Взято в работу"
    elif action == "p":
        idx = int(extra) if (extra and extra.isdigit()) else -1
        m = managers[idx] if 0 <= idx < len(managers) else None
        manager, status_db, note = (mention(m) if m else ""), "В работе", "Назначено"
    elif action == "s":
        manager, status_db, note = _mention_user(cbq.get("from", {})), "Скип", "Пропущено"
    else:
        tg.answer_callback_query(cb_id)
        return

    store.set_status(lot_id, manager, status_db)
    tg.edit_message_text(
        card["chat_id"], card["message_id"],
        _render(data, category, manager, status_db),
        reply_markup=main_keyboard(lot_id),
    )
    if workbook is not None:
        try:
            workbook.set_funnel_responsible(card["marker"], manager, status_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("не обновил Воронку для %s: %s", lot_id, exc)
    tg.answer_callback_query(cb_id, note)


def send_reminders(tg) -> int:
    """Reply-напоминание за 24ч до дедлайна по лотам «В работе». Возвращает число."""
    now = datetime.now(ASTANA)
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    plus24 = (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    done = 0
    for card in store.due_for_reminder(now_s, plus24):
        text = (
            "⏰ До окончания приёма заявок осталось 24 часа. "
            f"Ответственный: {card.get('manager') or '—'}"
        )
        try:
            tg.send_message(
                card["chat_id"], text,
                thread_id=card["thread_id"] or None,
                reply_to=card["message_id"],
            )
            store.mark_reminded(card["lot_id"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("напоминание по %s не ушло: %s", card["lot_id"], exc)
    return done
