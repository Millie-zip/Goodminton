from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .captcha import (
    AutoCaptchaSolver,
    AutoCaptchaUnavailable,
    CaptchaChallenge,
    decode_clickword_response,
    EmptyAutoCaptchaSolver,
    extract_clickword_fields,
)
from .config import AppConfig
from .models import DailyPlan, PlanStatus, SlotCandidate, parse_clock
from .secrets import SecretStore
from .store import StateStore


SYSTEM_BROWSER_EXECUTABLES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

DATE_BUTTON_FALLBACK_SELECTORS = (
    "[data-date]",
    "[data-day]",
    ".date-item",
    "[class*='date']",
    "[class*='day']",
    "[role='tab']",
    "button",
    "span",
)

MATRIX_CELL_FALLBACK_SELECTORS = (
    "td",
    "[role='gridcell']",
    "[class*='slot']",
    "[class*='cell']",
    "[class*='court']",
)

COMPANION_CANDIDATE_SELECTORS = (
    ".buddy-row:not(.addBtn)",
    "[class*='buddy-row']:not(.addBtn)",
    "[role='option']",
    ".companion-item",
    ".user-item",
    "[class*='companion']",
    "[class*='person']",
    "[class*='member']",
    "button",
    "[role='button']",
    "div",
    "span",
)

PAYMENT_BUTTON_FALLBACK_SELECTORS = (
    "div.btn:not(.cancel):has-text('支付（')",
    "div.btn:not(.cancel):has-text('支付(')",
    "div.btn:not(.cancel):has-text('支付')",
    "button:has-text('支付（')",
    "button:has-text('支付(')",
    "[role='button']:has-text('支付（')",
    "[role='button']:has-text('支付(')",
    "a:has-text('支付（')",
    "a:has-text('支付(')",
    "button:has-text('支付')",
    "[role='button']:has-text('支付')",
    "a:has-text('支付')",
    "button:has-text('付款')",
    "button:has-text('立即支付')",
    "button:has-text('去支付')",
    "a:has-text('付款')",
    "a:has-text('立即支付')",
    ".pay-button",
    ".payment-button",
)

PAYMENT_QR_FALLBACK_SELECTORS = (
    "img.payImage",
    "img[src*='qr']",
    "img[src*='QR']",
    "[class*='qr'] img",
    "[class*='qrcode'] img",
    "[class*='qr-code'] img",
    "[class*='qrcode'] canvas",
    "[class*='qr-code'] canvas",
)


class BookingError(RuntimeError):
    pass


class CalibrationRequired(BookingError):
    pass


class AuthenticationExpired(BookingError):
    pass


class BookingPageLoadError(BookingError):
    pass


AUTH_EXPIRED_JS = r"""[...document.querySelectorAll('.ivu-modal-wrap, .ivu-modal-confirm, [role="dialog"]')]
    .some(el => el.getBoundingClientRect().width > 0 &&
        /token.*(?:失效|过期)|(?:登录|会话).*(?:失效|过期)|请重新登录/i.test(el.textContent))"""


PAGE_DATA_ERROR_JS = r"""[...document.querySelectorAll('.ivu-modal-wrap, .ivu-modal-confirm, [role="dialog"]')]
    .some(el => el.getBoundingClientRect().width > 0 &&
        /返回数据格式不正确/.test(el.textContent))"""


async def launch_persistent_browser_context(
    playwright: Any,
    config: AppConfig,
    *,
    headless: bool,
) -> Any:
    """启动持久浏览器；Playwright 浏览器缺失时使用本机 Chrome/Edge。"""

    options: dict[str, Any] = {
        "headless": headless,
        "no_viewport": True,
        "locale": "zh-CN",
        "timezone_id": config.timezone,
    }
    bundled_path = Path(playwright.chromium.executable_path)
    system_browser = next(
        (path for path in SYSTEM_BROWSER_EXECUTABLES if path.is_file()),
        None,
    )
    if system_browser is not None:
        options["executable_path"] = str(system_browser)
    elif not bundled_path.is_file():
        raise BookingError(
            "找不到可用浏览器。请安装 Playwright Chromium、Google Chrome 或 Microsoft Edge"
        )

    return await playwright.chromium.launch_persistent_context(
        config.browser.profile_dir,
        **options,
    )


@dataclass(slots=True)
class LocatedCandidate:
    slot: SlotCandidate
    page: Any
    selector: str
    locator: Any | None = None
    selections: tuple[SlotSelection, ...] = ()


@dataclass(frozen=True, slots=True)
class SlotSelection:
    court: str
    time_label: str


def _clean_text(value: str) -> str:
    value = value.replace("：", ":").replace("—", "-").replace("–", "-")
    value = value.replace("至", "-").replace("~", "-")
    return re.sub(r"\s+", "", value)


def _slot_matches(text: str, plan: DailyPlan) -> bool:
    cleaned = _clean_text(text)
    return _clean_text(plan.start_time) in cleaned and _clean_text(plan.end_time) in cleaned


def _date_labels(plan: DailyPlan) -> tuple[str, ...]:
    value = plan.target_date
    labels = [
        value.isoformat(),
        value.strftime("%m-%d"),
        value.strftime("%m.%d"),
        f"{value.month}月{value.day}日",
        f"{value.month:02d}月{value.day:02d}日",
        f"{value.month}/{value.day}",
        f"{value.month}.{value.day}",
        f"周{'一二三四五六日'[value.weekday()]}",
        f"星期{'一二三四五六日'[value.weekday()]}",
    ]
    relative_days = (value - plan.run_date).days
    if 0 <= relative_days <= 2:
        labels.append(("今天", "明天", "后天")[relative_days])
    return tuple(labels)


