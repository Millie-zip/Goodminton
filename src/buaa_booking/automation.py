from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


class BookingError(RuntimeError):
    pass


class CalibrationRequired(BookingError):
    pass


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
                await self._ensure_logged_in(page)
                self._set_stage("登录", "登录状态已确认")
                venue_pages = await self._open_venue_pages(context, page)
                self._set_stage("场馆页面", "场馆 38、39 页面已打开")
                if wait_for_open:
                    self._set_stage("等待开放", f"等待 {self.config.open_time} 开放预约")
                    await self._wait_until_open()
                    await asyncio.gather(
                        *(venue_page.reload(wait_until="domcontentloaded") for venue_page in venue_pages.values())
                    )
                await asyncio.gather(
                    *(self._select_target_date(venue_page, plan) for venue_page in venue_pages.values())
                )
                self._set_stage("选择日期", f"已选择 {plan.target_date.isoformat()}")
                candidates = await self._load_candidates_with_refresh(venue_pages, plan)
                if not candidates:
                    raise BookingError("两个场馆都没有找到符合时间的可预约场地")
                self._set_stage("检测场地", f"找到 {len(candidates)} 个候选场地")
                await self._attempt_candidates(candidates, plan)
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

    async def _ensure_logged_in(self, page: Any) -> None:
        await page.goto(self.config.home_url, wait_until="domcontentloaded")
        if await self._any_visible(page, "logged_in_markers"):
            return

        username_input = await self._first_visible(page, "login_username")
        password_input = await self._first_visible(page, "login_password")
        if username_input is None or password_input is None:
            login_entry = await self._find_login_entry(page)
            if login_entry is not None:
                await login_entry.click()
                await page.wait_for_timeout(1000)
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
        self._notify(
            "需要完成登录",
            "统一身份认证没有自动通过，请远程打开电脑完成登录。",
            silent=True,
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
        selectors = tuple(
            dict.fromkeys(
                (*self.config.selectors.get("date_buttons", ()), *DATE_BUTTON_FALLBACK_SELECTORS)
            )
        )
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.browser.navigation_timeout_ms / 1000
        )
        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                locator = page.locator(selector)
                count = min(await locator.count(), 100)
                for index in range(count):
                    item = locator.nth(index)
                    try:
                        if not await item.is_visible():
                            continue
                        text = _clean_text(await item.inner_text())
                    except Exception:
                        continue
                    if not any(label in text for label in labels):
                        continue
                    try:
                        async with page.expect_response(
                            lambda response: self._is_target_date_response(response, plan),
                            timeout=min(5_000, self.config.browser.navigation_timeout_ms),
                        ) as response_info:
                            await item.click()
                        response = await response_info.value
                    except Exception:
                        continue
                    if response.status != 200:
                        raise BookingError(
                            f"预约日期接口返回 HTTP {response.status}：{plan.target_date.isoformat()}"
                        )
                    await page.wait_for_timeout(300)
                    return
            await page.wait_for_timeout(250)
        raise CalibrationRequired(
            f"没有找到预约日期 {plan.target_date.isoformat()}，需要校 date_buttons"
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
        return [candidate for group in results for candidate in group]

    async def _collect_page_candidates(
        self, venue_id: str, page: Any, plan: DailyPlan
    ) -> list[LocatedCandidate]:
        matrix_candidates, matrix_detected = await self._collect_matrix_candidates(
            venue_id, page, plan
        )
        if matrix_detected:
            return matrix_candidates

        blocked_words = ("已约", "占用", "锁定", "不可", "售罄", "结束", "活动")
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
        blocked_words = ("已约", "占用", "锁定", "不可", "售罄", "结束", "活动")
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
        blocked_words = ("已约", "占用", "锁定", "不可", "售罄", "结束", "活动")
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
        self, candidates: list[LocatedCandidate], plan: DailyPlan
    ) -> None:
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
            item = candidate.locator
            if item is None:
                item = candidate.page.locator(candidate.selector).nth(candidate.slot.dom_index)
            if not await item.is_visible() or not await item.is_enabled():
                continue
            await item.click()
            if not await self._wait_for_slot_selection(candidate.page):
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
            await self._save_screenshot(candidate.page, "before-submit.png")
            await self._confirm_submit(candidate, plan)
            self._start_post_submit_monitor(candidate.page)
            await submit.click()
            self._set_stage("提交预约", "预约表单已提交，正在识别返回页面")
            await candidate.page.wait_for_timeout(250)
            await self._save_screenshot(candidate.page, "after-submit.png")
            state = await self._wait_for_post_submit_state(candidate.page)

            if state == "companion_missing":
                raise CalibrationRequired("提交被页面拒绝：同伴不能小于1人，说明同伴未成功选中")
            if state == "occupied":
                self._set_stage(
                    "场地被占用",
                    f"场馆 {candidate.slot.venue_id} 的候选场地已被占用，尝试下一个",
                    level="warning",
                )
                await candidate.page.goto(
                    self.config.venue_urls[candidate.slot.venue_id],
                    wait_until="domcontentloaded",
                )
                await self._select_target_date(candidate.page, plan)
                continue
            if state == "captcha":
                await self._handle_captcha(candidate.page, plan, candidate.slot)
                return
            if state == "payment":
                await self._on_payment(candidate.page, plan, candidate.slot)
                return
            await self._save_screenshot(candidate.page, "post-submit-unknown.png")
            raise BookingError("提交后的页面状态无法确认。为避免重复订单，程序已停止")
        raise BookingError("所有候选场地都已被占用")

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

        # Real BUAA behavior: the selected companion is represented by the literal class value
        # "buddy-row active" (or "buddy-row is-active") as a space-separated class list.
        # We check the actual class attribute instead of assuming a dotted CSS selector.
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
    ) -> None:
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
            if await self._is_payment_page(page):
                await self._on_payment(page, plan, slot)
                return
            await asyncio.sleep(1)
        raise BookingError("等待人工验证码超时")

    async def _handle_captcha(
        self, page: Any, plan: DailyPlan, slot: SlotCandidate
    ) -> None:
        if self.config.browser.captcha_mode == "manual":
            await self._wait_for_manual_captcha(page, plan, slot)
            return

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
            await self._wait_for_manual_captcha(page, plan, slot)
            return
        except Exception as exc:
            self.store.add_event("error", "captcha_auto_error", str(exc))
            self._notify(
                "自动验证码执行失败",
                "自动接口发生错误，已切换为人工验证码。",
                silent=True,
            )
            await self._wait_for_manual_captcha(page, plan, slot)
            return

        if not solved:
            self.store.add_event(
                "warning", "captcha_auto_failed", "自动求解器未完成验证码"
            )
            await self._wait_for_manual_captcha(page, plan, slot)
            return

        self._set_stage("自动验证码", "求解器已返回成功，等待支付页面")
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.browser.post_submit_detection_timeout_seconds
        )
        while asyncio.get_running_loop().time() < deadline:
            if await self._is_payment_page(page):
                await self._on_payment(page, plan, slot)
                return
            await asyncio.sleep(0.25)
        self.store.add_event(
            "warning", "captcha_auto_unconfirmed", "无法确认自动验证码结果，改为人工处理"
        )
        await self._wait_for_manual_captcha(page, plan, slot)

    async def _is_payment_page(self, page: Any) -> bool:
        if await self._any_visible_across_frames(page, "payment_markers"):
            return True
        url = page.url.lower()
        return "payment" in url or re.search(r"(^|[/_-])pay([/?#_-]|$)", url) is not None

    async def _on_payment(self, page: Any, plan: DailyPlan, slot: SlotCandidate) -> None:
        self.store.update_status(plan.run_date, PlanStatus.PAYMENT)
        self.store.add_event(
            "info", "payment_ready", f"场馆 {slot.venue_id} 已进入支付页面"
        )
        self._notify(
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
                if await self._any_visible_across_frames(page, "occupied_markers"):
                    return "occupied"
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
        store.add_event("error", "booking_failed", str(exc))
        title = (
            "预约配置未完成"
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
