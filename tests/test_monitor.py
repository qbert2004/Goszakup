"""Отбор лотов: сумма, способ закупки, статус приёма.

По умолчанию лот в 20 часах от дедлайна — при запасе 24ч он уже должен уведомлять.
Лот в 30 часах уведомлять ещё рано: запас соблюдён, время терпит.
"""

import dataclasses
from datetime import timedelta

import pytest

from app import monitor, window
from app.config import Settings


@pytest.fixture
def settings():
    return Settings(
        goszakup_token="x",
        window_hours_min=24,
        max_applications=0,
        min_amount=2_000_000,
        respect_working_hours=False,
    )


def raw_lot(amount=5_000_000, method=3, status=240, hours=20.0, itogi=None):
    end = window.now() + timedelta(hours=hours)
    return {
        "id": 1,
        "lotNumber": "1-1",
        "nameRu": "Коммутатор сетевой",
        "amount": amount,
        "customerNameRu": "ГУ Тест",
        "trdBuyId": 10,
        "pointList": [99],
        "TrdBuy": {
            "id": 10,
            "numberAnno": "10-1",
            "endDate": end.strftime("%Y-%m-%d %H:%M:%S"),
            "itogiDatePublic": itogi,
            "refTradeMethodsId": method,
            "refBuyStatusId": status,
            "RefTradeMethods": {"nameRu": "ЗЦП"},
        },
    }


def test_big_lot_passes(settings):
    assert monitor._in_window(raw_lot(amount=5_000_000), settings)


def test_small_lot_is_dropped(settings):
    """В реальной выдаче висели лоты на 18 000 и 62 000 — это шум."""
    assert monitor._in_window(raw_lot(amount=18_000), settings) is None
    assert monitor._in_window(raw_lot(amount=1_999_999), settings) is None


def test_lot_exactly_at_threshold_passes(settings):
    assert monitor._in_window(raw_lot(amount=2_000_000), settings)


def test_unknown_amount_is_dropped_when_threshold_set(settings):
    """Сумма неизвестна — при заданном пороге молчим, а не гадаем."""
    assert monitor._in_window(raw_lot(amount=None), settings) is None


def test_unknown_amount_passes_when_no_threshold(settings):
    relaxed = dataclasses.replace(settings, min_amount=0)
    assert monitor._in_window(raw_lot(amount=None), relaxed)


def test_zero_threshold_keeps_small_lots(settings):
    relaxed = dataclasses.replace(settings, min_amount=0)
    assert monitor._in_window(raw_lot(amount=18_000), relaxed)


def test_single_source_is_skipped(settings):
    """Из одного источника: конкуренции нет, счётчика на странице тоже нет."""
    lot = raw_lot(method=monitor.METHOD_SINGLE_SOURCE)
    assert monitor._in_window(lot, settings) is None


def test_not_started_bidding_is_skipped(settings):
    """Приём ещё не открыт — счётчик появится позже, нуль там бессмысленный."""
    lot = raw_lot(status=monitor.STATUS_NOT_STARTED)
    assert monitor._in_window(lot, settings) is None


def test_published_results_are_skipped(settings):
    lot = raw_lot(itogi="2026-07-17 09:00:00")
    assert monitor._in_window(lot, settings) is None


def test_closed_lot_is_skipped(settings):
    assert monitor._in_window(raw_lot(hours=-1), settings) is None


def test_distant_lot_is_not_alerted_yet(settings):
    assert monitor._in_window(raw_lot(hours=200), settings) is None


def test_lot_with_our_code_survives_a_different_name(settings, monkeypatch):
    """Реальная потеря: на портале лот "Комплекс оборудования", в таблице позиция
    "Комплекс оборудования видеонаблюдения". Код при этом наш — терять нельзя."""
    codes = [{"code": "264033.900.000000", "name": "Комплекс оборудования видеонаблюдения"}]
    lot = raw_lot()
    lot["nameRu"] = "Комплекс оборудования"

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "264033.900.000000")
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [monitor._in_window(lot, s)])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    result = monitor.run(settings, codes, persist=False)

    assert result.passed == 1, "лот с нашим кодом обязан пройти, даже если имя другое"
    assert result.name_mismatch == 0


def test_foreign_code_is_dropped_even_if_name_matches(settings, monkeypatch):
    """Обратное: имя совпало, но код чужой — брать не должны."""
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}]
    lot = raw_lot()

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "999999.999.999999")
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [monitor._in_window(lot, s)])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    result = monitor.run(settings, codes, persist=False)

    assert result.passed == 0
    assert result.wrong_code == 1