class BookingAutomation:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        secrets: SecretStore,
        notifier: Any,
        artifacts_dir: Path,
        captcha_solver: AutoCaptchaSolver | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.secrets = secrets
        self.notifier = notifier
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.captcha_solver = captcha_solver or EmptyAutoCaptchaSolver()
        self._captcha_response_payload: Any = None
        self._captcha_image_bytes: bytes | None = None
        self._captcha_word_list: list[str] | None = None
        self._captcha_token: str | None = None
        self._captcha_secret_key: str | None = None
        self._post_submit_monitor: dict[str, Any] | None = None
        self.current_stage = "初始化"
        self._active_page: Any | None = None
        self._auth_recovery_used = False

    async def run(self, plan: DailyPlan, *, wait_for_open: bool = True) -> None:
        if not plan.enabled:
            self.store.update_status(plan.run_date, PlanStatus.SKIPPED)
            self.store.add_event("info", "booking_skipped", "当天计划未启用")
            self._notify(
                "今日预约未执行",
                f"{plan.run_date.isoformat()} 的计划未启用，程序已跳过。",
                silent=True,
            )
            return
        self._set_stage(
            "启动",
            f"准备预约 {plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}",
        )
        self._notify(
            "预约程序已启动",
            f"准备预约 {plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}。",
            silent=True,
        )
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BookingError("缺少 Playwright，请先安装依赖和 Chromium") from exc

        self.store.update_status(plan.run_date, PlanStatus.RUNNING)
        if self.config.selectors.get("slot_candidates"):
            self._set_stage("配置检查", "预约选择器已读取，准备打开浏览器")
        else:
            self._set_stage("配置检查", "使用预约表格的时间列和场地行解析器")
        self.config.browser.profile_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            context = await launch_persistent_browser_context(
                playwright,
                self.config,
                headless=not self.config.headed_browser,
            )
            context.set_default_timeout(self.config.browser.navigation_timeout_ms)
            page = context.pages[0] if context.pages else await context.new_page()
            self._active_page = page
            venue_pages: dict[str, Any] = {}
            try:
                self._set_stage("登录", "正在检查并恢复网站登录状态")
                await self._ensure_logged_in(page)
                self._set_stage("场馆校验", "首页已打开，正在检查场馆登录状态和预约数据")
                venue_pages = await self._open_venue_pages(context, page)
                await self._prepare_venue_pages(venue_pages)
                self._set_stage("场馆页面", "场馆 38、39 页面已打开")
                if wait_for_open:
                    self._set_stage("等待开放", f"等待 {self.config.open_time} 开放预约")
                    await self._wait_until_open()
                    await asyncio.gather(
                        *(venue_page.reload(wait_until="domcontentloaded") for venue_page in venue_pages.values())
                    )
                self._set_stage("选择日期", f"正在选择 {plan.target_date.isoformat()}")
                await self._prepare_venue_pages(venue_pages, plan)
                self._set_stage("选择日期", f"已选择 {plan.target_date.isoformat()}")
                candidates = await self._load_candidates_with_refresh(venue_pages, plan)
                if not candidates:
                    raise BookingError("两个场馆都没有找到符合时间的可预约场地")
                self._set_stage("检测场地", f"找到 {len(candidates)} 个候选场地")
                await self._attempt_candidates(candidates, plan, venue_pages)
            except Exception as exc:
                failure_page = self._active_page or page
                await self._save_screenshot(failure_page, "failure.png")
                await asyncio.gather(
                    *(
                        self._save_screenshot(venue_page, f"failure-{venue_id}.png")
                        for venue_id, venue_page in venue_pages.items()
                    )
                )
                await self._on_failure(failure_page, exc)
                raise
            finally:
                await context.close()

    async def _on_failure(self, page: Any, error: Exception) -> None:
        """在关闭浏览器前处理失败页面；正式任务默认不额外等待。"""

        del page, error

    async def _find_login_entry(self, page: Any) -> Any | None:
        for text in (
            "统一身份认证",
            "认证登录",
            "统一认证",
            "登录",
            "账号登录",
            "密码登录",
        ):
            try:
                locator = page.get_by_text(text, exact=False)
                count = min(await locator.count(), 20)
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible():
                        return item
            except Exception:
                continue

        for selector in ("a", "button", "span", "div", "[role='button']"):
            locator = page.locator(selector)
            count = min(await locator.count(), 200)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = re.sub(r"\s+", "", (await item.inner_text()).strip())
                    if not text:
                        continue
                    if "登录" in text or "认证" in text:
                        return item
                except Exception:
                    continue
        return None

    async def _has_booking_login_markers(self, page: Any) -> bool:
        # SSO 页也有“个人中心”链接，只有预约站点内的标记才代表已返回。
        return (urlsplit(page.url).netloc == urlsplit(self.config.home_url).netloc
                and await self._any_visible(page, "logged_in_markers"))

    async def _ensure_logged_in(self, page: Any) -> None:
        await page.goto(self.config.home_url, wait_until="domcontentloaded")
        if await page.evaluate(f"() => {AUTH_EXPIRED_JS}"):
            await self._renew_login(page)
            return
        if await self._has_booking_login_markers(page):
            return

        username_input = await self._first_visible(page, "login_username")
        password_input = await self._first_visible(page, "login_password")
        if username_input is None or password_input is None:
            # 首页含移出视口的“登录”菜单；使用目标站点的独立登录页。
            await page.goto(urljoin(self.config.home_url, "login"), wait_until="domcontentloaded")
            # 登录入口本身也由前端异步渲染；同时等待入口和 SSO 表单。
            deadline = asyncio.get_running_loop().time() + self.config.browser.navigation_timeout_ms / 1000
            entry_clicked = False
            while asyncio.get_running_loop().time() < deadline:
                if await self._has_booking_login_markers(page):
                    return
                username_input = await self._first_visible(page, "login_username")
                password_input = await self._first_visible(page, "login_password")
                if username_input is not None and password_input is not None:
                    break
                if not entry_clicked:
                    login_entry = await self._find_login_entry(page)
                    if login_entry is not None:
                        await login_entry.click()
                        entry_clicked = True
                await asyncio.sleep(0.05)

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

        if await self._has_booking_login_markers(page):
            return
        self._notify(
            "需要完成登录",
            "统一身份认证没有自动通过，请远程打开电脑完成登录。",
            silent=True,
            url=self.config.notification.remote_control_url,
            url_title="远程打开电脑",
        )
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            if await self._has_booking_login_markers(page):
                return
            await asyncio.sleep(1)
        raise BookingError("等待人工登录超时")

    async def _wait_for_authenticated_condition(self, page: Any, condition: str) -> None:
        result = await page.wait_for_function(
            f"() => {{ if ({AUTH_EXPIRED_JS}) return 'expired'; "
            f"if ({PAGE_DATA_ERROR_JS}) return 'data_error'; return ({condition})() ? 'ready' : false; }}",
            timeout=self.config.browser.navigation_timeout_ms, polling=50,
        )
        try:
            state = await result.json_value()
            if state == "expired":
                raise AuthenticationExpired("网站登录已过期（Token 失效），需要重新登录")
            if state == "data_error":
                raise BookingPageLoadError("网站提示：返回数据格式不正确，预约数据未加载")
        finally:
            await result.dispose()

    async def _prepare_venue_pages(self, pages: dict[str, Any], plan: DailyPlan | None = None) -> None:
        # 等所有并发读取结束后再重新登录，避免另一页仍在操作旧会话。
        data_retries = 0
        # 最多两次数据重载和一次登录恢复，最后一轮检查恢复结果。
        for _ in range(4):
            results = await asyncio.gather(*(
                self._select_target_date(page, plan) if plan is not None else self._wait_for_booking_table(page)
                for page in pages.values()
            ), return_exceptions=True)
            expired = next((page for page, result in zip(pages.values(), results)
                            if isinstance(result, AuthenticationExpired)), None)
            if expired is not None:
                await self._renew_login(expired)
                await asyncio.gather(*(page.goto(self.config.venue_urls[venue], wait_until="domcontentloaded")
                                       for venue, page in pages.items()))
                continue
            failed = []
            for (venue, page), result in zip(pages.items(), results):
                if isinstance(result, BookingPageLoadError):
                    failed.append((venue, page))
                elif isinstance(result, Exception):
                    raise BookingError(f"场馆 {venue} 页面校验失败：{result}") from result
                elif isinstance(result, BaseException):
                    raise result
            if not failed:
                return
            venues = "、".join(venue for venue, _ in failed)
            if data_retries >= 2:
                raise BookingPageLoadError(
                    f"场馆 {venues}：网站持续提示返回数据格式不正确，已重新加载 2 次，停止本次预约")
            data_retries += 1
            self._set_stage("场馆数据重试", f"场馆 {venues} 返回数据格式不正确，重新加载 {data_retries}/2", level="warning")
            await asyncio.sleep(0.3 * data_retries)
            await asyncio.gather(*(page.goto(self.config.venue_urls[venue], wait_until="domcontentloaded")
                                   for venue, page in failed))
        raise BookingPageLoadError("场馆页面恢复次数已用尽，停止本次预约")

    async def _renew_login(self, page: Any) -> None:
        if self._auth_recovery_used:
            raise AuthenticationExpired("重新登录后 Token 仍失效，请手动完成网站登录；本次停止预约")
        self._auth_recovery_used = True
        self._set_stage("恢复登录", "检测到 Token 过期，正在通过网站登录入口恢复会话", level="warning")
        await page.bring_to_front()
        modal = page.locator(".ivu-modal-wrap:visible").filter(
            has_text=re.compile(r"Token.*(?:失效|过期)|请重新登录", re.I)).first
        if await modal.count():
            await modal.get_by_role("button", name="确定", exact=True).click()
        # /venue/login 在旧令牌仍存在时会直接回首页；先走网页自身的退出流程。
        logout = await self._first_visible_with_fallback(page, "logout_button", (
            "text=/^退出$/", "text=/^退出登录$/",
        ))
        if logout is None:
            # 有些过期响应会直接清除会话并跳到登录页，此时已无需再次退出。
            if (await self._find_login_entry(page) is not None
                    or await self._first_visible(page, "login_username") is not None):
                await self._ensure_logged_in(page)
                return
            raise AuthenticationExpired("Token 已过期，但未找到网站退出入口，请手动重新登录")
        await logout.click()
        confirm = page.locator(".ivu-modal-wrap:visible").filter(has_text="是否退出本系统").first
        await confirm.get_by_role("button", name="确定", exact=True).click()
        # 等待站点清理旧登录状态，不能立即导航打断退出请求。
        await page.wait_for_function(r"""() => ![...document.querySelectorAll('a,span,div')].some(el =>
            el.getBoundingClientRect().width > 0 && /^(个人中心|退出|退出登录)$/.test(el.textContent.trim()))""",
            timeout=self.config.browser.navigation_timeout_ms, polling=50)
        await self._ensure_logged_in(page)

    async def _open_venue_pages(self, context: Any, first_page: Any) -> dict[str, Any]:
        pages: dict[str, Any] = {}
        for index, venue_id in enumerate(self.config.venue_priority):
            page = first_page if index == 0 else await context.new_page()
            await page.goto(self.config.venue_urls[venue_id], wait_until="domcontentloaded")
            pages[venue_id] = page
        return pages

    async def _select_target_date_legacy(self, page: Any, plan: DailyPlan) -> None:
        labels = tuple(_clean_text(item) for item in _date_labels(plan))
        selectors = tuple(
            dict.fromkeys(
                (*self.config.selectors.get("date_buttons", ()), *DATE_BUTTON_FALLBACK_SELECTORS)
            )
        )
        for selector in selectors:
            locator = page.locator(selector)
            try:
                texts = await locator.all_inner_texts()
            except Exception:
                continue
            for index, raw_text in enumerate(texts[:100]):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = _clean_text(raw_text)
                except Exception:
                    continue
                if not any(label in text for label in labels):
                    continue
                try:
                    async with page.expect_response(
                        lambda response: self._is_target_date_response(response, plan),
                        timeout=min(2_000, self.config.browser.navigation_timeout_ms),
                    ) as response_info:
                        await item.click()
                    response = await response_info.value
                except Exception:
                    continue
                if response.status != 200:
                    raise BookingError(
                        f"预约日期接口返回 HTTP {response.status}：{plan.target_date.isoformat()}"
                    )
                await page.wait_for_timeout(50)
                return
        raise CalibrationRequired(
            f"没有找到预约日期 {plan.target_date.isoformat()}，需要校 date_buttons"
        )

    async def _select_target_date_legacy_dynamic(self, page: Any, plan: DailyPlan) -> None:
        # 当前站点固定使用 .date_box > div > span 渲染日期。
        target_month_day = (plan.target_date.month, plan.target_date.day)
        target_label = f"{plan.target_date.month:02d}月{plan.target_date.day:02d}日"
        cards = page.locator("div.date_box > div")
        try:
            card_count = await cards.count()
        except Exception as exc:
            raise CalibrationRequired("日期控件 .date_box 未加载") from exc

        observed_dates: list[str] = []
        for index in range(card_count):
            card = cards.nth(index)
            try:
                if not await card.is_visible():
                    continue
                date_text = _clean_text(await card.inner_text())
                date_match = re.search(r"(\d{1,2})\D+(\d{1,2})", date_text)
                if date_match is None:
                    continue
                month_day = (int(date_match.group(1)), int(date_match.group(2)))
                observed_dates.append(
                    f"{month_day[0]:02d}月{month_day[1]:02d}日"
                )
                if month_day != target_month_day:
                    continue
                classes = (await card.get_attribute("class") or "").split()
                if "active" in classes:
                    await page.wait_for_timeout(50)
                    return

                await card.click()
                # 以 Vue 的 active 状态作为完成信号，不依赖响应监听时序。
                for _ in range(20):
                    classes = (await card.get_attribute("class") or "").split()
                    if "active" in classes:
                        await page.wait_for_timeout(50)
                        return
                    await page.wait_for_timeout(25)
            except Exception as exc:
                raise CalibrationRequired(
                    f"点击预约日期 {target_label} 失败"
                ) from exc

        raise CalibrationRequired(
            f"日期控件中没有目标日期 {target_label}，实际日期：{observed_dates}"
        )

    async def _select_target_date(self, page: Any, plan: DailyPlan) -> None:
        # 当前目标页面第三张卡片对应后天；不依赖加载期间会变化的 style 属性。
        target_label = f"{plan.target_date.month:02d}月{plan.target_date.day:02d}日"
        target_card = page.locator("div.date_box > div").nth(2)
        try:
            await self._wait_for_authenticated_condition(page,
                "() => document.querySelectorAll('div.date_box > div').length >= 3")
        except (AuthenticationExpired, BookingPageLoadError):
            raise
        except Exception as exc:
            raise CalibrationRequired("日期控件 order: 2 块未完成渲染") from exc

        phase = "等待初始预约表格加载"
        try:
            # 日期卡片先于初始场地数据出现；等初始请求结束后再切换日期，
            # 避免初始数据覆盖目标日期，或占满目标日期请求的等待时间。
            await self._wait_for_booking_table(page)
            date_text = _clean_text(await target_card.locator("span").first.inner_text())
            if date_text != target_label:
                raise CalibrationRequired(
                    f"order: 2 日期为 {date_text}，不是目标日期 {target_label}"
                )
            classes = (await target_card.get_attribute("class") or "").split()
            if "active" not in classes:
                phase = "等待目标日期场地数据返回"
                async with page.expect_response(
                    lambda response: self._is_target_date_response(response, plan),
                    timeout=self.config.browser.navigation_timeout_ms,
                ) as response_info:
                    await target_card.evaluate("element => element.click()")
                response = await response_info.value
                if response.status != 200:
                    raise BookingError(f"目标日期接口返回 HTTP {response.status}")
                await response.json()
            phase = "等待目标日期预约表格更新"
            await self._wait_for_booking_table(page)
            if "active" not in (await target_card.get_attribute("class") or "").split():
                raise CalibrationRequired("目标日期点击后未激活")
            return
        except BookingError:
            raise
        except Exception as exc:
            raise CalibrationRequired(
                f"预约日期 {target_label}：{phase}失败（{type(exc).__name__}）"
            ) from exc

    async def _wait_for_booking_table(self, page: Any) -> None:
        await self._wait_for_authenticated_condition(page,
            r"""() => {
                const table = document.querySelector('.spaceTable > table');
                if (!table || table.querySelectorAll('tr').length < 3) return false;
                const header = table.querySelector('tr').textContent.replace(/\s+/g, '');
                if (!/\d{2}:\d{2}-\d{2}:\d{2}/.test(header)) return false;
                return [...document.querySelectorAll('.loading.ivu-spin')].every(el =>
                    getComputedStyle(el).display === 'none' ||
                    getComputedStyle(el).visibility === 'hidden' ||
                    el.classList.contains('fade-leave-active')
                );
            }""",
        )

    @staticmethod
    def _is_target_date_response(response: Any, plan: DailyPlan) -> bool:
        return (
            "/venue-server/api/reservation/day/info" in response.url
            and f"searchDate={plan.target_date.isoformat()}" in response.url
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
        # 同场地连续预约优先；同等条件下保留配置的场馆顺序。
        return sorted(
            (candidate for group in results for candidate in group),
            key=lambda candidate: len({item.court for item in candidate.selections}) > 1,
        )

    @staticmethod
    def _requested_time_slots(plan: DailyPlan) -> tuple[str, ...]:
        start, end = parse_clock(plan.start_time), parse_clock(plan.end_time)
        if start.minute or end.minute or start.second or end.second or end.hour - start.hour not in (1, 2):
            raise BookingError("目前支持整点起止的一小时或连续两小时预约")
        return tuple(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(start.hour, end.hour))

    async def _collect_page_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> list[LocatedCandidate]:
        periods = self._requested_time_slots(plan)
        options = []
        for period in periods:
            snapshot = await self._time_column_snapshot(page, period)
            options.append([cell for cell in snapshot if self._cell_is_available(cell)])
        if any(not cells for cells in options):
            return []

        candidates = []
        for cells in product(*options):
            selections = tuple(SlotSelection(cell["court"], period) for cell, period in zip(cells, periods))
            # 多时段使用场地号和时间重新定位，不能保存翻页前的列定位器。
            locator = None
            if len(cells) == 1:
                locator = self._snapshot_cell_locator(page, cells[0])
            candidates.append(LocatedCandidate(
                slot=SlotCandidate(venue_id=venue_id, dom_index=cells[0]["row"],
                    label="；".join(f"{item.court} {item.time_label}" for item in selections)),
                page=page, selector=".spaceTable > table tr", locator=locator, selections=selections,
            ))
        return sorted(candidates, key=lambda candidate: len({item.court for item in candidate.selections}) > 1)

    async def _show_time_column(self, page: Any, target: str) -> None:
        table = page.locator(".spaceTable > table")
        seen = set()
        for _ in range(6):
            headers = await table.locator("tr").first.locator("td, th").all_text_contents()
            headers = [_clean_text(text) for text in headers]
            if target in headers:
                return
            if tuple(headers) in seen or len(headers) < 2:
                break
            seen.add(tuple(headers))
            arrow_class = (
                "ivu-icon-ios-arrow-back" if target < headers[1]
                else "ivu-icon-ios-arrow-forward"
            )
            arrow = table.locator(f"tr:first-child .{arrow_class}")
            if not await arrow.count():
                break
            old_header = await table.locator("tr").first.text_content()
            await arrow.evaluate("element => element.click()")
            try:
                await page.wait_for_function(
                    "old => document.querySelector('.spaceTable > table tr').textContent !== old",
                    arg=old_header, polling=25,
                    timeout=self.config.browser.navigation_timeout_ms,
                )
            except Exception as exc:
                raise CalibrationRequired(f"预约表格无法翻到目标时段 {target}") from exc
        raise CalibrationRequired(f"预约表格没有显示目标时段 {target}")

    async def _time_column_snapshot(self, page: Any, target: str) -> list[dict[str, Any]]:
        await self._show_time_column(page, target)
        return await page.locator(".spaceTable > table").evaluate(
            r"""(table, target) => {
                const rows = [...table.querySelectorAll('tr')];
                const text = el => (el?.textContent || '').replace(/\s+/g, '').replace(/：/g, ':').replace(/[—–至~]/g, '-');
                const column = [...rows[0].querySelectorAll('td,th')].findIndex(el => text(el) === target);
                return rows.flatMap((row, index) => {
                    const cells = [...row.querySelectorAll('td,th')];
                    const court = text(cells[0]).replace(/号场$/, '号');
                    if (!/^\d+号$/.test(court)) return [];
                    let logicalColumn = 0;
                    for (let i=0; i<cells.length; i++) {
                        const span = cells[i].colSpan || 1;
                        if (column >= logicalColumn && column < logicalColumn + span)
                            return [{row:index, column:i, court, span, text:text(cells[i])}];
                        logicalColumn += span;
                    }
                    return [];
                });
            }""", target,
        )

    def _cell_is_available(self, cell: dict[str, Any]) -> bool:
        return (cell["span"] == 1 and self._is_available_slot_text(cell["text"])
                and not any(word in cell["text"] for word in ("已售", "占用", "结束", "活动")))

    @staticmethod
    def _snapshot_cell_locator(page: Any, cell: dict[str, Any]) -> Any:
        return page.locator(".spaceTable > table tr").nth(cell["row"]).locator("td, th").nth(cell["column"])

    async def _selected_slots(self, page: Any) -> set[SlotSelection]:
        # 目标网页在尚未选场时隐藏此容器，但容器仍在 DOM 中。
        texts = await page.locator(".selected_box").all_text_contents()
        if not texts:
            raise CalibrationRequired("无法读取已选时段区域，停止提交")
        selected: set[SlotSelection] = set()
        for text in texts:
            cleaned = _clean_text(text)
            groups = re.findall(r"(\d+)号(?:场)?:([^号]*?)(?=\d+号(?:场)?:|$)", cleaned)
            if re.search(r"\d+号", cleaned) and not groups:
                raise CalibrationRequired("无法解析已选时段，停止提交")
            for court, ranges in groups:
                intervals = re.findall(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", ranges)
                if not intervals:
                    raise CalibrationRequired("无法解析已选时段，停止提交")
                for start, end in intervals:
                    begin, finish = parse_clock(start), parse_clock(end)
                    if begin.minute or finish.minute or finish <= begin:
                        raise CalibrationRequired("已选区域出现非整点时段，停止提交")
                    for hour in range(begin.hour, finish.hour):
                        selected.add(SlotSelection(f"{court}号", f"{hour:02d}:00-{hour+1:02d}:00"))
        return selected

    async def _wait_for_exact_selection(self, page: Any, expected: set[SlotSelection]) -> bool:
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if await self._selected_slots(page) == expected:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _clear_selected_slots(self, page: Any) -> None:
        selected = await self._selected_slots(page)
        # 已选列表的删除按钮跨分页仍可用，不必来回翻页取消格子。
        remove_buttons = page.locator(".selected_box .reserve-msg-row .ivu-icon-ios-close-circle")
        for _ in range(await remove_buttons.count()):
            await remove_buttons.first.click()
        if selected and not await self._wait_for_exact_selection(page, set()):
            raise CalibrationRequired("上一组已选场地未清除，停止提交")

    async def _select_candidate_slots(self, candidate: LocatedCandidate) -> bool:
        page = candidate.page
        await self._clear_selected_slots(page)
        expected: set[SlotSelection] = set()
        for item in candidate.selections:
            snapshot = await self._time_column_snapshot(page, item.time_label)
            cell = next((cell for cell in snapshot if cell["court"] == item.court), None)
            if cell is None or not self._cell_is_available(cell):
                await self._clear_selected_slots(page)
                return False
            await self._snapshot_cell_locator(page, cell).click()
            expected.add(item)
            if not await self._wait_for_exact_selection(page, expected):
                await self._clear_selected_slots(page)
                return False
        return bool(expected)

    async def _collect_matrix_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> tuple[list[LocatedCandidate], bool]:
        table_candidates, table_detected = await self._collect_table_matrix_candidates(
            venue_id, page, plan
        )
        if table_detected:
            return table_candidates, True
        return await self._collect_geometric_matrix_candidates(venue_id, page, plan)

    async def _collect_table_matrix_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> tuple[list[LocatedCandidate], bool]:
        """按“时间列 + 场地行”读取预约表格，不依赖单元格的屏幕坐标。"""

        target_labels = {
            self._normalise_time_label(f"{start}-{end}")
            for start in self._clock_labels(plan.start_time)
            for end in self._clock_labels(plan.end_time)
        }
        rows = page.locator("tr")
        row_count = min(await rows.count(), 500)
        blocked_words = ("已售", "占用","结束", "活动")
        candidates: list[LocatedCandidate] = []
        table_detected = False
        seen: set[tuple[int, int, int, int, str]] = set()

        for header_index in range(row_count):
            header_row = rows.nth(header_index)
            try:
                if not await header_row.is_visible():
                    continue
                header_cells = header_row.locator("th, td")
                header_count = await header_cells.count()
                header_texts = [
                    self._normalise_time_label(await header_cells.nth(index).inner_text())
                    for index in range(header_count)
                ]
            except Exception:
                continue

            target_column = next(
                (index for index, text in enumerate(header_texts) if text in target_labels),
                None,
            )
            if target_column is None:
                continue
            table_detected = True

            for row_index in range(header_index + 1, row_count):
                row = rows.nth(row_index)
                try:
                    if not await row.is_visible():
                        continue
                    cells = row.locator("th, td")
                    cell_count = await cells.count()
                    if cell_count <= target_column:
                        continue
                    row_texts = [
                        self._normalise_time_label(await cells.nth(index).inner_text())
                        for index in range(cell_count)
                    ]
                except Exception:
                    continue

                # 表格底部会重复时间表头；到达下一张表头后停止扫描本段场地行。
                if any(text in target_labels for text in row_texts):
                    break
                if not re.fullmatch(r"\d+\s*号(?:场)?", row_texts[0]):
                    continue

                item = cells.nth(target_column)
                try:
                    if not await item.is_visible() or not await item.is_enabled():
                        continue
                    box = await item.bounding_box()
                    text = (await item.inner_text()).strip()
                except Exception:
                    continue
                if box is None:
                    continue
                key = (
                    round(box["x"]),
                    round(box["y"]),
                    round(box["width"]),
                    round(box["height"]),
                    text,
                )
                if (
                    key in seen
                    or any(word in text for word in blocked_words)
                    or not self._is_available_slot_text(text)
                ):
                    continue
                seen.add(key)
                candidates.append(
                    LocatedCandidate(
                        slot=SlotCandidate(
                            venue_id=venue_id,
                            dom_index=row_index,
                            label=(
                                f"{row_texts[0]} "
                                f"{plan.start_time}-{plan.end_time} {text or '空闲'}"
                            ),
                        ),
                        page=page,
                        selector="tr",
                        locator=item,
                    )
                )
            # 一个页面只取第一张匹配目标时间列的预约表，避免重复处理固定表头。
            break
        return candidates, table_detected

    async def _collect_geometric_matrix_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> tuple[list[LocatedCandidate], bool]:
        """非表格排版的兼容后备：按目标时间表头的横向位置读取单元格。"""

        header_boxes = await self._target_time_header_boxes(page, plan)
        if not header_boxes:
            return [], False

        header = header_boxes[0]
        schedule_bottom = next(
            (box["y"] for box in header_boxes[1:] if box["y"] > header["y"] + 5),
            None,
        )
        header_left = header["x"]
        header_center = header_left + header["width"] / 2
        selectors = tuple(
            dict.fromkeys(
                (*self.config.selectors.get("slot_candidates", ()), *MATRIX_CELL_FALLBACK_SELECTORS)
            )
        )
        blocked_words = ("已售", "占用","结束", "活动")
        candidates: list[LocatedCandidate] = []
        seen: set[tuple[int, int, int, int, str]] = set()

        for selector in selectors:
            locator = page.locator(selector)
            count = min(await locator.count(), 500)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible() or not await item.is_enabled():
                        continue
                    box = await item.bounding_box()
                    if box is None or box["y"] <= header["y"] + header["height"]:
                        continue
                    if schedule_bottom is not None and box["y"] >= schedule_bottom:
                        continue
                    if not box["x"] <= header_center <= box["x"] + box["width"]:
                        continue
                    if box["width"] > 480:
                        continue
                    text = (await item.inner_text()).strip()
                except Exception:
                    continue
                key = (
                    round(box["x"]),
                    round(box["y"]),
                    round(box["width"]),
                    round(box["height"]),
                    text,
                )
                if (
                    key in seen
                    or any(word in text for word in blocked_words)
                    or not self._is_available_slot_text(text)
                ):
                    continue
                seen.add(key)
                candidates.append(
                    LocatedCandidate(
                        slot=SlotCandidate(
                            venue_id=venue_id,
                            dom_index=index,
                            label=f"{plan.start_time}-{plan.end_time} {text or '空闲'}",
                        ),
                        page=page,
                        selector=selector,
                        locator=item,
                    )
                )
        return candidates, True

    async def _target_time_header_boxes(
        self, page: Any, plan: DailyPlan
    ) -> list[dict[str, float]]:
        labels = {
            f"{start}-{end}"
            for start in self._clock_labels(plan.start_time)
            for end in self._clock_labels(plan.end_time)
        }
        boxes: list[dict[str, float]] = []
        seen: set[tuple[int, int, int, int]] = set()
        for label in labels:
            locator = page.get_by_text(label, exact=True)
            count = min(await locator.count(), 20)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    box = await item.bounding_box()
                except Exception:
                    continue
                if box is None:
                    continue
                key = (
                    round(box["x"]),
                    round(box["y"]),
                    round(box["width"]),
                    round(box["height"]),
                )
                if key not in seen:
                    seen.add(key)
                    boxes.append(box)
        return sorted(boxes, key=lambda item: (item["y"], item["x"]))

    @staticmethod
    def _clock_labels(value: str) -> tuple[str, ...]:
        hour, minute = value.split(":", maxsplit=1)
        return value, f"{int(hour):02d}:{int(minute):02d}"

    @staticmethod
    def _normalise_time_label(value: str) -> str:
        return re.sub(r"\s+", "", value).replace("～", "-").replace("—", "-")

    @staticmethod
    def _is_available_slot_text(text: str) -> bool:
        return re.search(r"(?:￥|¥)\s*\d+(?:\.\d+)?|免费|可预约", text) is not None

    async def _attempt_candidates(
        self,
        candidates: list[LocatedCandidate],
        plan: DailyPlan,
        venue_pages: dict[str, Any],
    ) -> None:
        while candidates:
            restart_from_booking_start = False
            for candidate in candidates:
                self._active_page = candidate.page
                try:
                    await candidate.page.bring_to_front()
                except Exception:
                    pass
                self._set_stage(
                    "选择场地",
                    f"尝试场馆 {candidate.slot.venue_id}：{candidate.slot.label}",
                )
                if candidate.selections:
                    selected = await self._select_candidate_slots(candidate)
                else:
                    item = candidate.locator
                    if item is None:
                        item = candidate.page.locator(candidate.selector).nth(candidate.slot.dom_index)
                    if not await item.is_visible() or not await item.is_enabled():
                        continue
                    await item.click()
                    selected = await self._wait_for_slot_selection(candidate.page)
                if not selected:
                    self.store.add_event(
                        "warning",
                        "candidate_not_selected",
                        f"场馆 {candidate.slot.venue_id} 的候选格点击后未进入已选状态",
                    )
                    continue
                self._set_stage("场地已选", f"场馆 {candidate.slot.venue_id} 的候选格已选中")
                await self._select_first_companion(candidate.page)
                self._set_stage("选择同伴", "已选择第一位可用同伴")
                await self._accept_terms(candidate.page)
                self._set_stage("预约须知", "已勾选预约须知")
                submit = await self._wait_for_ready_submit(candidate.page)
                if candidate.selections and await self._selected_slots(candidate.page) != set(candidate.selections):
                    await self._clear_selected_slots(candidate.page)
                    self._set_stage("场地选择失效", "目标时段未全部选中，跳过本组候选", level="warning")
                    continue
                await self._confirm_submit(candidate, plan)
                self._start_post_submit_monitor(candidate.page)
                await submit.click()
                self._set_stage("提交预约", "预约表单已提交，正在识别返回页面")
                await candidate.page.wait_for_timeout(250)
                state = await self._wait_for_post_submit_state(candidate.page)

                if state == "companion_missing":
                    raise CalibrationRequired("提交被页面拒绝：同伴不能小于1人，说明同伴未成功选中")
                if state == "submit_conflict":
                    self._set_stage(
                        "订单冲突",
                        "提交订单时场地已被他人预订，刷新页面并重新执行预约流程",
                        level="warning",
                    )
                    await self._restart_booking_pages(venue_pages, plan)
                    candidates = await self._load_candidates_with_refresh(
                        venue_pages, plan
                    )
                    restart_from_booking_start = True
                    break
                if state == "captcha":
                    payment_succeeded = await self._handle_captcha(
                        candidate.page, plan, candidate.slot
                    )
                    if payment_succeeded is True:
                        return
                    if payment_succeeded is False:
                        self._set_stage(
                            "支付失败",
                            "支付失败或 60 秒超时，重新执行预约全流程",
                            level="warning",
                        )
                        await self._restart_booking_pages(venue_pages, plan)
                        candidates = await self._load_candidates_with_refresh(
                            venue_pages, plan
                        )
                        restart_from_booking_start = True
                        break
                    return
                if state == "payment":
                    payment_succeeded = await self._on_payment(
                        candidate.page, plan, candidate.slot
                    )
                    if payment_succeeded:
                        return
                    self._set_stage(
                        "支付失败",
                        "支付失败或 60 秒超时，重新执行预约全流程",
                        level="warning",
                    )
                    await self._restart_booking_pages(venue_pages, plan)
                    candidates = await self._load_candidates_with_refresh(
                        venue_pages, plan
                    )
                    restart_from_booking_start = True
                    break
                await self._save_screenshot(candidate.page, "post-submit-unknown.png")
                raise BookingError("提交后的页面状态无法确认。为避免重复订单，程序已停止")
            if restart_from_booking_start:
                if not candidates:
                    raise BookingError("订单冲突后刷新页面，但没有找到新的可预约场地")
                continue
            break
        raise BookingError("所有候选场地都已被占用")

    async def _restart_booking_pages(
        self, venue_pages: dict[str, Any], plan: DailyPlan
    ) -> None:
        await asyncio.gather(
            *(
                page.goto(
                    self.config.venue_urls[venue_id],
                    wait_until="domcontentloaded",
                )
                for venue_id, page in venue_pages.items()
            )
        )
        await asyncio.gather(
            *(self._select_target_date(page, plan) for page in venue_pages.values())
        )

    async def _confirm_submit(
        self,
        candidate: LocatedCandidate,
        plan: DailyPlan,
    ) -> None:
        """正式流程默认直接提交；交互式测试可覆盖此钩子增加人工确认。"""

        return None

    async def _select_first_companion(self, page: Any) -> None:
        if await self._click_first_companion_candidate(page):
            return

        opener = await self._first_visible(page, "companion_open")
        if opener is not None:
            await opener.click()
            await page.wait_for_timeout(200)
        if await self._click_first_companion_candidate(page):
            return
        raise CalibrationRequired("没有找到可选择的同伴")

    async def _click_first_companion_candidate(self, page: Any) -> bool:
        section_top, section_bottom = await self._companion_section_bounds(page)
        selectors = tuple(
            dict.fromkeys(
                (*self.config.selectors.get("companion_options", ()), *COMPANION_CANDIDATE_SELECTORS)
            )
        )
        seen: set[tuple[int, int, int, int, str]] = set()
        excluded_words = (
            "请选择",
            "新增",
            "添加",
            "取消",
            "同伴",
            "最少",
            "最多",
            "手机号",
            "提交",
            "订单金额",
            "预约须知",
            "已阅读",
            "已选",
            "选择",
        )
        explicit_names = (
            "黄淼",
            "雷欧",
        )

        for selector in selectors:
            locator = page.locator(selector)
            count = min(await locator.count(), 500)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    class_name = ((await item.get_attribute("class")) or "").lower()
                    if "addbtn" in class_name or "add-btn" in class_name:
                        continue
                    text = re.sub(r"\s+", "", (await item.inner_text()).strip())
                    if not text or len(text) > 20:
                        continue
                    normalized = text.replace("：", "").replace(" ", "")
                    if any(word in normalized for word in excluded_words):
                        continue
                    if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{2,12}", normalized):
                        continue
                    if section_top is not None:
                        box = await item.bounding_box()
                        if box is not None and box["y"] < section_top:
                            continue
                    if section_bottom is not None:
                        box = await item.bounding_box()
                        if box is not None and box["y"] >= section_bottom:
                            continue
                    key = (
                        round((await item.bounding_box())["x"]),
                        round((await item.bounding_box())["y"]),
                        round((await item.bounding_box())["width"]),
                        round((await item.bounding_box())["height"]),
                        normalized,
                    ) if await item.bounding_box() is not None else (0, 0, 0, 0, normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    await item.click(force=True)
                    await page.wait_for_timeout(200)
                    if await self._has_selected_companion(page):
                        return True
                except Exception:
                    continue

        for text in explicit_names:
            try:
                item = page.get_by_text(text, exact=True)
                if await item.count() == 0:
                    continue
                candidate = item.nth(0)
                if await candidate.is_visible():
                    await candidate.click(force=True)
                    await page.wait_for_timeout(200)
                    if await self._has_selected_companion(page):
                        return True
            except Exception:
                continue

        generic_selectors = ("div", "span", "label", "li", "p")
        for selector in generic_selectors:
            locator = page.locator(selector)
            count = min(await locator.count(), 500)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = re.sub(r"\s+", "", (await item.inner_text()).strip())
                    if not text or len(text) > 10:
                        continue
                    if any(word in text for word in excluded_words):
                        continue
                    if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{2,10}", text):
                        await item.click(force=True)
                        await page.wait_for_timeout(200)
                        if await self._has_selected_companion(page):
                            return True
                except Exception:
                    continue
        return False

    async def _companion_section_bounds(
        self, page: Any
    ) -> tuple[float | None, float | None]:
        section_top: float | None = None
        header = page.get_by_text(re.compile(r"^同伴"))
        for index in range(min(await header.count(), 20)):
            item = header.nth(index)
            try:
                if not await item.is_visible():
                    continue
                text = (await item.inner_text()).strip()
                if not text.startswith("同伴") or len(text) > 20:
                    continue
                box = await item.bounding_box()
            except Exception:
                continue
            if box is not None:
                section_top = box["y"] + box["height"]
                break

        section_bottom: float | None = None
        phone_label = page.get_by_text("手机号", exact=True)
        for index in range(min(await phone_label.count(), 20)):
            item = phone_label.nth(index)
            try:
                if not await item.is_visible():
                    continue
                box = await item.bounding_box()
            except Exception:
                continue
            if box is not None:
                section_bottom = box["y"]
                break
        return section_top, section_bottom

    async def _wait_for_slot_selection(self, page: Any) -> bool:
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if await self._has_selected_slot(page):
                return True
            await page.wait_for_timeout(150)
        return False

    async def _has_selected_slot(self, page: Any) -> bool:
        """页面在成功选中场地后，会在“已选”栏展示“<场地号>号：<时间>”。"""

        try:
            text = _clean_text(await page.locator("body").inner_text())
        except Exception:
            return False
        return re.search(r"已选\s*\d+\s*号\s*[:：]", text) is not None

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
        if not await checkbox.is_checked():
            raise CalibrationRequired("预约须知复选框未能勾选")

    async def _has_selected_companion(self, page: Any) -> bool:
        try:
            body_text = _clean_text(await page.locator("body").inner_text())
        except Exception:
            return False

        if "请选择同伴" in body_text or "选择同伴" in body_text:
            return False
        if "同伴不能小于1人" in body_text:
            return False

        # 北航页面用 buddy-row active/is-active 表示已选同伴。
        for selector in (".buddy-row",):
            try:
                locator = page.locator(selector)
                if hasattr(locator, "evaluate_all"):
                    class_names = await locator.evaluate_all(
                        "(els) => els.map(el => (el.getAttribute('class') || ''))"
                    )
                    for class_name in class_names:
                        tokens = set(class_name.split())
                        if "buddy-row" in tokens and ("active" in tokens or "is-active" in tokens):
                            return True
                elif await locator.count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_ready_submit(self, page: Any) -> Any:
        """只等待已选场地所在页面的提交按钮进入蓝色可点击状态。"""

        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            submit = await self._first_visible(page, "submit_button")
            if submit is not None and await self._is_blue_enabled_button(submit):
                return submit
            await page.wait_for_timeout(150)
        incomplete: list[str] = []
        if not await self._has_selected_slot(page):
            incomplete.append("场地未选中（“已选”栏没有场地信息）")
        if not await self._has_selected_companion(page):
            incomplete.append("同伴未选中")
        if not await self._terms_are_accepted(page):
            incomplete.append("预约须知未勾选")
        detail = "；".join(incomplete) or "页面未将提交按钮置为可点击状态"
        raise CalibrationRequired(f"提交按钮尚未变为蓝色可点击状态：{detail}")

    async def _terms_are_accepted(self, page: Any) -> bool:
        checkbox = await self._first_visible(page, "agreement_checkbox")
        if checkbox is None:
            return False
        try:
            return await checkbox.is_checked()
        except Exception:
            return False

    @staticmethod
    async def _is_blue_enabled_button(button: Any) -> bool:
        try:
            if await button.get_attribute("disabled") is not None:
                return False
            aria_disabled = await button.get_attribute("aria-disabled")
            if aria_disabled == "true":
                return False
            if await button.evaluate(
                r"""element => {
                    const style = getComputedStyle(element);
                    const className = String(element.className || "").toLowerCase();
                    if (className.includes("disabled") || className.includes("disab") || className.includes("is-disabled")) {
                        return false;
                    }
                    if (style.pointerEvents === "none" || style.opacity === "0" || style.cursor === "not-allowed") {
                        return false;
                    }
                    return true;
                }"""
            ):
                return True

            return bool(
                await button.evaluate(
                    r"""element => {
                        const style = getComputedStyle(element);
                        const className = String(element.className || "").toLowerCase();
                        if (className.includes("disabled") || className.includes("disab") || className.includes("is-disabled")) return false;
                        if (className.includes("btn") || className.includes("primary") || className.includes("blue") || className.includes("submit")) return true;

                        const parseHex = value => {
                            if (!value) return null;
                            const s = value.trim();
                            if (!/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(s)) return null;
                            const hex = s.slice(1);
                            const full = hex.length === 3
                                ? hex.split("").map(ch => ch + ch).join("")
                                : hex;
                            const bigint = parseInt(full, 16);
                            return { r: (bigint >> 16) & 255, g: (bigint >> 8) & 255, b: bigint & 255 };
                        };

                        const parseRgb = value => {
                            if (!value) return null;
                            const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
                            if (!match) return null;
                            return { r: Number(match[1]), g: Number(match[2]), b: Number(match[3]) };
                        };

                        const colorCandidates = [
                            style.backgroundColor,
                            style.background,
                            style.getPropertyValue("--school-main-color"),
                            getComputedStyle(document.documentElement).getPropertyValue("--school-main-color"),
                            getComputedStyle(document.body).getPropertyValue("--school-main-color"),
                        ];

                        for (const candidate of colorCandidates) {
                            const rgb = parseRgb(candidate) || parseHex(candidate);
                            if (!rgb) continue;
                            const { r, g, b } = rgb;
                            if (b > r + 20 && b > g) return true;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    async def _wait_for_manual_captcha(
        self, page: Any, plan: DailyPlan, slot: SlotCandidate
    ) -> bool:
        self.store.update_status(plan.run_date, PlanStatus.CAPTCHA)
        self.store.add_event(
            "info", "captcha_wait", f"场馆 {slot.venue_id} 已到验证码，请人工点选"
        )
        self._notify(
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
            payment_page = await self._find_payment_page(page)
            if payment_page is not None:
                return await self._on_payment(payment_page, plan, slot)
            await asyncio.sleep(1)
        raise BookingError("等待人工验证码超时")

    async def _handle_captcha(
        self, page: Any, plan: DailyPlan, slot: SlotCandidate
    ) -> bool | None:
        if self.config.browser.captcha_mode == "manual":
            return await self._wait_for_manual_captcha(page, plan, slot)

        self._set_stage("自动验证码", "正在调用自动验证码求解器")
        challenge = CaptchaChallenge(
            page=page,
            plan=plan,
            slot=slot,
            artifacts_dir=self.artifacts_dir,
            image_bytes=self._captcha_image_bytes,
            response_payload=self._captcha_response_payload,
            token=self._captcha_token,
            secret_key=self._captcha_secret_key,
            word_list=self._captcha_word_list,
        )
        try:
            solved = await self.captcha_solver.solve(challenge)
        except AutoCaptchaUnavailable as exc:
            self.store.add_event("warning", "captcha_auto_unavailable", str(exc))
            self._notify(
                "自动验证码尚未接入",
                "已自动切换为人工验证码，请远程处理。",
                silent=True,
            )
            return await self._wait_for_manual_captcha(page, plan, slot)
        except Exception as exc:
            self.store.add_event("error", "captcha_auto_error", str(exc))
            self._notify(
                "自动验证码执行失败",
                "自动接口发生错误，已切换为人工验证码。",
                silent=True,
            )
            return await self._wait_for_manual_captcha(page, plan, slot)

        if not solved:
            payment_page = await self._find_payment_page(page)
            if payment_page is not None:
                return await self._on_payment(payment_page, plan, slot)
            self.store.add_event(
                "warning", "captcha_auto_failed", "自动求解器未完成验证码"
            )
            return await self._wait_for_manual_captcha(page, plan, slot)

        self._set_stage("自动验证码", "验证码点击已提交，等待支付页面确认结果")
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.browser.post_submit_detection_timeout_seconds
        )
        while asyncio.get_running_loop().time() < deadline:
            payment_page = await self._find_payment_page(page)
            if payment_page is not None:
                return await self._on_payment(payment_page, plan, slot)
            await asyncio.sleep(0.25)
        payment_page = await self._find_payment_page(page)
        if payment_page is not None:
            return await self._on_payment(payment_page, plan, slot)
        self.store.add_event(
            "warning", "captcha_auto_unconfirmed", "无法确认自动验证码结果，改为人工处理"
        )
        return await self._wait_for_manual_captcha(page, plan, slot)

    async def _find_payment_page(self, page: Any) -> Any | None:
        pages = [page]
        try:
            pages.extend(item for item in page.context.pages if item not in pages)
        except Exception:
            pass
        for candidate in pages:
            try:
                if await self._any_visible_across_frames(candidate, "payment_markers"):
                    return candidate
                url = candidate.url.lower()
                if "payment" in url or re.search(r"(^|[/_-])pay([/?#_-]|$)", url):
                    return candidate
            except Exception:
                continue
        return None

    async def _is_payment_page(self, page: Any) -> bool:
        return await self._find_payment_page(page) is not None

    async def _on_payment(
        self, page: Any, plan: DailyPlan, slot: SlotCandidate
    ) -> bool:
        page = await self._find_payment_page(page) or page
        payment_button = await self._first_visible_across_frames_with_fallback(
            page,
            "payment_button",
            PAYMENT_BUTTON_FALLBACK_SELECTORS,
        )
        if payment_button is None:
            raise CalibrationRequired("支付页未找到可点击的付款按钮，请校准 payment_button")
        await payment_button.click()

        qr_result = await self._wait_for_payment_qr(page)
        if qr_result is None:
            raise BookingError("点击付款后未找到支付二维码")
        qr_page, qr_locator = qr_result
        qr_path = self.artifacts_dir / "payment-qr.png"
        await self._save_payment_qr(qr_locator, qr_path)

        from .dashboard import payment_qr_url

        qr_url = payment_qr_url(self.config, self.secrets, plan.run_date)
        self.store.update_status(plan.run_date, PlanStatus.PAYMENT)
        self.store.add_event(
            "info", "payment_ready", f"场馆 {slot.venue_id} 已生成支付二维码"
        )
        self._notify(
            "场地已抢到，请打开二维码支付",
            f"{plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}，场馆 {slot.venue_id}",
            emergency=True,
            url=qr_url,
            image=qr_url,
            url_title="打开支付二维码",
        )
        await self._save_screenshot(qr_page, "payment-ready.png")
        payment_state = await self._wait_for_payment_result(qr_page)
        if payment_state == "success":
            self.store.update_status(plan.run_date, PlanStatus.CONFIRMED)
            self.store.add_event(
                "info", "payment_succeeded", f"场馆 {slot.venue_id} 已完成支付"
            )
            self._notify(
                "预约支付成功",
                f"{plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}，场馆 {slot.venue_id}",
                silent=True,
            )
            return True

        reason = "支付失败" if payment_state == "failure" else "支付等待超过 60 秒"
        self.store.update_status(plan.run_date, PlanStatus.RUNNING)
        self.store.add_event(
            "warning", "payment_retry", f"{reason}，准备重新提交预约"
        )
        return False

    async def _wait_for_payment_result(self, page: Any) -> str:
        timeout_seconds = max(1, min(self.config.browser.payment_hold_seconds, 60))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            pages = [page]
            try:
                pages.extend(item for item in page.context.pages if item not in pages)
            except Exception:
                pass
            for candidate in pages:
                if await self._any_visible_across_frames(
                    candidate, "payment_success_markers"
                ):
                    return "success"
                if await self._any_visible_across_frames(
                    candidate, "payment_failure_markers"
                ):
                    return "failure"
            await asyncio.sleep(0.25)
        return "timeout"

    async def _save_payment_qr(self, locator: Any, path: Path) -> None:
        # 当前支付页直接生成 PNG data URL。读取原图不依赖弹窗动画和滚动，
        # 避免 Locator.screenshot 等待期间 Vue 重绘节点导致截取失败。
        source = await locator.evaluate("""e => {
            if (e.tagName === 'CANVAS') return e.toDataURL('image/png');
            if (e.tagName !== 'IMG') return '';
            if (e.src.startsWith('data:image/png;base64,iVBOR')) return e.src;
            if (!e.src.startsWith('data:image/')) return '';
            const canvas = document.createElement('canvas');
            canvas.width = e.naturalWidth;
            canvas.height = e.naturalHeight;
            canvas.getContext('2d').drawImage(e, 0, 0);
            return canvas.toDataURL('image/png');
        }""")
        prefix = "data:image/png;base64,"
        if source.startswith(prefix):
            data = base64.b64decode(source[len(prefix):], validate=True)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BookingError("支付二维码图片不是有效的 PNG")
        else:
            data = await locator.screenshot(type="png")
        if not self._is_payment_qr(data):
            raise BookingError("支付图片未检测到二维码，已停止发送以避免误发验证码")
        path.write_bytes(data)

    @staticmethod
    def _is_payment_qr(data: bytes) -> bool:
        import cv2
        import numpy as np

        pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if pixels is None:
            return False
        detected, _ = cv2.QRCodeDetector().detect(pixels)
        return bool(detected)

    async def _wait_for_payment_qr(self, page: Any) -> tuple[Any, Any] | None:
        # 当前目标站的支付二维码固定为此元素；data URL 的随机内容也可能
        # 含有 qr，不能用 img[src*='qr'] 或通用 canvas 去匹配验证码。
        selectors = ("img.payImage",)
        deadline = asyncio.get_running_loop().time() + min(
            self.config.browser.post_submit_detection_timeout_seconds,
            30,
        )
        while asyncio.get_running_loop().time() < deadline:
            pages = [page]
            try:
                pages.extend(item for item in page.context.pages if item not in pages)
            except Exception:
                pass
            for candidate in pages:
                for frame in candidate.frames:
                    for selector in selectors:
                        locator = frame.locator(selector)
                        for index in range(await locator.count()):
                            item = locator.nth(index)
                            if await item.is_visible() and await item.evaluate(
                                "e => e.tagName !== 'IMG' || (e.complete && e.naturalWidth > 0)"
                            ):
                                return candidate, item
            await asyncio.sleep(0.25)
        return None

    async def _first_visible(self, page: Any, selector_group: str) -> Any | None:
        selectors = self.config.selectors.get(selector_group, ())
        for selector in selectors:
            locator = page.locator(selector)
            count = min(await locator.count(), 20)
            for index in range(count):
                item = locator.nth(index)
                if await item.is_visible():
                    return item
        return None

    async def _first_visible_with_fallback(
        self,
        page: Any,
        selector_group: str,
        fallback: tuple[str, ...],
        *,
        require_enabled: bool = False,
    ) -> Any | None:
        selectors = self.config.selectors.get(selector_group, fallback)
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(await locator.count(), 20)
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible() and (
                        not require_enabled or await item.is_enabled()
                    ):
                        return item
            except Exception:
                continue
        return None

    async def _any_visible(self, page: Any, selector_group: str) -> bool:
        return await self._first_visible(page, selector_group) is not None

    async def _first_visible_across_frames(
        self, page: Any, selector_group: str
    ) -> Any | None:
        scopes = [page]
        main_frame = getattr(page, "main_frame", None)
        for frame in getattr(page, "frames", []):
            if frame is not main_frame:
                scopes.append(frame)
        for scope in scopes:
            try:
                item = await self._first_visible(scope, selector_group)
            except Exception:
                continue
            if item is not None:
                return item
        return None

    async def _first_visible_across_frames_with_fallback(
        self,
        page: Any,
        selector_group: str,
        fallback: tuple[str, ...],
        *,
        require_enabled: bool = False,
    ) -> Any | None:
        scopes = [page]
        main_frame = getattr(page, "main_frame", None)
        for frame in getattr(page, "frames", []):
            if frame is not main_frame:
                scopes.append(frame)
        for scope in scopes:
            try:
                item = await self._first_visible_with_fallback(
                    scope,
                    selector_group,
                    fallback,
                    require_enabled=require_enabled,
                )
            except Exception:
                continue
            if item is not None:
                return item
        return None

    async def _any_visible_across_frames(
        self, page: Any, selector_group: str
    ) -> bool:
        return await self._first_visible_across_frames(page, selector_group) is not None

    async def _wait_for_post_submit_state(self, page: Any) -> str | None:
        monitor = self._post_submit_monitor or self._start_post_submit_monitor(page)
        captcha_seen = monitor["captcha_seen"]

        deadline = (
            asyncio.get_running_loop().time()
            + self.config.browser.post_submit_detection_timeout_seconds
        )
        try:
            while asyncio.get_running_loop().time() < deadline:
                if captcha_seen["value"]:
                    response_task = monitor["response_task"]
                    if response_task is None:
                        return "captcha"
                    if response_task.done():
                        response_task.result()
                        return "captcha"
                if await self._has_companion_required_error(page):
                    return "companion_missing"
                if await self._any_visible_across_frames(page, "submit_conflict_markers"):
                    return "submit_conflict"
                if await self._any_visible_across_frames(page, "captcha_markers"):
                    return "captcha"
                if await self._is_payment_page(page):
                    return "payment"
                await asyncio.sleep(0.25)
            return None
        finally:
            response_task = monitor["response_task"]
            if response_task is not None and not response_task.done():
                response_task.cancel()
            handler = monitor["handler"]
            try:
                page.remove_listener("response", handler)
            except Exception:
                pass
            self._post_submit_monitor = None

    def _start_post_submit_monitor(self, page: Any) -> dict[str, Any]:
        captcha_seen = {"value": False}
        monitor: dict[str, Any] = {
            "captcha_seen": captcha_seen,
            "response_task": None,
            "handler": None,
        }
        self._captcha_response_payload = None
        self._captcha_image_bytes = None

        async def read_captcha_response(response: Any) -> None:
            headers = getattr(response, "headers", {})
            if callable(headers):
                headers = headers()
            content_type = str((headers or {}).get("content-type", "")).lower()
            if "application/json" not in content_type:
                return
            payload = await response.json()
            fields = extract_clickword_fields(payload)
            image_bytes = decode_clickword_response(payload)
            self._captcha_response_payload = payload
            self._captcha_image_bytes = image_bytes
            current_word_list = getattr(self, "_captcha_word_list", None)
            current_token = getattr(self, "_captcha_token", None)
            current_secret_key = getattr(self, "_captcha_secret_key", None)
            self._captcha_word_list = fields.get("word_list") or current_word_list
            self._captcha_token = fields.get("token") or current_token
            self._captcha_secret_key = fields.get("secret_key") or current_secret_key

        def on_response(response: Any) -> None:
            try:
                url = response.url
            except Exception:
                return
            if "/api/captcha/get" in url and "captchaType=clickWord" in url:
                captcha_seen["value"] = True
                try:
                    monitor["response_task"] = asyncio.create_task(
                        read_captcha_response(response)
                    )
                except RuntimeError:
                    monitor["response_task"] = None

        monitor["handler"] = on_response
        self._post_submit_monitor = monitor
        try:
            page.on("response", on_response)
        except Exception:
            pass
        return monitor

    async def _has_companion_required_error(self, page: Any) -> bool:
        try:
            text = _clean_text(await page.locator("body").inner_text())
        except Exception:
            return False
        return "同伴不能小于1人" in text

    def _notify(self, title: str, message: str, **kwargs: Any) -> None:
        try:
            self.notifier.send(title, message, **kwargs)
        except Exception as exc:
            self.store.add_event(
                "warning", "notification_failed", f"{title}：{exc}"
            )

    def _set_stage(self, stage: str, message: str, *, level: str = "info") -> None:
        self.current_stage = stage
        self.store.add_event(level, "booking_progress", f"{stage}：{message}")

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
    captcha_solver: AutoCaptchaSolver | None = None,
) -> None:
    automation = BookingAutomation(
        config,
        store,
        secrets,
        notifier,
        artifacts_dir,
        captcha_solver=captcha_solver,
    )
    try:
        await automation.run(plan, wait_for_open=wait_for_open)
    except Exception as exc:
        store.update_status(plan.run_date, PlanStatus.FAILED)
        failure_detail = str(exc)
        if exc.__cause__ is not None:
            cause = exc.__cause__
            cause_message = str(cause).splitlines()[0] if str(cause) else ""
            failure_detail += f"；底层错误：{type(cause).__name__}：{cause_message}"
        store.add_event("error", "booking_failed", failure_detail)
        title = (
            "预约页面操作失败"
            if isinstance(exc, CalibrationRequired)
            else "预约程序停止"
        )
        message = f"停止步骤：{automation.current_stage}。错误：{exc}"
        try:
            notifier.send(title, message, silent=True)
        except Exception as notification_exc:
            store.add_event(
                "warning",
                "notification_failed",
                f"{title}：{notification_exc}",
            )
        raise
    finally:
        store.purge_old_events(days=3)
