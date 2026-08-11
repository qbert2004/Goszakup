"""Отбор по числу заявок: max_applications + min_competition."""

from app.config import Settings


def test_zero_only_by_default():
    # Дефолт из ТЗ: только нулевая конкуренция.
    s = Settings(max_applications=0)
    assert s.applications_ok(0)
    assert not s.applications_ok(1)
    assert not s.applications_ok(5)


def test_low_competition_contiguous():
    # "До 4": проходят 0..4, пятая уже нет.
    s = Settings(max_applications=4)
    assert all(s.applications_ok(n) for n in (0, 1, 2, 3, 4))
    assert not s.applications_ok(5)


def test_zero_and_three_plus_with_gap():
    # Требование заказчика: "0 и 3+", 1-2 отсекаются.
    s = Settings(max_applications=0, min_competition=3)
    assert s.applications_ok(0)
    assert not s.applications_ok(1)
    assert not s.applications_ok(2)
    assert s.applications_ok(3)
    assert s.applications_ok(4)
    assert s.applications_ok(100)


def test_applications_desc_matches_rule():
    # Текст для heartbeat должен совпадать с реальным правилом.
    assert Settings(max_applications=0).applications_desc() == "≤ 0"
    assert Settings(max_applications=4).applications_desc() == "≤ 4"
    assert Settings(max_applications=0, min_competition=3).applications_desc() == "0 или ≥ 3"
    assert Settings(max_applications=1, min_competition=5).applications_desc() == "≤ 1 или ≥ 5"
