"""Расписание: когда следующий проход.

Для суточного режима время должно быть фиксированным, а не "через 24 часа после
прошлого раза". Сервер стоит в офисе: пропало питание, машина поднялась в 3 ночи —
и при интервальной логике все сканы навсегда переезжают на 3 ночи, а менеджеры
получают лоты, когда спят.
"""

import dataclasses
from datetime import datetime, timedelta

import pytest

from app import scheduler
from app.config import Settings
from app.window import ASTANA


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ASTANA)


@pytest.fixture
def daily():
    return Settings(poll_interval_minutes=1440, daily_scan_at="09:00")


def test_daily_scan_waits_until_the_fixed_hour(daily):
    """Проход в 03:00 (сервер перезагрузился) не должен назначать следующий на 03:00."""
    nxt = scheduler.next_run_at(daily, now=at("2026-07-20 03:00"))

    assert nxt == at("2026-07-20 09:00"), "ждём утра, а не сутки от перезагрузки"


def test_daily_scan_moves_to_tomorrow_if_hour_already_passed(daily):
    nxt = scheduler.next_run_at(daily, now=at("2026-07-20 10:00"))

    assert nxt == at("2026-07-21 09:00")


def test_daily_scan_time_survives_restarts(daily):
    """Хоть каждый час перезагружайся — скан всё равно в 09:00."""
    for hour in ("00:30", "05:00", "08:59", "13:00", "23:45"):
        nxt = scheduler.next_run_at(daily, now=at(f"2026-07-20 {hour}"))
        assert nxt.hour == 9 and nxt.minute == 0, f"сорвалось при рестарте в {hour}"


def test_frequent_scan_uses_the_interval():
    settings = Settings(poll_interval_minutes=120)
    nxt = scheduler.next_run_at(settings, now=at("2026-07-20 10:00"))

    assert nxt == at("2026-07-20 12:00")


def test_broken_time_falls_back_to_interval_instead_of_crashing(daily):
    """Кривое значение в настройках не должно ронять расписание насмерть."""
    broken = dataclasses.replace(daily, daily_scan_at="девять утра")

    nxt = scheduler.next_run_at(broken, now=at("2026-07-20 10:00"))

    assert nxt == at("2026-07-21 10:00"), "откатились на интервал, расписание живо"


def test_interval_never_goes_below_a_minute():
    """Защита от 0 в настройках: иначе поток съест площадку и процессор."""
    settings = Settings(poll_interval_minutes=0)
    nxt = scheduler.next_run_at(settings, now=at("2026-07-20 10:00"))

    assert nxt - at("2026-07-20 10:00") >= timedelta(minutes=5)


class _Result:
    matched = 42
    unreadable: list = []


def test_heartbeat_says_it_is_alive_and_why_it_is_silent():
    settings = Settings(window_hours_min=48, max_applications=0, min_amount=2_000_000)

    text = scheduler.notify.format_heartbeat(_Result(), settings)

    assert "48" in text, "условия отбора видны — иначе непонятно, что проверяли"
    assert "42" in text, "видно, что обход реально шёл"
    assert "пропало" in text.lower(), "объясняем, что тишина = поломка"


def test_heartbeat_warns_about_unreadable_counters():
    settings = Settings()
    result = _Result()
    result.unreadable = ["17339073-1", "17339072-1"]

    text = scheduler.notify.format_heartbeat(result, settings)

    assert "2" in text and "не прочитался" in text


def test_loop_fires_immediately_and_catches_up(monkeypatch):
    """Планировщик делает проход сразу при старте и не ждёт до завтра.

    Именно этого не хватало: после сна ПК длинный таймер просыпал момент 09:00,
    и проход в тот день не случался вовсе.
    """
    calls = []

    def ok_pass():
        calls.append(1)
        return True  # успех: расписание пойдёт по next_run_at, не по retry

    monkeypatch.setattr(scheduler, "_do_pass", ok_pass)
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
    # следующий проход — далеко, чтобы после первого прохода цикл сразу упёрся в stop
    monkeypatch.setattr(scheduler, "next_run_at",
                        lambda s, now=None: at("2099-01-01 09:00"))
    monkeypatch.setattr(scheduler.config, "load", lambda: Settings())

    scheduler._stop.set()  # цикл выйдет после первого прохода
    scheduler._loop()

    assert calls == [1], "проход должен пройти сразу при запуске"


def test_loop_runs_again_once_target_time_arrives(monkeypatch):
    """Когда наступает время следующего прохода, цикл его выполняет."""
    calls = []
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
    # target в прошлом -> на первом же тике условие now >= target истинно
    monkeypatch.setattr(scheduler, "next_run_at",
                        lambda s, now=None: at("2000-01-01 09:00"))
    monkeypatch.setattr(scheduler.config, "load", lambda: Settings())

    # успешный проход -> расписание по next_run_at (в прошлом), тик сразу повторит
    def stop_after_two():
        calls.append(1)
        if len(calls) >= 2:
            scheduler._stop.set()
        return True
    monkeypatch.setattr(scheduler, "_do_pass", stop_after_two)

    scheduler._stop.clear()
    scheduler._loop()

    assert len(calls) >= 2, "проход при старте + проход по наступлению времени"


def test_failed_pass_retries_soon_not_tomorrow(monkeypatch):
    """Площадка легла в 09:00 — следующая попытка через 20 минут, а не завтра."""
    monkeypatch.setattr(scheduler.config, "load", lambda: Settings())
    before = scheduler.datetime.now(scheduler.ASTANA)

    target = scheduler._schedule_next(succeeded=False)

    delta = (target - before).total_seconds() / 60
    assert 15 <= delta <= 25, f"повтор должен быть через ~20 мин, а не {delta:.0f}"


def test_successful_pass_uses_normal_schedule(monkeypatch):
    monkeypatch.setattr(scheduler.config, "load",
                        lambda: Settings(poll_interval_minutes=1440, daily_scan_at="09:00"))
    target = scheduler._schedule_next(succeeded=True)
    assert target.hour == 9 and target.minute == 0


def test_alert_only_on_state_change(monkeypatch):
    """Сбой озвучиваем один раз, а не на каждом из повторов каждые 20 минут."""
    sent = []
    monkeypatch.setattr(scheduler, "_alert", lambda text: sent.append(text))
    monkeypatch.setattr(scheduler, "run_once",
                        lambda: (_ for _ in ()).throw(RuntimeError("площадка 500")))

    scheduler._state["last_error"] = None
    scheduler._do_pass()   # первый сбой — алерт
    scheduler._do_pass()   # второй подряд — молчим
    scheduler._do_pass()   # третий — молчим

    assert len(sent) == 1, "алерт о сбое должен уйти ровно один раз"
    assert "не удалось" in sent[0].lower()


def test_recovery_is_announced(monkeypatch):
    calls = {"n": 0}
    sent = []
    monkeypatch.setattr(scheduler, "_alert", lambda text: sent.append(text))

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("площадка 500")
        return {"matched": 0, "notified": 0, "passed": 0, "unreadable": []}

    monkeypatch.setattr(scheduler, "run_once", flaky)

    scheduler._state["last_error"] = None
    scheduler._do_pass()   # сбой -> алерт
    scheduler._do_pass()   # успех -> "восстановлено"

    assert len(sent) == 2
    assert "восстановлен" in sent[1].lower()
