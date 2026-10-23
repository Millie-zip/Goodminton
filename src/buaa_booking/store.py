from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import DailyPlan, Event, PlanStatus


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_plans (
                    run_date TEXT PRIMARY KEY,
                    target_date TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                """
            )

    def save_plan(self, plan: DailyPlan) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_plans (
                    run_date, target_date, enabled, start_time, end_time, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_date) DO UPDATE SET
                    target_date=excluded.target_date,
                    enabled=excluded.enabled,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    plan.run_date.isoformat(),
                    plan.target_date.isoformat(),
                    int(plan.enabled),
                    plan.start_time,
                    plan.end_time,
                    plan.status.value,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )

    def get_plan(self, run_date: date) -> DailyPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_plans WHERE run_date = ?", (run_date.isoformat(),)
            ).fetchone()
        if row is None:
            return None
        return DailyPlan(
            run_date=date.fromisoformat(row["run_date"]),
            target_date=date.fromisoformat(row["target_date"]),
            enabled=bool(row["enabled"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            status=PlanStatus(row["status"]),
        )

    def update_status(self, run_date: date, status: PlanStatus) -> None:
        plan = self.get_plan(run_date)
        if plan is None:
            raise KeyError(f"没有 {run_date.isoformat()} 的预约计划")
        self.save_plan(
            DailyPlan(
                run_date=plan.run_date,
                target_date=plan.target_date,
                enabled=plan.enabled,
                start_time=plan.start_time,
                end_time=plan.end_time,
                status=status,
            )
        )

    def add_event(self, level: str, event_type: str, message: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s: %s", event_type, message)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events (created_at, level, event_type, message) VALUES (?, ?, ?, ?)",
                (
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    level,
                    event_type,
                    message,
                ),
            )

    def recent_events(self, limit: int = 20) -> list[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT created_at, level, event_type, message FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Event(
                created_at=datetime.fromisoformat(row["created_at"]),
                level=row["level"],
                event_type=row["event_type"],
                message=row["message"],
            )
            for row in rows
        ]

    def purge_old_events(self, days: int = 3, *, now: datetime | None = None) -> int:
        if days < 0:
            raise ValueError("days must be >= 0")
        cutoff = (now or datetime.now().astimezone()) - timedelta(days=days)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM events WHERE created_at <= ?",
                (cutoff.isoformat(timespec="seconds"),),
            )
            return cursor.rowcount

