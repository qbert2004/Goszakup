"""Telegram-слой бота продаж: клиент Bot API (httpx), инлайн-клавиатуры, разбор callback.

Интерактив (кнопки «Взять/Назначить/Скип») требует, чтобы бот ПОЛУЧАЛ обновления —
один процесс long-polling getUpdates. Отправка/редактирование карточек и ответы на
нажатия идут через этот же клиент. Разбор callback_data и сборка клавиатур — чистые
функции, покрыты тестами; сетевые методы — тонкие обёртки.

callback_data у Telegram ограничен 64 байтами, поэтому действия кодируем коротко:
  t:<lot_id>          — Взять в работу (нажавший становится ответственным)
  a:<lot_id>          — развернуть меню «Назначить»
  p:<lot_id>:<idx>    — назначить менеджера idx из списка
  s:<lot_id>          — Не наш / Скип
  b:<lot_id>          — Назад (к основной клавиатуре)
"""

from __future__ import annotations

import time

import httpx

from .statuses import STATUSES


# ------------------------------ чистые функции ---------------------------------

def mention(manager: dict) -> str:
    """Как упоминать менеджера в тексте: @username, иначе имя."""
    u = (manager or {}).get("username")
    return f"@{u}" if u else (manager or {}).get("name", "")


def main_keyboard(lot_id, assigned: bool = False) -> dict:
    """Клавиатура под карточкой. assigned=True (есть ответственный) -> кнопка «Статус»."""
    if assigned:
        return {
            "inline_keyboard": [
                [{"text": "📊 Статус ▾", "callback_data": f"ss:{lot_id}"}],
                [
                    {"text": "👥 Переназначить ▾", "callback_data": f"a:{lot_id}"},
                    {"text": "🚫 Не наш / Скип", "callback_data": f"s:{lot_id}"},
                ],
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": "✅ Взять в работу", "callback_data": f"t:{lot_id}"}],
            [
                {"text": "👥 Назначить ▾", "callback_data": f"a:{lot_id}"},
                {"text": "🚫 Не наш / Скип", "callback_data": f"s:{lot_id}"},
            ],
        ]
    }


def menu_keyboard() -> dict:
    """Меню бота: мои лоты / все ответственные / без ответственного."""
    return {
        "inline_keyboard": [
            [{"text": "📋 Мои лоты", "callback_data": "mn:mine"}],
            [{"text": "👥 Все ответственные", "callback_data": "mn:all"}],
            [{"text": "🆓 Лоты без ответственного", "callback_data": "mn:free"}],
        ]
    }


def status_keyboard(lot_id) -> dict:
    """Меню статусов: кнопка на каждый статус (st:<lot>:<idx>) + «Назад»."""
    rows = [
        [{"text": s, "callback_data": f"st:{lot_id}:{i}"}]
        for i, s in enumerate(STATUSES)
    ]
    rows.append([{"text": "◀ Назад", "callback_data": f"b:{lot_id}"}])
    return {"inline_keyboard": rows}


def assign_keyboard(lot_id, managers: list[dict]) -> dict:
    """Клавиатура со списком менеджеров + «Назад»."""
    rows = [
        [{"text": m.get("name", f"#{i}"), "callback_data": f"p:{lot_id}:{i}"}]
        for i, m in enumerate(managers)
    ]
    rows.append([{"text": "◀ Назад", "callback_data": f"b:{lot_id}"}])
    return {"inline_keyboard": rows}


RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def month_menu_keyboard() -> dict:
    """Клавиатура из 12 месяцев (3 в ряд). callback: pm:<month 1..12>."""
    rows = []
    for r in range(4):
        rows.append([
            {"text": RU_MONTHS[r * 3 + i], "callback_data": f"pm:{r * 3 + i + 1}"}
            for i in range(3)
        ])
    return {"inline_keyboard": rows}


def parse_callback(data: str) -> tuple[str, str | None, str | None]:
    """callback_data -> (action, lot_id, extra)."""
    parts = (data or "").split(":")
    action = parts[0] if parts else ""
    lot_id = parts[1] if len(parts) > 1 else None
    extra = parts[2] if len(parts) > 2 else None
    return action, lot_id, extra


# ------------------------------ клиент Bot API ---------------------------------

class Telegram:
    """Тонкий клиент Telegram Bot API на httpx."""

    def __init__(self, token: str, timeout: float = 35.0):
        if not token:
            raise ValueError("Telegram token не задан")
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    def _call(self, method: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        # Telegram при превышении лимита отвечает 429 с parameters.retry_after —
        # ждём ровно столько и повторяем (до 5 раз), чтобы рассылка не падала.
        for _ in range(5):
            r = self._client.post(f"{self._base}/{method}", json=params)
            if r.status_code == 429:
                retry_after = (r.json().get("parameters") or {}).get("retry_after", 1)
                time.sleep(retry_after + 0.5)
                continue
            r.raise_for_status()
            payload = r.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
            return payload.get("result")
        raise RuntimeError(f"Telegram {method}: лимит 429 не ушёл за 5 попыток")

    def send_message(self, chat_id, text, thread_id=None, reply_markup=None, reply_to=None) -> dict:
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to,
        )

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> dict:
        return self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    def answer_callback_query(self, callback_query_id, text=None) -> dict:
        return self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def set_my_commands(self, commands: list[dict]) -> dict:
        return self._call("setMyCommands", commands=commands)

    def get_updates(self, offset=None, timeout=25, allowed_updates=None) -> list:
        return self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=allowed_updates or ["callback_query", "message"],
        ) or []
