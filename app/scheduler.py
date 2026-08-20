"""Фоновый обход по расписанию.

Требование заказчика — автономность: работает само, без ручного перезапуска.
Поэтому поток живёт внутри приложения и переживает любые ошибки одного прохода:
упавший обход не должен убивать расписание. Всё, что произошло, пишется в базу
и видно в админке.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import config, monitor, notify, sheet, store
from .window import ASTANA

log = logging.getLogger("monitor")

_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict = {"running": False, "last_run": None, "last_error": None, "next_run": None}


def status() -> dict:
    return {**_state, "runs": store.last_runs(5)}


def next_run_at(settings: config.Settings, now: datetime | None = None) -> datetime:
    """Когда следующий проход.

    Для суточного режима — фиксированное время по Астане, а не "через 24 часа
    после прошлого раза". Иначе перезагрузка сервера (например после сбоя питания
    в офисе) навсегда сдвигает скан на случайный час ночи.
    """
    now = now or datetime.now(ASTANA)

    if settings.poll_interval_minutes >= 1440 and settings.daily_scan_at:
        try:
            hour, minute = (int(part) for part in settings.daily_scan_at.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, TypeError):
            log.warning("не понял daily_scan_at=%r, иду по интервалу",
                        settings.daily_scan_at)
        else:
            if target <= now:
                target += timedelta(days=1)
            return target

    return now + timedelta(minutes=max(settings.poll_interval_minutes, 5))


def run_once() -> dict:
    """Один полноценный проход с отправкой в Telegram."""
    settings = config.load_or_die()
    codes = sheet.load_codes(settings.sheet_url)

    # Забываем лоты, у которых приём уже закрылся: помнить их незачем — отправлены
    # они больше не будут в любом случае. Заодно база не растёт бесконечно.
    forgotten = store.purge_closed()
    if forgotten:
        log.info("забыто закрывшихся лотов: %s", forgotten)

    run_id = store.start_run()

    def send(lots: list[dict]) -> list[dict]:
        # Два потока: 0 / <= max_applications — основному боту менеджерам,
        # 3+ (открытая конкуренция) — в отдельный бот. Разные аудитории.
        # Возвращаем реально отправленные лоты, чтобы неотправленные (второй бот
        # не настроен) не пометились в базе и попали в следующий проход.
        main = [lot for lot in lots if not settings.is_competition(lot.get("applications"))]
        comp = [lot for lot in lots if settings.is_competition(lot.get("applications"))]
        dispatched: list[dict] = []
        if main:
            notify.send_lots(
                settings.telegram_bot_token, settings.telegram_chat_id, main
            )
            dispatched += main
        if comp:
            # Второй поток можно слать тем же ботом в другой чат (задан только
            # competition_chat_id) или отдельным ботом (задан и competition_bot_token).
            comp_token = settings.competition_bot_token or settings.telegram_bot_token
            if settings.competition_chat_id and comp_token:
                notify.send_lots(comp_token, settings.competition_chat_id, comp)
                dispatched += comp
            else:
                # Второй чат не задан — лоты 3+ не смешиваем с основным потоком
                # и НЕ помечаем отправленными: честно жалуемся, ждём настройки.
                log.warning(
                    "лоты с открытой конкуренцией (3+) есть, но competition_chat_id "
                    "не задан — не отправлены: %s",
                    [lot.get("number_anno") for lot in comp],
                )
        return dispatched

    try:
        result = monitor.run(settings, codes, send=send)
    except Exception as exc:
        store.finish_run(run_id, 0, 0, 0, error=str(exc))
        raise

    store.finish_run(run_id, result.matched, result.passed, result.notified)

    # Датчик жизни по каждой теме: молчание должно означать поломку, а не спокойный
    # день. Тема получает "проверку" только если в ЕЁ диапазоне ничего не нашлось —
    # если лоты были, они сами и есть признак жизни.
    # Сканов может быть много (частый опрос ловит лоты быстрее), но проверка — это
    # СУТОЧНЫЙ сигнал, а не сообщение на каждый скан. Поэтому шлём не чаще раза в
    # день (по Астане) на тему, иначе heartbeat спамит при частом опросе.
    if settings.daily_report:
        today = datetime.now(ASTANA).date().isoformat()
        low = [lot for lot in result.lots if not settings.is_competition(lot.get("applications"))]
        high = [lot for lot in result.lots if settings.is_competition(lot.get("applications"))]
        # Тема "0 заявок" (нижний диапазон) — основной бот/чат.
        if not low and _state.get("last_hb_low") != today:
            notify.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                notify.format_heartbeat(result, settings, apps_desc=f"≤ {settings.max_applications}"),
            )
            _state["last_hb_low"] = today
        # Тема "3+" (верхний диапазон) — только если она настроена.
        if (settings.min_competition is not None and settings.competition_chat_id
                and not high and _state.get("last_hb_high") != today):
            notify.send(
                settings.competition_bot_token or settings.telegram_bot_token,
                settings.competition_chat_id,
                notify.format_heartbeat(result, settings, apps_desc=f"≥ {settings.min_competition}"),
            )
            _state["last_hb_high"] = today

    return {
        "matched": result.matched,
        "passed": result.passed,
        "notified": result.notified,
        "unreadable": result.unreadable,
    }


# Тик короткий, чтобы пропущенный дедлайн подхватывался почти сразу. Один длинный
# сон на 20 часов ненадёжен: если машина уходила в сон (обычный офисный ПК ночью),
# таймер во сне не тикает — и поток "просыпает" момент скана. Короткими тиками мы
# на каждом проверяем реальное время и наверстываем просроченный проход.
TICK_SECONDS = 60

# Проход не удался (площадка лежит) — повторяем скоро, а не через сутки. Иначе при
# суточном расписании упавший в 09:00 проход перенёс бы следующую попытку на завтра,
# и ожившую через час площадку мы бы не заметили до утра.
RETRY_AFTER_FAILURE = timedelta(minutes=20)


def _do_pass() -> bool:
    """Один проход. Возвращает True при успехе, False если площадка подвела.

    Алерт шлём только на СМЕНЕ состояния — упало и восстановилось, — а не на каждом
    провале: при лежачей часами площадке повтор каждые 20 минут был бы спамом.
    Молчание у нас значит "монитор мёртв", поэтому первый сбой озвучиваем.
    """
    was_failing = _state.get("last_error") is not None
    try:
        result = run_once()
        if was_failing:
            _alert("🟢 <b>Связь восстановлена</b>\n\nПроверка лотов снова работает.")
        _state["last_error"] = None
        log.info(
            "проход завершён: в окне %s, отправлено %s",
            result["matched"], result["notified"],
        )
        return True
    except Exception as exc:
        # Один сбойный проход не должен останавливать расписание.
        _state["last_error"] = str(exc)
        log.warning("проход упал: %s", exc)
        if not was_failing:  # первый сбой в серии — озвучиваем один раз
            import html
            _alert(
                "🔴 <b>Не удалось проверить лоты</b>\n\n"
                f"{html.escape(str(exc))}\n\n"
                "<i>Буду повторять каждые 20 минут. Если это площадка — она "
                "восстановится сама, придёт «связь восстановлена». Если тишина "
                "затянулась — проверьте сервер.</i>"
            )
        return False
    finally:
        _state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _alert(text: str) -> None:
    """Служебное сообщение в Telegram. Ошибку отправки глотаем — падать из-за неё
    поверх основного события нельзя."""
    try:
        settings = config.load()
        if settings.telegram_bot_token and settings.telegram_chat_id:
            notify.send(settings.telegram_bot_token, settings.telegram_chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("не смог отправить служебный алерт: %s", exc)


def _loop() -> None:
    # Первый проход — сразу при запуске, чтобы не ждать до завтрашних 09:00.
    ok = _do_pass()
    target = _schedule_next(ok)

    while not _stop.wait(TICK_SECONDS):
        if datetime.now(ASTANA) < target:
            continue  # ещё не время — спим дальше короткими тиками
        ok = _do_pass()
        target = _schedule_next(ok)


def _schedule_next(succeeded: bool) -> datetime:
    if succeeded:
        target = next_run_at(config.load())
    else:
        # Площадка подвела — не ждём до завтра, пробуем скоро.
        target = datetime.now(ASTANA) + RETRY_AFTER_FAILURE
    _state["next_run"] = target.isoformat(timespec="seconds")
    log.info("следующий проход: %s", target.strftime("%Y-%m-%d %H:%M %Z"))
    return target


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="monitor", daemon=True)
    _thread.start()
    _state["running"] = True
    log.info("расписание запущено")


def stop() -> None:
    _stop.set()
    _state["running"] = False
