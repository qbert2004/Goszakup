"""Отправка уведомлений в Telegram.

Bot API: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import html
from datetime import datetime
import time

import httpx

API = "https://api.telegram.org/bot{token}/{method}"

# Telegram режет примерно на 20 сообщениях в минуту в одну группу, дальше 429.
# Первый проход находит сразу сотню лотов, поэтому по одному их слать нельзя:
# половина не дойдёт. Много лотов -> дайджест пачками.
SEND_PAUSE = 3.5
DIGEST_THRESHOLD = 5
DIGEST_CHUNK = 10


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, payload: dict | None = None) -> dict:
    if not token:
        raise TelegramError("Токен бота не задан.")
    try:
        response = httpx.post(
            API.format(token=token, method=method), json=payload or {}, timeout=30.0
        )
    except httpx.HTTPError as exc:
        raise TelegramError(f"Не удалось связаться с Telegram: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"Telegram ответил не-JSON (HTTP {response.status_code}).")

    if not data.get("ok"):
        description = data.get("description", "без объяснения")
        if response.status_code == 401:
            raise TelegramError(
                "Telegram не принял токен бота. Проверьте, что скопировали его "
                "целиком из @BotFather (вид: 123456789:AA...)."
            )
        if response.status_code == 400 and "chat not found" in description.lower():
            raise TelegramError(
                "Чат не найден. Бот должен быть добавлен в группу, а chat_id — "
                "начинаться с минуса (например -1001234567890)."
            )
        if response.status_code == 403:
            raise TelegramError(
                f"Telegram запретил отправку: {description}. Обычно это значит, что "
                "бота выкинули из группы или он ещё не добавлен."
            )
        raise TelegramError(f"Telegram вернул ошибку: {description}")

    return data["result"]


def get_me(token: str) -> dict:
    """Проверяет токен бота и возвращает его имя."""
    return _call(token, "getMe")


def discover_chats(token: str) -> list[dict]:
    """Находит чаты, где бот уже побывал, — чтобы не искать chat_id вручную.

    Telegram отдаёт только свежие апдейты (примерно за сутки), и при включённом
    privacy mode бот видит лишь адресованные ему сообщения. Поэтому в группе нужно
    написать /start@имя_бота.
    """
    updates = _call(token, "getUpdates", {"limit": 100, "timeout": 0})

    chats: dict[int, dict] = {}
    for update in updates:
        message = (
            update.get("message")
            or update.get("channel_post")
            or update.get("my_chat_member")
            or {}
        )
        chat = message.get("chat")
        if not chat:
            continue
        chats[chat["id"]] = {
            "id": chat["id"],
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        }
    return list(chats.values())


def send(token: str, chat_id: str, text: str, disable_preview: bool = True) -> dict:
    return _call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        },
    )


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _deadline_text(lot: dict) -> str:
    """Когда истекает приём — днём недели и датой, а не только "через N часов".

    Одни часы вводят в заблуждение: "до конца 70 ч" выглядит как ошибка, пока не
    поймёшь, что дедлайн в понедельник, а сегодня пятница и это последний рабочий
    день, когда о лоте вообще можно предупредить.
    """
    end = lot.get("end_date")
    hours = lot.get("hours_left")
    left = f"осталось {hours:.0f} ч" if isinstance(hours, (int, float)) else ""

    if not end:
        return left or "срок неизвестен"
    try:
        moment = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return left or "срок неизвестен"

    when = f"{WEEKDAYS[moment.weekday()]} {moment:%d.%m} в {moment:%H:%M}"
    return f"до {when}" + (f" · {left}" if left else "")


def format_heartbeat(result, settings) -> str:
    """Сводка, когда лотов не нашлось.

    Не отчётность, а датчик жизни. При запасе 48ч пустая пятница — норма (окно
    упирается в выходные, а дедлайнов там не ставят), поэтому молчание сломанного
    сервера неотличимо от молчания спокойного дня. Пришла сводка — система жива.
    """
    checked = result.matched
    parts = [
        "🟢 <b>Проверка выполнена, подходящих лотов нет</b>",
        "",
        f"Просмотрено в окне: {checked}",
        f"Условия: до конца приёма ≤ {settings.window_hours_min} ч · "
        f"заявок ≤ {settings.max_applications} · "
        f"сумма от {settings.min_amount:,.0f} ₸".replace(",", " "),
    ]
    if result.unreadable:
        parts.append(
            f"⚠️ Счётчик заявок не прочитался у {len(result.unreadable)} — "
            "по ним промолчали, чтобы не выдать незнание за отсутствие конкурентов."
        )
    parts.append("")
    parts.append("<i>Это сообщение приходит каждый скан. Если оно пропало — "
                 "мониторинг не работает.</i>")
    return "\n".join(parts)


def format_digest(lots: list[dict]) -> str:
    """Компактная сводка: когда лотов много, сотня отдельных сообщений — это флуд."""
    esc = html.escape
    lines = [f"🎯 <b>Лотов без конкурентов: {len(lots)}</b>\n"]
    for lot in lots:
        amount = lot.get("amount")
        price = (
            f"{amount:,.0f} ₸".replace(",", " ")
            if isinstance(amount, (int, float))
            else "—"
        )
        apps = lot.get("applications")
        apps_note = "" if apps == 0 else f" · заявок: {apps}"
        lines.append(
            f'\n<a href="{esc(str(lot.get("url", "")))}">'
            f"{esc(str(lot.get('name', '')))}</a>\n"
            f"{price} · {_deadline_text(lot)}{apps_note}\n"
            f"<i>{esc(str(lot.get('customer', '—')))}</i>"
        )
    return "".join(lines)


def send_lots(token: str, chat_id: str, lots: list[dict]) -> int:
    """Отправляет найденные лоты, не нарываясь на лимит Telegram.

    Срочные идут первыми: обход перебирает позиции по алфавиту, и без сортировки
    лот с двумя часами до конца мог бы оказаться в хвосте последнего дайджеста.

    Возвращает число отправленных сообщений.
    """
    if not lots:
        return 0

    urgent_first = sorted(lots, key=lambda lot: lot.get("hours_left") or 0)

    if len(urgent_first) <= DIGEST_THRESHOLD:
        for index, lot in enumerate(urgent_first):
            if index:
                time.sleep(SEND_PAUSE)
            send(token, chat_id, format_lot(lot))
        return len(urgent_first)

    sent = 0
    for start in range(0, len(urgent_first), DIGEST_CHUNK):
        if sent:
            time.sleep(SEND_PAUSE)
        send(token, chat_id, format_digest(urgent_first[start:start + DIGEST_CHUNK]))
        sent += 1
    return sent


def format_lot(lot: dict) -> str:
    """Сообщение об одном лоте. Ссылка на лот — обязательна по ТЗ."""
    esc = html.escape
    deadline = _deadline_text(lot)
    amount = lot.get("amount")
    price = f"{amount:,.0f} ₸".replace(",", " ") if isinstance(amount, (int, float)) else "—"

    apps = lot.get("applications")
    if apps == 0:
        head, apps_line = "🎯", "<b>0 заявок</b> — конкурентов пока нет"
    else:
        head, apps_line = "📌", f"заявок: {apps}"

    code = lot.get("enstru_code")
    return (
        f"{head} <b>{esc(str(lot.get('name', '')))}</b>\n"
        f"{apps_line} · приём {deadline}\n\n"
        f"Заказчик: {esc(str(lot.get('customer', '—')))}\n"
        f"Сумма: {price}\n"
        f"Лот: {esc(str(lot.get('lot_number', '—')))} · "
        f"объявление {esc(str(lot.get('number_anno', '—')))}\n"
        + (f"ЕНСТРУ: {esc(str(code))}\n" if code else "")
        + f'\n<a href="{esc(str(lot.get("url", "")))}">Открыть лот на портале</a>'
    )
