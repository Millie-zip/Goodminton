from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .automation import run_booking
from .captcha import LocalClickCaptchaSolver
from .calibration import calibrate
from .config import AppConfig, load_config
from .dashboard import dashboard_url, serve_dashboard, wait_for_dashboard
from .models import DailyPlan, PlanStatus
from .notifier import BarkNotifier, ConsoleNotifier
from .secrets import SecretStore
from .store import StateStore
from .tasks import set_reminder_enabled, sync_reminder_task


def project_root() -> Path:
    return Path.cwd().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="北航羽毛球预约助手")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/booking.json"),
        help="配置文件路径",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="创建本地配置文件")
    init_parser.add_argument("--force", action="store_true", help="覆盖已有配置")

    subparsers.add_parser("setup-secrets", help="把账号和通知密钥保存到系统凭据库")
    subparsers.add_parser("serve", help="启动手机设置页")

    prompt_parser = subparsers.add_parser("prompt", help="提醒设置明早预约计划")
    prompt_parser.add_argument("--reminder", action="store_true", help="发送未确认提醒")

    run_parser = subparsers.add_parser("run", help="执行当天预约计划")
    run_parser.add_argument("--run-date", type=date.fromisoformat, help="YYYY-MM-DD")
    run_parser.add_argument("--now", action="store_true", help="不等待 open_time，立即执行")

    subparsers.add_parser("calibrate", help="采集登录后页面结构，不提交订单")
    subparsers.add_parser("show", help="显示当天和明天的计划及最近事件")
    subparsers.add_parser("test-notification", help="发送一条普通测试通知")
    subparsers.add_parser("sync-reminder", help="按当前计划启用或停用提醒任务")
    return parser


def _load_runtime(config_path: Path) -> tuple[AppConfig, StateStore, SecretStore, Any]:
    config = load_config(config_path)
    root = config_path.resolve().parent.parent
    store = StateStore(root / "data" / "state.sqlite3")
    secrets = SecretStore()
    if config.notification.provider.lower() == "bark":
        notifier: Any = BarkNotifier(config.notification, secrets)
    elif config.notification.provider.lower() == "console":
        notifier = ConsoleNotifier()
    else:
        raise ValueError(f"不支持的通知方式：{config.notification.provider}")
    return config, store, secrets, notifier


def _build_captcha_solver(config: AppConfig) -> LocalClickCaptchaSolver | None:
    if config.browser.captcha_mode != "auto":
        return None
    return LocalClickCaptchaSolver()


def _init_config(destination: Path, force: bool) -> None:
    root = project_root()
    source = root / "config" / "booking.example.json"
    destination = destination.resolve()
    if not source.exists():
        raise FileNotFoundError(f"示例配置不存在：{source}")
    if destination.exists() and not force:
        raise FileExistsError(f"配置已存在：{destination}；如需覆盖，请加 --force")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    print(f"已创建配置：{destination}")


def _effective_run_date_for_booking_window(now: datetime, open_time: str) -> date:
    del open_time
    return now.date()


def _resolve_or_create_plan_for_booking_window(
    config: AppConfig,
    store: StateStore,
    now: datetime,
) -> DailyPlan:
    run_date = _effective_run_date_for_booking_window(now, config.open_time)
    expected_plan = DailyPlan.for_run_date(
        run_date,
        config.days_ahead,
        config.default_start_time,
        config.default_end_time,
    )
    existing = store.get_plan(run_date)
    if existing is not None:
        if (
            existing.run_date == expected_plan.run_date
            and existing.target_date == expected_plan.target_date
            and existing.start_time == expected_plan.start_time
            and existing.end_time == expected_plan.end_time
        ):
            return existing
        store.save_plan(expected_plan)
        return expected_plan

    store.save_plan(expected_plan)
    return expected_plan


def _ensure_prompt_plan(
    config: AppConfig,
    store: StateStore,
    *,
    reminder: bool,
    now: datetime | None = None,
) -> DailyPlan | None:
    current = now or datetime.now(config.tz)
    if not reminder:
        run_date = current.date() + timedelta(days=1)
    elif current.hour >= 22 and (current.hour > 22 or current.minute >= 30):
        run_date = current.date() + timedelta(days=1)
    elif current.hour < 2:
        run_date = current.date()
    else:
        return None

    existing = store.get_plan(run_date)
    if existing is not None:
        return existing
    plan = DailyPlan.for_run_date(
        run_date,
        config.days_ahead,
        config.default_start_time,
        config.default_end_time,
    )
    store.save_plan(plan)
    return plan


