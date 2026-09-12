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
from .calibration import calibrate
from .config import AppConfig, load_config
from .dashboard import dashboard_url, serve_dashboard
from .models import DailyPlan, PlanStatus
from .notifier import BarkNotifier, ConsoleNotifier
from .secrets import SecretStore
from .store import StateStore


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


def _ensure_tomorrow_plan(config: AppConfig, store: StateStore) -> DailyPlan:
    run_date = datetime.now(config.tz).date() + timedelta(days=1)
    existing = store.get_plan(run_date)
    if existing:
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
) -> None:
    plan = _ensure_tomorrow_plan(config, store)
    if reminder and plan.status == PlanStatus.CONFIRMED:
        store.add_event("info", "reminder_skipped", "预约计划已确认，不再提醒")
        return
    if "YOUR-" in config.dashboard.public_url:
        raise ValueError("请先在 booking.json 中填写 dashboard.public_url")
    title = "请确认明早是否预约" if not reminder else "预约计划尚未确认"
    message = (
        f"计划预约 {plan.target_date.isoformat()}，"
        f"当前时间 {plan.start_time}-{plan.end_time}。点击后可修改。"
    )
    notifier.send(
        title,
        message,
        url=dashboard_url(config, secrets),
        url_title="设置预约计划",
    )
    store.add_event("info", "prompt_sent", title)


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
            serve_dashboard(config, store, secrets)
        elif args.command == "prompt":
            _send_prompt(config, store, secrets, notifier, args.reminder)
            print("提醒已发送。")
        elif args.command == "run":
            run_date = args.run_date or datetime.now(config.tz).date()
            plan = store.get_plan(run_date)
            if plan is None:
                store.add_event("info", "no_plan", f"{run_date.isoformat()} 没有预约计划")
                print(f"{run_date.isoformat()} 没有预约计划，程序结束。")
                return
            asyncio.run(
                run_booking(
                    config,
                    store,
                    secrets,
                    notifier,
                    plan,
                    root / "artifacts" / run_date.isoformat(),
                    wait_for_open=not args.now,
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
        else:
            parser.error(f"未知命令：{args.command}")
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
