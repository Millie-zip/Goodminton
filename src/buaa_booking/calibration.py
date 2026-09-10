from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig


SAFE_TEXT = re.compile(
    r"(?:\d{1,2}[:：]\d{2}|预约|提交|同伴|同行|阅读|同意|验证码|支付|日期|下一步|登录)"
)


def _redact_text(value: str) -> str:
    value = " ".join(value.split())[:120]
    return value if SAFE_TEXT.search(value) else "<redacted>"


async def calibrate(config: AppConfig, artifacts_dir: Path) -> Path:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装依赖和 Chromium") from exc

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = (artifacts_dir / f"calibration-{stamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    config.browser.profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            config.browser.profile_dir,
            headless=False,
            no_viewport=True,
            locale="zh-CN",
            timezone_id=config.timezone,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(config.home_url, wait_until="domcontentloaded")
        await asyncio.to_thread(
            input,
            "请在浏览器中登录并进入任一羽毛球预约页。不要提交订单。完成后按 Enter：",
        )

        inventory: list[dict[str, object]] = []
        for venue_id in config.venue_priority:
            await page.goto(config.venue_urls[venue_id], wait_until="domcontentloaded")
            await asyncio.to_thread(
                input,
                f"请把场馆 {venue_id} 切换到可显示场地时间的页面，然后按 Enter：",
            )
            raw: list[dict[str, object]] = await page.locator(
                "button, input, label, a, [role], [data-time], [class*='time'], [class*='venue']"
            ).evaluate_all(
                """elements => elements.slice(0, 500).map((el, index) => ({
                    index,
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type'),
                    role: el.getAttribute('role'),
                    name: el.getAttribute('name'),
                    className: typeof el.className === 'string' ? el.className.slice(0, 160) : '',
                    dataTime: el.getAttribute('data-time'),
                    disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
                    text: (el.innerText || el.getAttribute('aria-label') || '').slice(0, 120)
                }))"""
            )
            for item in raw:
                item["text"] = _redact_text(str(item.get("text", "")))
                item["venue_id"] = venue_id
                inventory.append(item)
            await page.screenshot(path=output_dir / f"venue-{venue_id}.png", full_page=True)

        report_path = output_dir / "element-inventory.json"
        report_path.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await context.close()
    return report_path

