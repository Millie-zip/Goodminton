from datetime import date

import pytest

from buaa_booking.automation import _slot_matches
from buaa_booking.models import DailyPlan, parse_clock


def test_parse_clock_accepts_minutes_and_seconds() -> None:
    assert parse_clock("07:00").hour == 7
    assert parse_clock("07:00:05").second == 5


def test_plan_calculates_target_date() -> None:
    plan = DailyPlan.for_run_date(date(2026, 9, 10), 3, "19:00", "21:00", True)
    assert plan.target_date == date(2026, 9, 13)
    assert plan.enabled is True


def test_rejects_reversed_time_window() -> None:
    with pytest.raises(ValueError, match="开始时间"):
        DailyPlan.for_run_date(date(2026, 9, 10), 3, "21:00", "19:00")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("1号场 19:00-21:00 可预约", True),
        ("2号场 19：00 至 21：00", True),
        ("3号场 18:00-19:00", False),
    ],
)
def test_slot_matching_handles_common_separators(label: str, expected: bool) -> None:
    plan = DailyPlan.for_run_date(date(2026, 9, 10), 3, "19:00", "21:00")
    assert _slot_matches(label, plan) is expected

