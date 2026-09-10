from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import DailyPlan, PlanStatus, SlotCandidate, parse_clock
from .secrets import SecretStore
from .store import StateStore


class BookingError(RuntimeError):
    pass


class CalibrationRequired(BookingError):
    pass


@dataclass(slots=True)
class LocatedCandidate:
    slot: SlotCandidate
    page: Any
    selector: str


def _clean_text(value: str) -> str:
    value = value.replace("：", ":").replace("—", "-").replace("–", "-")
    value = value.replace("至", "-").replace("~", "-")
    return re.sub(r"\s+", "", value)


def _slot_matches(text: str, plan: DailyPlan) -> bool:
    cleaned = _clean_text(text)
    return _clean_text(plan.start_time) in cleaned and _clean_text(plan.end_time) in cleaned


def _date_labels(plan: DailyPlan) -> tuple[str, ...]:
    value = plan.target_date
    return (
        value.isoformat(),
        value.strftime("%m-%d"),
        f"{value.month}月{value.day}日",
        f"{value.month}/{value.day}",
    )


class BookingAutomation:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        secrets: SecretStore,
        notifier: Any,
        artifacts_dir: Path,
    ) -> None:
        self.config = config
        self.store = store
        self.secrets = secrets
        self.notifier = notifier
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    async def run(self, plan: DailyPlan, *, wait_for_open: bool = True) -> None:
        if not plan.enabled:
            self.store.update_status(plan.run_date, PlanStatus.SKIPPED)
            self.store.add_event("info", "booking_skipped", "当天计划未启用")
            return
        if not self.config.selectors.get("slot_candidates"):
            raise CalibrationRequired(
                "slot_candidates 尚未配置。请先运行 calibrate，并根据页面结构填写选择器"
            )

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BookingError("缺少 Playwright，请先安装依赖和 Chromium") from exc

        self.store.update_status(plan.run_date, PlanStatus.RUNNING)
        self.store.add_event(
            "info",
            "booking_started",
            f"开始处理 {plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}",
        )
        self.config.browser.profile_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                self.config.browser.profile_dir,
                headless=not self.config.headed_browser,
                no_viewport=True,
                locale="zh-CN",
                timezone_id=self.config.timezone,
            )
            context.set_default_timeout(self.config.browser.navigation_timeout_ms)
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await self._ensure_logged_in(page)
                venue_pages = await self._open_venue_pages(context, page)
                if wait_for_open:
                    await self._wait_until_open()
                    await asyncio.gather(
                        *(venue_page.reload(wait_until="domcontentloaded") for venue_page in venue_pages.values())
                    )
                await asyncio.gather(
                    *(self._select_target_date(venue_page, plan) for venue_page in venue_pages.values())
                )
                candidates = await self._load_candidates_with_refresh(venue_pages, plan)
                if not candidates:
                    raise BookingError("两个场馆都没有找到符合时间的可预约场地")

                if self.config.dry_run:
                    await self._save_screenshot(candidates[0].page, "dry-run-candidates.png")
                    self.store.update_status(plan.run_date, PlanStatus.DRY_RUN)
                    self.store.add_event(
                        "info", "dry_run", f"试运行找到 {len(candidates)} 个候选场地，未点击"
                    )
                    self.notifier.send(
                        "预约试运行完成",
                        f"找到 {len(candidates)} 个候选场地；dry_run=true，没有提交。",
                    )
                    return

                await self._attempt_candidates(candidates, plan)
            except Exception:
                await self._save_screenshot(page, "failure.png")
                raise
            finally:
                await context.close()

    async def _ensure_logged_in(self, page: Any) -> None:
        await page.goto(self.config.home_url, wait_until="domcontentloaded")
        if await self._any_visible(page, "logged_in_markers"):
            return

        username_input = await self._first_visible(page, "login_username")
        password_input = await self._first_visible(page, "login_password")
        if username_input is not None and password_input is not None:
            username = self.secrets.get("buaa_username", required=True) or ""
            password = self.secrets.get("buaa_password", required=True) or ""
            await username_input.fill(username)
            await password_input.fill(password)
            submit = await self._first_visible(page, "login_submit")
            if submit is None:
                raise CalibrationRequired("登录按钮选择器未匹配")
            await submit.click()
            await page.wait_for_timeout(1500)

        if await self._any_visible(page, "logged_in_markers"):
            return
        self.notifier.send(
            "需要完成登录",
            "统一身份认证没有自动通过，请远程打开电脑完成登录。",
            emergency=True,
            url=self.config.notification.remote_control_url,
            url_title="远程打开电脑",
        )
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            if await self._any_visible(page, "logged_in_markers"):
                return
            await asyncio.sleep(1)
        raise BookingError("等待人工登录超时")

    async def _open_venue_pages(self, context: Any, first_page: Any) -> dict[str, Any]:
        pages: dict[str, Any] = {}
        for index, venue_id in enumerate(self.config.venue_priority):
            page = first_page if index == 0 else await context.new_page()
            await page.goto(self.config.venue_urls[venue_id], wait_until="domcontentloaded")
            pages[venue_id] = page
        return pages

    async def _select_target_date(self, page: Any, plan: DailyPlan) -> None:
        labels = tuple(_clean_text(item) for item in _date_labels(plan))
        for selector in self.config.selectors.get("date_buttons", ()):
            locator = page.locator(selector)
            count = min(await locator.count(), 100)
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                text = _clean_text(await item.inner_text())
                if any(label in text for label in labels):
                    await item.click()
                    await page.wait_for_timeout(300)
                    return
        body_text = _clean_text(await page.locator("body").inner_text())
        if any(label in body_text for label in labels):
            return
        raise CalibrationRequired(
            f"没有找到预约日期 {plan.target_date.isoformat()}，需要校 date_buttons"
        )

    async def _load_candidates_with_refresh(
        self, venue_pages: dict[str, Any], plan: DailyPlan
    ) -> list[LocatedCandidate]:
        for attempt in range(self.config.browser.availability_refresh_count):
            candidates = await self._collect_candidates(venue_pages, plan)
            if candidates:
                return candidates
            if attempt + 1 < self.config.browser.availability_refresh_count:
                await asyncio.sleep(self.config.browser.availability_refresh_interval_seconds)
                await asyncio.gather(*(page.reload(wait_until="domcontentloaded") for page in venue_pages.values()))
                await asyncio.gather(
                    *(self._select_target_date(page, plan) for page in venue_pages.values())
                )
        return []

    async def _collect_candidates(
        self, venue_pages: dict[str, Any], plan: DailyPlan
    ) -> list[LocatedCandidate]:
        tasks = [
            self._collect_page_candidates(venue_id, venue_pages[venue_id], plan)
            for venue_id in self.config.venue_priority
        ]
        results = await asyncio.gather(*tasks)
        return [candidate for group in results for candidate in group]

    async def _collect_page_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> list[LocatedCandidate]:
        blocked_words = ("已约", "占用", "锁定", "不可", "售罄", "结束")
        candidates: list[LocatedCandidate] = []
        for selector_template in self.config.selectors.get("slot_candidates", ()):
            selector = selector_template.format(
                start=plan.start_time,
                end=plan.end_time,
                target_date=plan.target_date.isoformat(),
            )
            locator = page.locator(selector)
            count = min(await locator.count(), 200)
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible() or not await item.is_enabled():
                    continue
                label = (await item.inner_text()).strip()
                if not _slot_matches(label, plan) or any(word in label for word in blocked_words):
                    continue
                candidates.append(
                    LocatedCandidate(
                        slot=SlotCandidate(venue_id=venue_id, dom_index=index, label=label),
                        page=page,
                        selector=selector,
                    )
                )
        return candidates

    async def _attempt_candidates(
        self, candidates: list[LocatedCandidate], plan: DailyPlan
    ) -> None:
        for candidate in candidates:
            item = candidate.page.locator(candidate.selector).nth(candidate.slot.dom_index)
            if not await item.is_visible() or not await item.is_enabled():
                continue
            await item.click()
            await candidate.page.wait_for_timeout(250)
            await self._select_first_companion(candidate.page)
            await self._accept_terms(candidate.page)
            await self._save_screenshot(candidate.page, "before-submit.png")
            submit = await self._first_visible(candidate.page, "submit_button")
            if submit is None:
                raise CalibrationRequired("没有找到提交预约按钮")
            await submit.click()
            await candidate.page.wait_for_timeout(1000)

            if await self._any_visible(candidate.page, "occupied_markers"):
                self.store.add_event(
                    "warning",
                    "candidate_occupied",
                    f"场馆 {candidate.slot.venue_id} 的候选场地已被占用，尝试下一个",
                )
                await candidate.page.goto(
                    self.config.venue_urls[candidate.slot.venue_id],
                    wait_until="domcontentloaded",
                )
                await self._select_target_date(candidate.page, plan)
                continue
            if await self._any_visible(candidate.page, "captcha_markers"):
                await self._wait_for_manual_captcha(candidate.page, plan, candidate.slot)
                return
            if await self._is_payment_page(candidate.page):
                await self._on_payment(candidate.page, plan, candidate.slot)
                return
            raise BookingError("提交后的页面状态无法确认。为避免重复订单，程序已停止")
        raise BookingError("所有候选场地都已被占用")

    async def _select_first_companion(self, page: Any) -> None:
        opener = await self._first_visible(page, "companion_open")
        if opener is not None:
            await opener.click()
            await page.wait_for_timeout(200)
        for selector in self.config.selectors.get("companion_options", ()):
            locator = page.locator(selector)
            count = min(await locator.count(), 50)
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible() or not await item.is_enabled():
                    continue
                text = (await item.inner_text()).strip()
                if not text or any(word in text for word in ("请选择", "添加", "取消")):
                    continue
                await item.click()
                return
        raise CalibrationRequired("没有找到可选择的同伴")

    async def _accept_terms(self, page: Any) -> None:
        checkbox = await self._first_visible(page, "agreement_checkbox")
        if checkbox is None:
            raise CalibrationRequired("没有找到预约须知复选框")
        try:
            if await checkbox.is_checked():
                return
            await checkbox.check()
        except Exception:
            await checkbox.click()

    async def _wait_for_manual_captcha(
        self, page: Any, plan: DailyPlan, slot: SlotCandidate
    ) -> None:
        self.store.update_status(plan.run_date, PlanStatus.CAPTCHA)
        self.store.add_event(
            "info", "captcha_wait", f"场馆 {slot.venue_id} 已到验证码，请人工点选"
        )
        self.notifier.send(
            "场地已选，请完成验证码",
            f"{plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}，请立即远程点选验证码。",
            emergency=True,
            url=self.config.notification.remote_control_url,
            url_title="远程打开电脑",
        )
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.browser.manual_captcha_timeout_seconds
        )
        while asyncio.get_running_loop().time() < deadline:
            if await self._is_payment_page(page):
                await self._on_payment(page, plan, slot)
                return
            await asyncio.sleep(1)
        raise BookingError("等待人工验证码超时")

    async def _is_payment_page(self, page: Any) -> bool:
        if await self._any_visible(page, "payment_markers"):
            return True
        url = page.url.lower()
        return "payment" in url or re.search(r"(^|[/_-])pay([/?#_-]|$)", url) is not None

    async def _on_payment(self, page: Any, plan: DailyPlan, slot: SlotCandidate) -> None:
        self.store.update_status(plan.run_date, PlanStatus.PAYMENT)
        self.store.add_event(
            "info", "payment_ready", f"场馆 {slot.venue_id} 已进入支付页面"
        )
        self.notifier.send(
            "场地已抢到，请付款",
            f"{plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}，场馆 {slot.venue_id}。",
            emergency=True,
            url=self.config.notification.remote_control_url,
            url_title="打开支付页面",
        )
        await self._save_screenshot(page, "payment-ready.png")
        await asyncio.sleep(self.config.browser.payment_hold_seconds)

    async def _first_visible(self, page: Any, selector_group: str) -> Any | None:
        for selector in self.config.selectors.get(selector_group, ()):
            locator = page.locator(selector)
            count = min(await locator.count(), 20)
            for index in range(count):
                item = locator.nth(index)
                if await item.is_visible():
                    return item
        return None

    async def _any_visible(self, page: Any, selector_group: str) -> bool:
        return await self._first_visible(page, selector_group) is not None

    async def _wait_until_open(self) -> None:
        open_clock = parse_clock(self.config.open_time)
        now = datetime.now(self.config.tz)
        open_at = datetime.combine(now.date(), open_clock, tzinfo=self.config.tz)
        if now >= open_at:
            return
        delay = (open_at - now).total_seconds()
        self.store.add_event("info", "waiting_open", f"等待到 {self.config.open_time}")
        await asyncio.sleep(delay)

    async def _save_screenshot(self, page: Any, name: str) -> None:
        try:
            await page.screenshot(path=self.artifacts_dir / name, full_page=True)
        except Exception:
            return


async def run_booking(
    config: AppConfig,
    store: StateStore,
    secrets: SecretStore,
    notifier: Any,
    plan: DailyPlan,
    artifacts_dir: Path,
    *,
    wait_for_open: bool = True,
) -> None:
    automation = BookingAutomation(config, store, secrets, notifier, artifacts_dir)
    try:
        await automation.run(plan, wait_for_open=wait_for_open)
    except Exception as exc:
        store.update_status(plan.run_date, PlanStatus.FAILED)
        store.add_event("error", "booking_failed", str(exc))
        try:
            notifier.send("预约程序停止", str(exc), emergency=True)
        except Exception:
            pass
        raise
