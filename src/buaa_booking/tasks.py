from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta

from .models import PlanStatus
from .store import StateStore


REMINDER_TASK_NAME = "BUAA-Badminton-Reminder"


def reminder_run_date(now: datetime) -> date:
    """凌晨提醒当天运行的计划，其余时间对应下一天。"""
    return now.date() if now.hour < 2 else now.date() + timedelta(days=1)


def set_reminder_enabled(enabled: bool, store: StateStore) -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Change", "/TN", REMINDER_TASK_NAME,
             "/ENABLE" if enabled else "/DISABLE"],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError(f"schtasks 退出码 {result.returncode}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        store.add_event("warning", "reminder_task_failed", f"修改提醒任务失败：{exc}")
        return False
    store.add_event(
        "info", "reminder_task_enabled" if enabled else "reminder_task_disabled",
        "已启用下一轮提醒任务" if enabled else "已停用后续 15 分钟提醒任务",
    )
    return True


def sync_reminder_task(store: StateStore, now: datetime) -> bool:
    plan = store.get_plan(reminder_run_date(now))
    return set_reminder_enabled(plan is None or plan.status != PlanStatus.CONFIRMED, store)
