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

import httpx


# ------------------------------ чистые функции ---------------------------------

def mention(manager: dict) -> str:
    """Как упоминать менеджера в тексте: @username, иначе имя."""
    u = (manager or {}).get("username")
    return f"@{u}" if u else (manager or {}).get("name", "")


def main_keyboard(lot_id) -> dict:
    """Основная клавиатура под карточкой."""
    return {
        "inline_keyboard": [
            [{"text": "✅ Взять в работу", "callback_data": f"t:{lot_id}"}],
            [
                {"text": "👥 Назначить ▾", "callback_data": f"a:{lot_id}"},
                {"text": "🚫 Не наш / Скип", "callback_data": f"s:{lot_id}"},
            ],
        ]
    }


def assign_keyboard(lot_id, managers: list[dict]) -> dict:
    """Клавиатура со списком менеджеров + «Назад»."""
    rows = [
        [{"text": m.get("name", f"#{i}"), "callback_data": f"p:{lot_id}:{i}"}]
        for i, m in enumerate(managers)
    ]
    rows.append([{"text": "◀ Назад", "callback_data": f"b:{lot_id}"}])
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
        r = self._client.post(f"{self._base}/{method}", json=params)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
        return payload.get("result")

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

    def get_updates(self, offset=None, timeout=25, allowed_updates=None) -> list:
        return self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=allowed_updates or ["callback_query", "message"],
        ) or []