def _send_prompt(
    config: AppConfig,
    store: StateStore,
    secrets: SecretStore,
    notifier: Any,
    reminder: bool,
) -> bool:
    plan = _ensure_prompt_plan(config, store, reminder=reminder)
    if plan is None:
        store.add_event("info", "reminder_skipped", "已超过凌晨 02:00 提醒截止时间")
        return False
    if not reminder:
        set_reminder_enabled(plan.status != PlanStatus.CONFIRMED, store)
        # 手机可能在启用任务的同时提交反馈，再读一次已保存的计划。
        plan = store.get_plan(plan.run_date) or plan
        if plan.status == PlanStatus.CONFIRMED:
            set_reminder_enabled(False, store)
    if reminder and plan.status == PlanStatus.CONFIRMED:
        set_reminder_enabled(False, store)
        store.add_event("info", "reminder_skipped", "预约计划已确认，不再提醒")
        return False
    if "YOUR-" in config.dashboard.public_url:
        raise ValueError("请先在 booking.json 中填写 dashboard.public_url")
    if not wait_for_dashboard(config):
        title = "手机设置页未启动"
        message = "电脑上的预约设置页未能启动，本次没有发送失效链接。请检查电脑任务。"
        store.add_event("error", "dashboard_unavailable", message)
        notifier.send(title, message, silent=True)
        return False
    title = "请确认明早是否预约" if not reminder else "预约计划尚未确认"
    message = (
        f"计划预约 {plan.target_date.isoformat()}，"
        f"当前时间 {plan.start_time}-{plan.end_time}。点击后可修改。"
    )
    notifier.send(
        title,
        message,
        url=dashboard_url(config, secrets, plan.run_date),
        url_title="设置预约计划",
    )
    store.add_event("info", "prompt_sent", title)
    return True


def _show(config: AppConfig, store: StateStore) -> None:
    today = datetime.now(config.tz).date()
    for run_date in (today, today + timedelta(days=1)):
        plan = store.get_plan(run_date)
        if plan is None:
            print(f"{run_date.isoformat()}: 无计划")
        else:
            print(
                json.dumps(
                    {
                        "run_date": plan.run_date.isoformat(),
                        "target_date": plan.target_date.isoformat(),
                        "enabled": plan.enabled,
                        "time": f"{plan.start_time}-{plan.end_time}",
                        "status": plan.status.value,
                    },
                    ensure_ascii=False,
                )
            )
    for event in store.recent_events(10):
        print(
            f"{event.created_at.isoformat(timespec='seconds')} "
            f"[{event.level}] {event.event_type}: {event.message}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _init_config(args.config, args.force)
            return

        config, store, secrets, notifier = _load_runtime(args.config)
        root = args.config.resolve().parent.parent

        if args.command == "setup-secrets":
            secrets.interactive_setup(config.notification.provider)
            print("账号和通知密钥已保存到系统凭据库。")
        elif args.command == "serve":
            serve_dashboard(config, store, secrets, root / "artifacts")
        elif args.command == "prompt":
            sent = _send_prompt(config, store, secrets, notifier, args.reminder)
            print("提醒已发送。" if sent else "本次未发送设置链接，请运行 show 查看记录。")
        elif args.command == "run":
            run_date = args.run_date or _effective_run_date_for_booking_window(
                datetime.now(config.tz),
                config.open_time,
            )
            plan = store.get_plan(run_date)
            if plan is None:
                plan = _resolve_or_create_plan_for_booking_window(
                    config,
                    store,
                    datetime.now(config.tz),
                )
            run_date = plan.run_date
            if plan is None:
                message = f"{run_date.isoformat()} 没有预约计划，程序已结束。"
                store.add_event("info", "no_plan", message)
                try:
                    notifier.send("今日预约未执行", message, silent=True)
                except Exception as exc:
                    store.add_event(
                        "warning", "notification_failed", f"今日预约未执行：{exc}"
                    )
                print(f"{run_date.isoformat()} 没有预约计划，程序结束。")
                return
            captcha_solver = _build_captcha_solver(config)
            asyncio.run(
                run_booking(
                    config,
                    store,
                    secrets,
                    notifier,
                    plan,
                    root / "artifacts" / run_date.isoformat(),
                    wait_for_open=not args.now,
                    captcha_solver=captcha_solver,
                )
            )
        elif args.command == "calibrate":
            report_path = asyncio.run(calibrate(config, root / "artifacts"))
            print(f"页面结构已保存：{report_path}")
        elif args.command == "show":
            _show(config, store)
        elif args.command == "test-notification":
            notifier.send("预约助手测试", "手机通知连接正常。")
            print("测试通知已发送。")
        elif args.command == "sync-reminder":
            if not sync_reminder_task(store, datetime.now(config.tz)):
                raise RuntimeError("提醒任务状态同步失败，请运行 show 查看记录")
        else:
            parser.error(f"未知命令：{args.command}")
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
