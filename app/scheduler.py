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

from . import config, monitor, notify, portal, sheet, store, watch
from .goszakup import GoszakupClient
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


# Сколько дней после дедлайна ещё пытаться прочитать счётчик, если страница молчит.
ZERO_AUDIT_GRACE_DAYS = 7


def _audit_zero_closed(settings: config.Settings) -> None:
    """Перепроверить закрывшиеся 0-лоты: реально ли без участников.

    Лот, отправленный как «0 заявок», мог собрать заявки в последние часы. После
    дедлайна читаем счётчик со страницы: 0 — в копилку closed_zero, >0 — забываем.
    None (страница не прочиталась) — оставляем на следующий раз, но не дольше grace.
    """
    now = datetime.now(ASTANA)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (now - timedelta(days=ZERO_AUDIT_GRACE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    store.purge_stale_zero_pending(cutoff)
    for lot in store.zero_pending_due(now_str):
        count = portal.application_count(lot["trd_buy_id"])
        if count is None:
            continue  # страница молчит — попробуем в следующий раз
        if count == 0:
            store.add_closed_zero(lot)
        store.remove_zero_pending(lot["lot_id"])


def _daily_heartbeat(token: str, chat_id: str, text: str) -> None:
    """Проверка-датчик в конкретный чат не чаще раза в день (по Астане).

    Датчик жизни: если в теме за день не было тендеров, всё равно подаём голос,
    чтобы молчание означало поломку, а не спокойный день. Троттл — по chat_id,
    поэтому у каждой темы (0, 3+, БИН-о-наличии, БИН-по-ЕНСТРУ) он свой.
    """
    if not chat_id:
        return
    today = datetime.now(ASTANA).date().isoformat()
    key = "hb:" + str(chat_id)
    if _state.get(key) == today:
        return
    try:
        notify.send(token, chat_id, text)
        _state[key] = today
    except Exception as exc:  # noqa: BLE001 — битая тема не должна ронять проход
        log.warning("проверка в %s не ушла: %s", chat_id, exc)


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
        # Каждая тема — независимо и в своём try. Сбой отправки в одну тему НЕ должен
        # оставлять непомеченными лоты, уже доставленные в другую, иначе те долбят
        # каждый скан. Помечается (попадает в dispatched) только реально доставленное;
        # упавшее — не помечается и повторится в следующий проход.
        if main:
            try:
                notify.send_lots(
                    settings.telegram_bot_token, settings.telegram_chat_id, main
                )
                dispatched += main
            except Exception as exc:  # noqa: BLE001
                log.warning("отправка в основной чат упала, лоты повторим: %s", exc)
        if comp:
            # Второй поток можно слать тем же ботом в другой чат (задан только
            # competition_chat_id) или отдельным ботом (задан и competition_bot_token).
            comp_token = settings.competition_bot_token or settings.telegram_bot_token
            if settings.competition_chat_id and comp_token:
                try:
                    notify.send_lots(comp_token, settings.competition_chat_id, comp)
                    dispatched += comp
                except Exception as exc:  # noqa: BLE001
                    log.warning("отправка в тему 3+ упала, лоты повторим: %s", exc)
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
        low = [lot for lot in result.lots if not settings.is_competition(lot.get("applications"))]
        high = [lot for lot in result.lots if settings.is_competition(lot.get("applications"))]
        # Тема "0 заявок" (нижний диапазон) — основной бот/чат.
        if not low:
            _daily_heartbeat(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                notify.format_heartbeat(result, settings, apps_desc=f"≤ {settings.max_applications}"),
            )
        # Тема "3+" (верхний диапазон) — только если она настроена.
        if settings.min_competition is not None and settings.competition_chat_id and not high:
            _daily_heartbeat(
                settings.competition_bot_token or settings.telegram_bot_token,
                settings.competition_chat_id,
                notify.format_heartbeat(result, settings, apps_desc=f"≥ {settings.min_competition}"),
            )

    # БИН-треки — ДВА независимых набора заказчиков, в разные чаты:
    #   A: watch_bins        -> bin_chat_id        — любой тендер («о наличии»)
    #   B: watch_bins_enstru -> bin_enstru_chat_id — только профильные (список ЕНСТРУ)
    # Оба изолированы общим try/except: падение (таймаут площадки) не задевает
    # основной скан, который выше уже отработал и разослал лоты.
    groups = []
    if settings.watch_bins and settings.bin_chat_id:
        groups.append(("all", settings.watch_bins, settings.bin_chat_id))
    if settings.watch_bins_enstru and settings.bin_enstru_chat_id:
        groups.append(("enstru", settings.watch_bins_enstru, settings.bin_enstru_chat_id))
    if groups:
        try:
            need_keywords = any(kind == "enstru" for kind, _, _ in groups)
            our_codes = {item["code"] for item in codes} if need_keywords else None
            with GoszakupClient(settings.goszakup_token) as client:
                our_names = None
                if need_keywords:
                    # official_names берёт из кэша enstru_cache — почти бесплатно.
                    names, _ = monitor.official_names(client, codes)
                    our_names = {n.lower() for n in names}
                token = settings.bin_bot_token or settings.telegram_bot_token
                for kind, bins, chat_id in groups:
                    cc = our_codes if kind == "enstru" else None
                    nn = our_names if kind == "enstru" else None
                    fresh = watch.find_new(client, settings, bins, cc, nn)
                    if fresh:
                        notify.send_customer_lots(token, chat_id, fresh)
                        for lot in fresh:
                            store.mark_notified_bin(lot)
                        log.info("БИН-трек [%s]: отправлено %s", kind, len(fresh))
                    elif settings.daily_report:
                        # Тендеров не было — подаём голос (датчик жизни), раз в день.
                        label = "профильных" if kind == "enstru" else "новых"
                        _daily_heartbeat(
                            token, chat_id,
                            f"🟢 <b>Проверка выполнена, {label} тендеров нет</b>\n\n"
                            "<i>Приходит раз в день. Пропало — мониторинг не работает.</i>",
                        )
        except Exception as exc:  # noqa: BLE001 — трек не должен ронять проход
            log.warning("БИН-трек упал (основной скан не затронут): %s", exc)

    # Аудит закрывшихся 0-лотов + суточный дайджест «закрылись без участников».
    # Изолирован: своя перепроверка счётчика, свой троттл, свой try/except.
    if settings.closed_zero_chat_id:
        try:
            _audit_zero_closed(settings)
            if settings.daily_report:
                token = settings.bin_bot_token or settings.telegram_bot_token
                closed = store.list_closed_zero()
                today = datetime.now(ASTANA).date().isoformat()
                if closed:
                    if _state.get("last_closed_zero") != today:
                        notify.send_closed_zero_digest(
                            token, settings.closed_zero_chat_id, closed
                        )
                        store.clear_closed_zero()
                        _state["last_closed_zero"] = today
                        log.info("дайджест «закрылись с 0»: отправлено %s", len(closed))
                else:
                    _daily_heartbeat(
                        token, settings.closed_zero_chat_id,
                        "🟢 <b>Проверка выполнена</b>\n\nЗа сутки лотов, закрывшихся "
                        "без участников, нет.\n\n<i>Приходит раз в день.</i>",
                    )
        except Exception as exc:  # noqa: BLE001 — не должен ронять проход
            log.warning("аудит закрытых 0-лотов упал (проход не затронут): %s", exc)

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
