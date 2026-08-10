"""Когда уведомлять о лоте.

Требование: система НЕ должна молчать за день до окончания приёма. Уведомить обязана.

Наивное "за 24-48 часов до дедлайна" это требование нарушает. Замеры по площадке
(3272 будущих дедлайна): будни ~1000/день, суббота 18, воскресенье 7. Дедлайны на
выходные не ставят. Отсюда:

  - лот с дедлайном в понедельник 09:00 попадает в окно 24-48ч в субботу-воскресенье;
    уведомление уйдёт в выходной, менеджер увидит его утром понедельника,
    когда до конца осталось 0-2 часа;
  - запуск в пятницу вообще не находит ничего: окно = суббота + воскресенье.

А на понедельник и вторник приходится половина всех дедлайнов.

Поэтому момент уведомления считается так: берём "дедлайн минус запас"; если он попал
на рабочее время — это и есть момент. Если на нерабочее (выходные, ночь, вечер) —
отдаём НАЧАЛО последнего рабочего дня, с которого запас ещё соблюдается. Лот с
дедлайном в понедельник 09:00 при запасе 24ч показывается с утра пятницы: менеджер
получает его на работе и с реальным временем на подготовку.

Горизонт (сколько времени вперёд вообще смотреть) здесь не при чём — он ограничивает
только объём выборки. Гейт уведомления — момент ниже, и он же сам отсекает лоты,
до которых ещё далеко.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

ASTANA = timezone(timedelta(hours=5))

WORK_START = time(9, 0)
WORK_END = time(18, 0)
SATURDAY = 5
MAX_LOOKBACK_DAYS = 10


def is_working_moment(moment: datetime) -> bool:
    return moment.weekday() < SATURDAY and WORK_START <= moment.time() < WORK_END


def last_working_day_start(target: datetime) -> datetime:
    """Начало последнего рабочего дня, который наступает не позже target."""
    candidate = target.replace(
        hour=WORK_START.hour, minute=WORK_START.minute, second=0, microsecond=0
    )
    for _ in range(MAX_LOOKBACK_DAYS):
        if candidate <= target and candidate.weekday() < SATURDAY:
            return candidate
        candidate -= timedelta(days=1)
    return candidate


def alert_moment(
    end_date: datetime, lead_hours: int, respect_working_hours: bool = True
) -> datetime:
    """Момент, начиная с которого лот пора показать менеджеру."""
    target = end_date - timedelta(hours=lead_hours)
    if not respect_working_hours or is_working_moment(target):
        return target
    return last_working_day_start(target)


def should_alert(
    end_date: datetime,
    now: datetime,
    lead_hours: int = 24,
    respect_working_hours: bool = True,
) -> bool:
    """Показывать ли лот прямо сейчас.

    lead_hours — минимальный запас: позже этого срока до дедлайна молчать нельзя.
    """
    if end_date <= now:
        return False  # приём уже закрыт
    return now >= alert_moment(end_date, lead_hours, respect_working_hours)


def hours_left(end_date: datetime, now: datetime) -> float:
    return (end_date - now).total_seconds() / 3600


def parse(end_date: str) -> datetime:
    """Даты площадки приходят без таймзоны и обозначают время Астаны."""
    return datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA)


def now() -> datetime:
    return datetime.now(ASTANA)
