"""Проверка момента уведомления.

Главный сценарий: дедлайн в понедельник утром. Наивные 24-48 часов дают уведомление
в выходной, менеджер видит его в понедельник, когда уже поздно. На пн/вт приходится
половина всех дедлайнов площадки, поэтому случай не краевой, а основной.
"""

from datetime import datetime, timedelta

from app.window import (
    ASTANA,
    alert_moment,
    hours_left,
    is_working_moment,
    last_working_day_start,
    parse,
    should_alert,
)


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ASTANA)


# 2026-07-17 — пятница, 18-19 — выходные, 20 — понедельник.

def test_working_moment_recognises_non_working_time():
    assert is_working_moment(dt("2026-07-17 10:00"))      # пятница днём
    assert not is_working_moment(dt("2026-07-18 10:00"))  # суббота
    assert not is_working_moment(dt("2026-07-19 10:00"))  # воскресенье
    assert not is_working_moment(dt("2026-07-17 22:00"))  # вечер
    assert not is_working_moment(dt("2026-07-17 07:00"))  # раннее утро


def test_weekend_falls_back_to_friday_morning():
    result = last_working_day_start(dt("2026-07-19 09:00"))  # воскресенье
    assert result == dt("2026-07-17 09:00"), "должно быть утро пятницы"


def test_monday_deadline_alerts_on_friday_morning():
    """Ключевой случай, ради которого всё это написано."""
    deadline = dt("2026-07-20 09:00")

    moment = alert_moment(deadline, lead_hours=24)

    assert moment == dt("2026-07-17 09:00"), "утро пятницы, а не выходной"
    assert is_working_moment(moment)
    assert hours_left(deadline, moment) >= 24, "запас меньше суток недопустим"


def test_monday_deadline_is_not_silent_during_friday():
    """Весь рабочий день пятницы лот обязан быть виден."""
    deadline = dt("2026-07-20 09:00")

    for hour in ("09:00", "12:00", "17:30"):
        assert should_alert(deadline, now=dt(f"2026-07-17 {hour}")), f"молчит в {hour}"


def test_naive_window_would_have_stayed_silent_on_friday():
    """Фиксируем поведение, которое мы чиним."""
    deadline = dt("2026-07-20 09:00")
    friday = dt("2026-07-17 17:30")

    assert hours_left(deadline, friday) > 48, "лот вне наивного окна 24-48ч"
    assert not should_alert(deadline, now=friday, respect_working_hours=False)
    assert should_alert(deadline, now=friday), "с поправкой обязан сработать"


def test_midweek_deadline_alerts_exactly_a_day_before():
    """В будни поправка не нужна и не должна ничего смещать."""
    deadline = dt("2026-07-23 10:00")  # четверг
    assert alert_moment(deadline, lead_hours=24) == dt("2026-07-22 10:00")


def test_deadline_after_hours_uses_same_day_morning():
    """Дедлайн во вторник вечером: за сутки — вечер понедельника, нерабочий.
    Последний рабочий день с нужным запасом — сам понедельник."""
    deadline = dt("2026-07-21 20:00")  # вторник, вечер
    moment = alert_moment(deadline, lead_hours=24)

    assert moment == dt("2026-07-20 09:00"), "утро понедельника"
    assert hours_left(deadline, moment) >= 24


def test_distant_deadline_is_not_alerted_yet():
    deadline = dt("2026-07-31 10:00")
    assert not should_alert(deadline, now=dt("2026-07-17 10:00"))


def test_closed_lot_is_never_alerted():
    assert not should_alert(dt("2026-07-17 09:00"), now=dt("2026-07-17 10:00"))


def test_lot_inside_window_is_alerted():
    deadline = dt("2026-07-23 10:00")  # четверг
    assert should_alert(deadline, now=dt("2026-07-22 11:00"))  # среда, 23ч запаса


def test_lead_hours_is_respected_for_every_weekday_deadline():
    """Запас не должен нарушаться ни для одного дедлайна ближайших двух недель."""
    start = dt("2026-07-20 09:00")
    for offset in range(0, 24 * 14):
        deadline = start + timedelta(hours=offset)
        if deadline.weekday() >= 5:
            continue
        moment = alert_moment(deadline, lead_hours=24)
        assert hours_left(deadline, moment) >= 24, f"запас нарушен для {deadline}"
        assert is_working_moment(moment), f"нерабочий момент для {deadline}"


def test_parse_treats_platform_time_as_astana():
    parsed = parse("2026-07-20 09:00:00")
    assert parsed.utcoffset() == timedelta(hours=5)
    assert parsed.hour == 9
