import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from buaa_booking.cli import _effective_run_date_for_booking_window, _resolve_or_create_plan_for_booking_window
from buaa_booking.models import DailyPlan, PlanStatus
from buaa_booking.store import StateStore


def test_before_open_time_keeps_current_run_date():
    now = datetime(2026, 9, 20, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    assert _effective_run_date_for_booking_window(now, "07:00:00") == date(2026, 9, 20)


def test_after_open_time_keeps_launch_day_and_books_second_day_ahead():
    now = datetime(2026, 9, 20, 22, 30, tzinfo=timezone(timedelta(hours=8)))
    assert _effective_run_date_for_booking_window(now, "07:00:00") == date(2026, 9, 20)
    plan = DailyPlan.for_run_date(date(2026, 9, 20), 2, "20:00", "21:00")
    assert plan.target_date == date(2026, 9, 22)


def test_stale_plan_is_corrected_to_current_days_ahead_target():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        store = StateStore(Path(temp_dir) / "state.sqlite3")
        run_date = date(2026, 9, 20)
        stale = DailyPlan(
            run_date=run_date,
            target_date=date(2026, 9, 23),
            enabled=True,
            start_time="20:00",
            end_time="21:00",
            status=PlanStatus.CONFIRMED,
        )
        store.save_plan(stale)

        class Config:
            open_time = "07:00:00"
            days_ahead = 2
            default_start_time = "20:00"
            default_end_time = "21:00"

        now = datetime(2026, 9, 20, 7, 10, tzinfo=timezone(timedelta(hours=8)))
        resolved = _resolve_or_create_plan_for_booking_window(Config(), store, now)

        assert resolved.run_date == run_date
        assert resolved.target_date == date(2026, 9, 22)


def test_debug_config_forces_enabled_plan_for_run_date():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        store = StateStore(Path(temp_dir) / "state.sqlite3")
        run_date = date(2026, 9, 20)
        stale = DailyPlan(
            run_date=run_date,
            target_date=date(2026, 9, 22),
            enabled=False,
            start_time="7:00",
            end_time="8:00",
            status=PlanStatus.SKIPPED,
        )
        store.save_plan(stale)

        class Config:
            open_time = "07:00:00"
            days_ahead = 2
            default_start_time = "07:00"
            default_end_time = "08:00"

        now = datetime(2026, 9, 20, 7, 10, tzinfo=timezone(timedelta(hours=8)))
        resolved = _resolve_or_create_plan_for_booking_window(Config(), store, now, force_enabled=True)

        assert resolved.run_date == run_date
        assert resolved.target_date == date(2026, 9, 22)
        assert resolved.enabled is True


def test_keep_only_recent_three_days_of_events():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        store = StateStore(Path(temp_dir) / "state.sqlite3")
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone(timedelta(hours=8)))

        recent = [
            (now - timedelta(days=0), "info", "today"),
            (now - timedelta(days=1), "info", "yesterday"),
            (now - timedelta(days=2), "info", "two_days_ago"),
            (now - timedelta(days=3), "info", "three_days_ago"),
            (now - timedelta(days=10), "warning", "old"),
        ]

        for created_at, level, message in recent:
            store.add_event(level, message, message)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE events SET created_at = ? WHERE message = ? AND rowid = (SELECT MAX(rowid) FROM events WHERE message = ?)",
                    (created_at.isoformat(), message, message),
                )

        store.purge_old_events(days=3, now=now)
        events = store.recent_events(limit=20)
        messages = [event.message for event in events]

        assert "today" in messages
        assert "yesterday" in messages
        assert "two_days_ago" in messages
        assert "three_days_ago" not in messages
        assert "old" not in messages
