"""Классификация закупок: new / failed / repeat / None."""

from app.sales.queries import category, is_approved_plan, _has_parent


def _buy(status_id=None, status_name="", parent=None, year=2026):
    return {
        "refBuyStatusId": status_id,
        "RefBuyStatus": {"nameRu": status_name},
        "parentId": parent,
        "finYear": year,
    }


def test_new_lot_receiving_no_parent():
    assert category(_buy(status_id=220), 2026) == "new"


def test_repeat_lot_has_parent():
    assert category(_buy(status_id=220, parent=17900000), 2026) == "repeat"


def test_failed_primary_current_year():
    assert category(_buy(status_name="Не состоялась", year=2026), 2026) == "failed"


def test_failed_but_not_current_year_is_none():
    assert category(_buy(status_name="Не состоялась", year=2025), 2026) is None


def test_failed_but_has_parent_is_none():
    # несостоявшаяся, но вторичная (есть родитель) — не наш «первый» случай
    assert category(_buy(status_name="Не состоялась", parent=123, year=2026), 2026) is None


def test_completed_status_is_none():
    assert category(_buy(status_id=350, status_name="Завершено"), 2026) is None


def test_parent_zero_is_not_parent():
    assert _has_parent({"parentId": 0}) is False
    assert _has_parent({"parentId": "0"}) is False
    assert _has_parent({"parentId": 123}) is True


def test_is_approved_plan():
    assert is_approved_plan({"RefPlnPointStatus": {"nameRu": "Утвержден"}})
    assert not is_approved_plan({"RefPlnPointStatus": {"nameRu": "Проект"}})
    assert not is_approved_plan({})
