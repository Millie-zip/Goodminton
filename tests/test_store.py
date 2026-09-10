from datetime import date
from pathlib import Path

from buaa_booking.models import DailyPlan, PlanStatus
from buaa_booking.store import StateStore


def test_plan_round_trip_and_status_update(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    plan = DailyPlan.for_run_date(date(2026, 9, 10), 3, "19:00", "21:00", True)
    store.save_plan(plan)
    loaded = store.get_plan(plan.run_date)
    assert loaded == plan

    store.update_status(plan.run_date, PlanStatus.CAPTCHA)
    assert store.get_plan(plan.run_date).status == PlanStatus.CAPTCHA  # type: ignore[union-attr]


def test_recent_events_are_newest_first(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.add_event("info", "first", "第一条")
    store.add_event("warning", "second", "第二条")
    assert [event.event_type for event in store.recent_events()] == ["second", "first"]