def test_unresolvable_code_falls_back_to_name(settings, monkeypatch):
    """Код не сверился: своё имя берём, чужое — нет.

    Чужое имя тут "Мебель офисная": поиск идёт и по характеристике, поэтому запрос
    "Коммутатор сетевой" может вытащить лот, где коммутатор лишь упомянут в описании.
    Специально НЕ берём для примера "Комплекс медицинский" — он звучит посторонним,
    но это позиция заказчика (строка 17 таблицы, код 262015.000.000018).
    """
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}]
    ours = monitor._in_window(raw_lot(), settings)
    noise = monitor._in_window({**raw_lot(), "id": 2, "nameRu": "Мебель офисная"}, settings)

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: None)
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [ours, noise])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    result = monitor.run(settings, codes, persist=False)

    assert result.passed == 1, "свой лот по имени берём"
    assert result.unverified == 1
    assert result.name_mismatch == 1, "мусор из поиска по описанию отсекаем"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_dry_run_never_touches_the_database(settings, monkeypatch, tmp_path):
    """Сухой прогон обязан быть сухим.

    Иначе он тихо помечает лоты отправленными — и настоящий запуск про них
    промолчит. Ровно это и случилось: 109 лотов оказались в базе, ни одного
    сообщения при этом не ушло.
    """
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "dry.db")
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}]

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "263023.900.000076")
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [monitor._in_window(raw_lot(), s)])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    result = monitor.dry_run(settings, codes)

    assert result.passed == 1, "лот найден"
    with store.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM notified").fetchone()["c"]
    assert rows == 0, "сухой прогон записал лот в базу — настоящий запуск промолчит"


def test_real_run_marks_only_after_sending(settings, monkeypatch, tmp_path):
    """Отметка ставится после отправки, иначе сбой Telegram теряет лот навсегда."""
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "real.db")
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}]
    sent = []

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "263023.900.000076")
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [monitor._in_window(raw_lot(), s)])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    monitor.run(settings, codes, send=sent.extend, persist=True)

    assert len(sent) == 1
    assert store.already_notified(1), "отправленный лот запомнен"


def test_telegram_failure_does_not_lose_the_lot(settings, monkeypatch, tmp_path):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fail.db")
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"}]

    def boom(lots):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "263023.900.000076")
    monkeypatch.setattr(monitor, "candidates", lambda c, n, s: [monitor._in_window(raw_lot(), s)])
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "official_names", lambda c, codes: (
        {i["name"] for i in codes}, []))

    with pytest.raises(RuntimeError):
        monitor.run(settings, codes, send=boom, persist=True)

    assert not store.already_notified(1), (
        "лот помечен несмотря на сбой отправки — он потерян навсегда"
    )


def test_pass_aborts_fast_when_platform_is_down(settings, monkeypatch, tmp_path):
    """Площадка отдаёт HTTP 500 на всё — проход должен быстро сдаться, а не долбить
    все 89 позиций по 30с ретраев (это давало ~45 минут зависания)."""
    from app import store
    from app.goszakup import GoszakupError

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "down.db")
    codes = [{"code": f"26302{i}.900.000076", "name": f"Позиция {i}"} for i in range(20)]

    monkeypatch.setattr(monitor, "official_names",
                        lambda c, cc: ({i["name"] for i in cc}, []))
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())

    attempts = []

    def always_500(client, name, s):
        attempts.append(name)
        raise GoszakupError("Площадка вернула HTTP 500")

    monkeypatch.setattr(monitor, "candidates", always_500)

    with pytest.raises(GoszakupError, match="недоступна"):
        monitor.run(settings, codes, persist=False)

    assert len(attempts) <= monitor.PLATFORM_DOWN_STREAK, (
        "прервались быстро, а не обошли все 20 позиций"
    )


def test_single_position_failure_does_not_abort_the_pass(settings, monkeypatch, tmp_path):
    """Одна позиция моргнула — остальные обрабатываем, проход не роняем."""
    from app import store
    from app.goszakup import GoszakupError

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "blip.db")
    codes = [{"code": "263023.900.000076", "name": "Коммутатор сетевой"},
             {"code": "262013.000.000018", "name": "Сервер"}]

    monkeypatch.setattr(monitor, "official_names",
                        lambda c, cc: ({i["name"] for i in cc}, []))
    monkeypatch.setattr(monitor, "GoszakupClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(monitor, "_enstru_code_of", lambda c, l: "263023.900.000076")
    monkeypatch.setattr(monitor.portal, "application_count", lambda b: 0)

    def flaky(client, name, s):
        if name == "Сервер":
            raise GoszakupError("моргнуло")
        return [monitor._in_window(raw_lot(), s)]

    monkeypatch.setattr(monitor, "candidates", flaky)

    result = monitor.run(settings, codes, persist=False)  # не должно бросить

    assert result.passed == 1, "рабочая позиция обработана несмотря на сбой соседней"


def test_result_carries_amount_and_link(settings):
    found = monitor._in_window(raw_lot(amount=4_500_000), settings)
    assert found["amount"] == 4_500_000
    assert found["url"].endswith("/10?tab=lots")
    assert found["point_list"] == [99], "ключ нужен для сверки кода ЕНСТРУ"
