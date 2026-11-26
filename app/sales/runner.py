"""Рантайм бота продаж: отправка карточек, обработка кнопок, напоминания.

Связывает scan -> cards -> Telegram-ветки -> Воронка_Лотов и обрабатывает нажатия
(Взять/Назначить/Скип), редактируя и карточку, и строку в таблице. `tg` и `workbook`
инъектируются, поэтому логика тестируется на заглушках. Петля опроса и периодический
скан — в loop.py (тонкий слой поверх этого).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from . import cards, sheets, store
from .statuses import FIRST, SKIP, STATUSES
from .tg import (
    RU_MONTHS, assign_keyboard, main_keyboard, mention, month_menu_keyboard,
    parse_callback, status_keyboard,
)
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
    d["status"] = status_db
    return cards.build_card(d, category)


def _assigned(card: dict) -> bool:
    """Назначен ли ответственный (и это не отказ)."""
    return bool(card.get("manager")) and card.get("status") != SKIP


def dispatch(results: dict, tg, workbook, chat_id, topics: dict, send_delay: float = 0.0) -> int:
    """Разослать найденные лоты по веткам, записать в память и в «Воронка_Лотов».

    topics: {"new": thread_id, "failed": ..., "repeat": ...}. send_delay — пауза между
    отправками (сек), чтобы не упираться в лимиты Telegram. Возвращает число отправленных.
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
            if send_delay:
                time.sleep(send_delay)
        if rows_funnel and workbook is not None:
            try:
                workbook.append_funnel(rows_funnel)
            except Exception as exc:  # noqa: BLE001
                log.warning("не записал в Воронку (%s): %s", category, exc)
    return sent


def seed(results: dict, workbook, chat_id, topics: dict, to_funnel: bool = True) -> int:
    """«Посев»: пометить текущие совпадения отправленными БЕЗ рассылки в Telegram.

    Нужен для первого запуска, чтобы не завалить группу бэклогом: после посева в
    чат идут только НОВЫЕ лоты. Текущие при to_funnel=True кладём в «Воронка_Лотов»
    (без кнопок) — чтобы команда их всё же видела в таблице.
    """
    seeded = 0
    for category in CATEGORIES:
        thread = topics.get(category)
        if not thread:
            continue
        rows_funnel = []
        for data in results.get(category, []):
            lot_id = data.get("lot_id")
            if lot_id is None or store.already_sent(lot_id):
                continue
            store.record_card(data, category, chat_id, thread, 0)  # message_id=0: не постили
            rows_funnel.append(sheets.funnel_row(data, category, manager="", status="Не назначен"))
            seeded += 1
        if to_funnel and rows_funnel and workbook is not None:
            try:
                workbook.append_funnel(rows_funnel)
            except Exception as exc:  # noqa: BLE001
                log.warning("посев: не записал в Воронку (%s): %s", category, exc)
    return seeded


def _plans_menu_press(cbq: dict, month_s, tg) -> None:
    """Нажата кнопка месяца в меню планов: показать свод по месяцу + ссылку."""
    cb_id = cbq.get("id")
    msg = cbq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    month = int(month_s) if (month_s and month_s.isdigit()) else 0
    md = store.get_plan_month(month) or {}
    name = RU_MONTHS[month - 1] if 1 <= month <= 12 else ""
    url = store.get_meta("plans_url") or ""
    text = cards.plans_menu_text(name, md.get("cnt", 0), md.get("total", 0.0), url)
    try:
        tg.edit_message_text(chat_id, message_id, text, reply_markup=month_menu_keyboard())
    except Exception as exc:  # noqa: BLE001 — «message is not modified» и т.п. не роняют
        log.warning("меню планов: %s", exc)
    tg.answer_callback_query(cb_id)


