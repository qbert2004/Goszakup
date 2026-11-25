"""Расписание скана продаж: интервал vs раз в день в sales_scan_at."""

from app.config import Settings
from app.sales import loop


def test_next_scan_delay_interval():
    s = Settings(sales_scan_interval_minutes=30)
    assert loop._next_scan_delay(s) == 30 * 60


def test_next_scan_delay_daily_at_time():
    s = Settings(sales_scan_interval_minutes=1440, sales_scan_at="09:00")
    d = loop._next_scan_delay(s)
    assert 0 < d <= 24 * 3600  # пауза до ближайших 09:00 по Астане


def test_next_scan_delay_floor():
    s = Settings(sales_scan_interval_minutes=1)  # меньше 5 -> не чаще 5 минут
    assert loop._next_scan_delay(s) == 5 * 60
