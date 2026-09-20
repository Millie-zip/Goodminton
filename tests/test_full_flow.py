"""北航羽毛球预约真实网页全流程测试。

校准页面：
    python tests/test_full_flow.py --calibrate

真实提交测试：
    python tests/test_full_flow.py --allow-submit --start-time 20:00 --end-time 21:00
    python tests/test_full_flow.py --allow-submit --start-time 7:00 --end-time 8:00
测试会连接真实北航网页并可能创建订单。场地、同伴、预约须知和蓝色可点击的
提交按钮均验证通过后，脚本会自动点击提交。统一身份认证由脚本自动填写并验证；
验证码由本地 OCR 自动识别和点击。进入支付页后不会付款，脚本会等待用户确认后
再关闭浏览器。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from buaa_booking.automation import (  # noqa: E402
    BookingAutomation,
    BookingError,
    CalibrationRequired,
    LocatedCandidate,
)
from buaa_booking.calibration import calibrate  # noqa: E402
from buaa_booking.captcha import LocalClickCaptchaSolver  # noqa: E402
from buaa_booking.config import AppConfig, load_config  # noqa: E402
from buaa_booking.models import DailyPlan, PlanStatus, validate_time_window  # noqa: E402
from buaa_booking.notifier import BarkNotifier, ConsoleNotifier  # noqa: E402
from buaa_booking.secrets import SecretStore  # noqa: E402
from buaa_booking.store import StateStore  # noqa: E402


TEST_DAYS_AHEAD = 2
AUTO_LOGIN_TIMEOUT_SECONDS = 30


class InteractiveLiveAutomation(BookingAutomation):
    async def _ensure_logged_in(self, page) -> None:
        await page.goto(self.config.home_url, wait_until="domcontentloaded")
        if await self._any_visible(page, "logged_in_markers"):
            self.store.add_event("info", "auto_login_verified", "已有登录会话验证通过")
            return

        username_input = await self._first_visible(page, "login_username")
        password_input = await self._first_visible(page, "login_password")
        submit = await self._first_visible(page, "login_submit")
        if username_input is None or password_input is None or submit is None:
            raise CalibrationRequired("统一身份认证页面选择器未匹配，无法自动登录")

        username = self.secrets.get("buaa_username", required=True) or ""
        password = self.secrets.get("buaa_password", required=True) or ""
        await username_input.fill(username)
        await password_input.fill(password)
        await submit.click()

        deadline = asyncio.get_running_loop().time() + AUTO_LOGIN_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await self._any_visible(page, "logged_in_markers"):
                self.store.add_event(
                    "info",
                    "auto_login_verified",
                    "统一身份认证自动登录并验证通过",
                )
                return
            await asyncio.sleep(0.5)
        raise BookingError("统一身份认证已提交，但 30 秒内未验证登录成功")

    async def _confirm_submit(
        self,
        candidate: LocatedCandidate,
        plan: DailyPlan,
    ) -> None:
        print("\n场地、同伴、预约须知和蓝色提交按钮均已验证：")
        print(f"  场馆：{candidate.slot.venue_id}")
        print(f"  日期：{plan.target_date.isoformat()}")
        print(f"  时间：{plan.start_time}-{plan.end_time}")
        print(f"  页面候选项：{candidate.slot.label}")
        print("现在自动点击提交按钮，并进入验证码阶段。")

    async def _wait_for_manual_captcha(self, page, plan, slot) -> None:
        del page, plan, slot
        raise BookingError("自动验证码未完成；测试按要求停止，不回退人工处理")

    async def _on_payment(self, page, plan, slot) -> None:
        await super()._on_payment(page, plan, slot)
        print("\n已检测到支付页。测试到此结束，程序不会点击付款按钮。")
        await asyncio.to_thread(input, "检查完支付页后按 Enter 关闭测试浏览器：")

    async def _on_failure(self, page, error: Exception) -> None:
        try:
            await page.bring_to_front()
        except Exception:
            pass
        print("\n测试失败，已保留 failure.png 对应的网页供检查。")
        print(f"  失败原因：{error}")
        print(f"  截图目录：{self.artifacts_dir}（含 failure-38.png、failure-39.png）")
        await asyncio.to_thread(input, "检查完失败页面后按 Enter 关闭测试浏览器：")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用真实北航网页测试预约流程；进入支付页后停止",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "booking.json",
        help="真实配置文件路径",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="只采集真实页面结构，不提交订单",
    )
    parser.add_argument(
        "--allow-submit",
        action="store_true",
        help="允许测试在完成场地、同伴、预约须知和蓝色按钮校验后点击真实提交按钮",
    )
    parser.add_argument(
        "--run-date",
        type=date.fromisoformat,
        help="流程运行日期，默认今天，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=TEST_DAYS_AHEAD,
        help="预约目标日期相对运行日期的提前天数，默认 2；用来对准明天/后天等空场窗口",
    )
    parser.add_argument("--start-time", help="预约开始时间，格式 HH:MM")
    parser.add_argument("--end-time", help="预约结束时间，格式 HH:MM")
    parser.add_argument(
        "--captcha-image-selector",
        default="#captcha-image",
        help="验证码图片元素 selector",
    )
    parser.add_argument(
        "--captcha-prompt-selector",
        default="#captcha-prompt",
        help="“请依次点击”提示元素 selector",
    )
    parser.add_argument(
        "--captcha-success-selector",
        default="#captcha-success",
        help="验证码通过后出现的元素 selector",
    )
    return parser


def prepare_live_config(config: AppConfig) -> AppConfig:
    return replace(
        config,
        headed_browser=True,
        browser=replace(
            config.browser,
            captcha_mode="auto",
            payment_hold_seconds=0,
        ),
    )


def build_notifier(config: AppConfig, secrets: SecretStore):
    if config.notification.provider.lower() == "bark":
        return BarkNotifier(config.notification, secrets)
    if config.notification.provider.lower() == "console":
        return ConsoleNotifier()
    raise ValueError(f"不支持的通知方式：{config.notification.provider}")


def verify_required_secrets(config: AppConfig, secrets: SecretStore) -> None:
    # 持久浏览器可能已有有效登录会话；账号密码仅在确实进入登录页时读取。
    del config, secrets


async def run_live_test(args: argparse.Namespace) -> None:
    config_path = args.config.resolve()
    config = load_config(config_path)

    if args.calibrate:
        report = await calibrate(config, PROJECT_ROOT / "artifacts")
        print(f"页面结构已保存：{report}")
        return

    if not args.allow_submit:
        raise SystemExit(
            "真实流程可能创建订单。确认接受后请添加 --allow-submit；"
            "脚本会在完成场地、同伴、预约须知和蓝色按钮校验后自动提交。"
        )

    start_time = args.start_time or config.default_start_time
    end_time = args.end_time or config.default_end_time
    validate_time_window(start_time, end_time)
    run_date = args.run_date or datetime.now(config.tz).date()
    if args.days_ahead < 1:
        raise ValueError("--days-ahead 必须大于等于 1")
    if run_date < datetime.now(config.tz).date():
        raise ValueError("真实即时测试的 --run-date 不能早于今天")

    config = prepare_live_config(config)
    secrets = SecretStore()
    verify_required_secrets(config, secrets)
    notifier = build_notifier(config, secrets)

    # 使用独立数据库，避免改写正式定时任务读取的 data/state.sqlite3。
    store = StateStore(PROJECT_ROOT / "data" / "live-test-state.sqlite3")
    plan = replace(
        DailyPlan.for_run_date(
            run_date,
            args.days_ahead,
            start_time,
            end_time,
            enabled=True,
        ),
        status=PlanStatus.CONFIRMED,
    )
    store.save_plan(plan)

    print("真实测试参数：")
    print(f"  预约日期：{plan.target_date.isoformat()}")
    print(f"  时间：{plan.start_time}-{plan.end_time}")
    print(f"  场馆顺序：{' -> '.join(config.venue_priority)}")
    print("  登录：自动提交统一身份认证并验证登录状态")
    print("  验证码：本地 OCR 自动识别并点击，失败后停止")
    print("  支付：进入支付页后停止，不付款")

    captcha_solver = LocalClickCaptchaSolver(
        image_selector=args.captcha_image_selector,
        prompt_selector=args.captcha_prompt_selector,
        success_selector=args.captcha_success_selector,
    )

    automation = InteractiveLiveAutomation(
        config,
        store,
        secrets,
        notifier,
        PROJECT_ROOT / "artifacts" / f"live-test-{run_date.isoformat()}",
        captcha_solver=captcha_solver,
    )
    try:
        await automation.run(plan, wait_for_open=False)
    except Exception as exc:
        store.update_status(plan.run_date, PlanStatus.FAILED)
        store.add_event("error", "live_test_failed", str(exc))
        raise

    saved = store.get_plan(plan.run_date)
    if saved is None or saved.status != PlanStatus.PAYMENT:
        raise BookingError("测试结束，但没有确认进入支付页")
    print("真实网页全流程验证通过：订单已到支付页，未执行付款。")


def main() -> int:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_live_test(args))
    except KeyboardInterrupt:
        print("\n测试已由用户取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"测试失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
