from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum


class PlanStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    CAPTCHA = "captcha"
    PAYMENT = "payment"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"
    FAILED = "failed"


def parse_clock(value: str) -> time:
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, pattern).time()
        except ValueError:
            continue
    raise ValueError(f"时间格式错误：{value!r}，应为 HH:MM 或 HH:MM:SS")


def validate_time_window(start_time: str, end_time: str) -> None:
    start = parse_clock(start_time)
    end = parse_clock(end_time)
    if start >= end:
        raise ValueError("开始时间必须早于结束时间")


@dataclass(frozen=True, slots=True)
class DailyPlan:
    run_date: date
    target_date: date
    enabled: bool
    start_time: str
    end_time: str
    status: PlanStatus = PlanStatus.DRAFT

    def __post_init__(self) -> None:
        validate_time_window(self.start_time, self.end_time)
        if self.target_date <= self.run_date:
            raise ValueError("预约日期必须晚于程序运行日期")

    @classmethod
    def for_run_date(
        cls,
        run_date: date,
        days_ahead: int,
        start_time: str,
        end_time: str,
        enabled: bool = False,
    ) -> "DailyPlan":
        return cls(
            run_date=run_date,
            target_date=run_date + timedelta(days=days_ahead),
            enabled=enabled,
            start_time=start_time,
            end_time=end_time,
        )


@dataclass(frozen=True, slots=True)
class SlotCandidate:
    venue_id: str
    dom_index: int
    label: str


@dataclass(frozen=True, slots=True)
class Event:
    created_at: datetime
    level: str
    event_type: str
    message: str