def handle_callback(cbq: dict, tg, workbook, managers: list[dict]) -> None:
    """Обработать нажатие инлайн-кнопки."""
    cb_id = cbq.get("id")
    action, lot_s, extra = parse_callback(cbq.get("data", ""))

    if action == "pm":  # меню планов (Модуль 5), не карточка лота
        _plans_menu_press(cbq, lot_s, tg)
        return

    card = store.get_card(int(lot_s)) if (lot_s and lot_s.isdigit()) else None
    if not card:
        tg.answer_callback_query(cb_id, "Карточка устарела")
        return
    lot_id = card["lot_id"]
    category = card["category"]
    data = json.loads(card.get("data_json") or "{}")
    presser = _mention_user(cbq.get("from", {}))
    owns = (not card.get("manager")) or presser == card["manager"]

    # Навигация по клавиатурам (без изменения состояния).
    if action == "a":  # меню «Назначить/Переназначить»
        tg.edit_message_text(card["chat_id"], card["message_id"],
                             _render(data, category, card["manager"], card["status"]),
                             reply_markup=assign_keyboard(lot_id, managers))
        tg.answer_callback_query(cb_id)
        return
    if action == "ss":  # меню статусов — только ответственный
        if not owns:
            tg.answer_callback_query(cb_id, "Это не ваш лот — статус меняет ответственный")
            return
        tg.edit_message_text(card["chat_id"], card["message_id"],
                             _render(data, category, card["manager"], card["status"]),
                             reply_markup=status_keyboard(lot_id))
        tg.answer_callback_query(cb_id)
        return
    if action == "b":  # назад к основной клавиатуре
        tg.edit_message_text(card["chat_id"], card["message_id"],
                             _render(data, category, card["manager"], card["status"]),
                             reply_markup=main_keyboard(lot_id, assigned=_assigned(card)))
        tg.answer_callback_query(cb_id)
        return

    # Изменение состояния: take / pick / skip / смена статуса.
    if action == "t":
        manager, status_db, note = presser, FIRST, "Взято в работу"
    elif action == "p":
        idx = int(extra) if (extra and extra.isdigit()) else -1
        m = managers[idx] if 0 <= idx < len(managers) else None
        manager, status_db, note = (mention(m) if m else ""), FIRST, "Назначено"
    elif action == "s":
        manager, status_db, note = (card.get("manager") or presser), SKIP, "Не наш / Скип"
    elif action == "st":  # смена статуса — только ответственный
        if not owns:
            tg.answer_callback_query(cb_id, "Это не ваш лот — статус меняет ответственный")
            return
        idx = int(extra) if (extra and extra.isdigit()) else -1
        if not (0 <= idx < len(STATUSES)):
            tg.answer_callback_query(cb_id)
            return
        manager, status_db, note = (card.get("manager") or presser), STATUSES[idx], STATUSES[idx]
    else:
        tg.answer_callback_query(cb_id)
        return

    store.set_status(lot_id, manager, status_db)
    tg.edit_message_text(
        card["chat_id"], card["message_id"],
        _render(data, category, manager, status_db),
        reply_markup=main_keyboard(lot_id, assigned=(status_db != SKIP)),
    )
    if workbook is not None:
        try:
            workbook.set_funnel_responsible(card["marker"], manager, status_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("не обновил Воронку для %s: %s", lot_id, exc)
    tg.answer_callback_query(cb_id, note)


def notify_changes(results: dict, tg) -> int:
    """Reply об изменении сроков по уже отправленным карточкам (Модуль 1).

    Сравниваем новый дедлайн с сохранённым: сдвинулся — отвечаем на исходную карточку
    (с тегом ответственного, если назначен), новую карточку НЕ создаём.
    """
    changed = 0
    for category in CATEGORIES:
        for data in results.get(category, []):
            lot_id = data.get("lot_id")
            card = store.get_card(lot_id) if lot_id is not None else None
            if not card or not card.get("message_id"):  # не отправляли (или посев: msg_id=0)
                continue
            new_end = data.get("end_date_raw") or ""
            if new_end and new_end != (card.get("end_date") or ""):
                manager = card.get("manager") or "—"
                text = (
                    "🔄 <b>Изменились сроки приёма заявок</b>\n"
                    f"Новый дедлайн: {data.get('end_date')}\n"
                    f"Ответственный: {manager}"
                )
                try:
                    tg.send_message(
                        card["chat_id"], text,
                        thread_id=card["thread_id"] or None,
                        reply_to=card["message_id"],
                    )
                    store.update_deadline(lot_id, new_end)
                    changed += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("reply об изменении срока по %s не ушёл: %s", lot_id, exc)
    return changed


def _card_link(card: dict) -> str:
    try:
        data = json.loads(card.get("data_json") or "{}")
    except Exception:  # noqa: BLE001
        data = {}
    url = data.get("url") or ""
    label = data.get("lot_number") or card.get("marker") or str(card.get("lot_id"))
    return f'<a href="{url}">{label}</a>' if url else label


def assignments_text(only_manager: str | None = None) -> str:
    """Компактный список закреплённых лотов (номер-ссылка + статус), по менеджерам."""
    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for card in store.assigned_cards():
        mgr = card.get("manager") or "—"
        if only_manager and mgr != only_manager:
            continue
        groups.setdefault(mgr, []).append(card)
    if not groups:
        return ""
    lines = ["📋 <b>Закреплённые лоты</b>"]
    for mgr, items in groups.items():
        lines.append(f"\n<b>{mgr}</b> ({len(items)}):")
        for card in items:
            lines.append(f"• {_card_link(card)} — {card.get('status') or '—'}")
    text = "\n".join(lines)
    return text if len(text) <= 3900 else text[:3900] + "\n…(обрезано)"


def handle_message(msg: dict, tg) -> None:
    """Текстовые команды: /my — мои лоты, /list — все."""
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0].lower()
    chat_id = (msg.get("chat") or {}).get("id")
    thread = msg.get("message_thread_id")
    if cmd in ("/my", "/мои"):
        who = _mention_user(msg.get("from", {}))
        tg.send_message(chat_id, assignments_text(who) or "У вас нет закреплённых лотов.",
                        thread_id=thread)
    elif cmd in ("/list", "/lots", "/all", "/все"):
        tg.send_message(chat_id, assignments_text() or "Закреплённых лотов нет.", thread_id=thread)
    elif cmd in ("/help", "/start"):
        tg.send_message(chat_id,
                        "Команды:\n/my — мои закреплённые лоты\n/list — все закреплённые лоты",
                        thread_id=thread)


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
