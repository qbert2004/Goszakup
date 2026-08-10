"""Настройки приложения.

Секреты (токен площадки, токен Telegram-бота) хранятся в config/settings.local.json,
который добавлен в .gitignore и никогда не попадает в репозиторий.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.local.json"

DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1_JNniQTZ_iY-DChZJFrkmIsk3XmB3ahXXbA5PZWSUe0/edit?usp=sharing"
)


@dataclass
class Settings:
    # Токен API goszakup — вставляется через веб-форму, в коде его нет.
    goszakup_token: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Источник списка ЕНСТРУ / ключевых слов
    sheet_url: str = DEFAULT_SHEET_URL

    # Минимальный запас до дедлайна, часы. Позже этого срока молчать нельзя —
    # уведомление обязано уйти. ТЗ: "остался ровно 1 день" = 24 часа.
    window_hours_min: int = 24

    # Максимум заявок, при котором лот ещё интересен. 0 = только нулевая
    # конкуренция, как в ТЗ. Больше нуля удобно для проверки системы на живых
    # данных и для режима "низкая конкуренция".
    max_applications: int = 0

    # Минимальная плановая сумма лота, тенге. Мелочь на 18-60 тысяч менеджеру
    # неинтересна и только зашумляет чат.
    min_amount: float = 2_000_000

    # Считать запас по рабочим дням. Дедлайны на выходные площадка почти не ставит
    # (сб 18, вс 7 из 3272), поэтому наивные 24 часа для понедельничного лота дают
    # уведомление в воскресенье — менеджер увидит его, когда останется пара часов.
    # С этой поправкой такой лот показывается с утра пятницы. См. app/window.py.
    respect_working_hours: bool = True

    # Как часто опрашивать площадку, минут.
    # Не путать с памятью об отправленном: редкий скан НЕ защищает от повторов
    # (для этого есть store.already_notified), зато рушит обещание "за сутки".
    # Худший запас у менеджера = window_hours_min минус этот интервал: при скане
    # раз в 24ч и запасе 24ч лот может прийти за час до конца приёма.
    poll_interval_minutes: int = 1440

    # Время ежедневного скана по Астане, "ЧЧ:ММ". Работает, когда интервал >= суток.
    # Без него скан привязан к моменту запуска: сервер перезагрузился ночью после
    # сбоя питания — и все сканы навсегда переехали на ночь.
    daily_scan_at: str = "09:00"

    # Слать сводку даже когда ничего не найдено.
    # Это не отчётность, а датчик жизни: при запасе 48ч пустая пятница — норма,
    # поэтому молчание сломанного сервера неотличимо от молчания спокойного дня.
    # Пропала утренняя сводка — значит мониторинг умер, а не лотов нет.
    daily_report: bool = True

    def masked(self) -> dict:
        """Версия для отдачи в браузер: секреты не уходят на клиент в открытом виде."""
        data = asdict(self)
        for key in ("goszakup_token", "telegram_bot_token"):
            data[key] = _mask(data[key])
        return data


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * 12}{secret[-4:]}"


def load() -> Settings:
    if not SETTINGS_PATH.exists():
        return Settings()
    raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    known = {f.name for f in Settings.__dataclass_fields__.values()}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # На Windows chmod почти ничего не значит, но на POSIX закрываем файл от чужих.
    if os.name != "nt":
        SETTINGS_PATH.chmod(0o600)


def update(**changes) -> Settings:
    """Обновляет только переданные поля. Пустая строка для секрета = 'не менять'."""
    settings = load()
    for key, value in changes.items():
        if value is None:
            continue
        if key in ("goszakup_token", "telegram_bot_token") and value.strip() == "":
            continue
        setattr(settings, key, value)
    save(settings)
    return settings


def load_or_die() -> Settings:
    """Настройки с проверкой, что монитору есть с чем работать."""
    settings = load()
    missing = [
        label
        for label, value in (
            ("токен площадки", settings.goszakup_token),
            ("токен Telegram-бота", settings.telegram_bot_token),
            ("Chat ID", settings.telegram_chat_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Не заполнено: " + ", ".join(missing) + ". Откройте http://127.0.0.1:8765"
        )
    return settings
