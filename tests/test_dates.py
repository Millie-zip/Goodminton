"""Offline date-window regressions using an isolated browser and mocked HTTP."""
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from buaa_booking.automation import (
    AuthenticationExpired, BookingAutomation, BookingError, BookingPageLoadError,
    CalibrationRequired, SYSTEM_BROWSER_EXECUTABLES,
)
from buaa_booking.config import load_config
from buaa_booking.models import DailyPlan
from playwright.async_api import async_playwright

PLAN = DailyPlan(date(2026, 10, 21), date(2026, 10, 23), True, "21:00", "22:00")


def fixture(days=(21, 22, 23), active=21, delay=0):
    cards = "".join(
        f'<div style="order: {i}" class="{"active" if day == active else ""}" '
        f'onclick="choose(this,{day})"><div>星期</div><span>10月{day}日</span></div>'
        for i, day in enumerate(days)
    )
    return f"""
    <div class="date_box_title">选择日期 星期五（10/23） 最早于10月22日 07:00可预约</div>
    <div class="date_box">{cards}</div>
    <div class="loading ivu-spin" style="display:none">loading</div>
    <div class="spaceTable"><table>
      <tr><td>07:00-08:00</td></tr><tr><td>1号</td></tr><tr><td>2号</td></tr>
    </table></div>
    <script>
    window.clicked=[];
    function choose(card,day) {{
      clicked.push(day);
      document.querySelector('.loading').style.display='block';
      fetch('https://date-fixture.test/venue-server/api/reservation/day/info?searchDate=2026-10-'+day)
        .then(r=>r.json()).then(()=>setTimeout(()=>{{
          document.querySelectorAll('.date_box > div').forEach(el=>el.classList.remove('active'));
          card.classList.add('active');
          document.querySelector('.loading').style.display='none';
        }},{delay}));
    }}
    </script>
    """


class DateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = object.__new__(BookingAutomation)
        config = load_config(ROOT / "config/booking.example.json")
        self.app.config = replace(config, browser=replace(config.browser, navigation_timeout_ms=1500))
        self.app._set_stage = Mock()
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        executable = next((p for p in SYSTEM_BROWSER_EXECUTABLES if p.is_file()), None)
        self.browser = await self.pw.chromium.launch(
            headless=True, timeout=20000,
            **({"executable_path": str(executable)} if executable else {}))
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.route("**/*", lambda route: route.abort())
        await self.context.route("https://date-fixture.test/**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}, body="{}"))
        self.page = await self.context.new_page()
        await self.page.set_content(fixture())
        self.page.reload = AsyncMock()  # Retain the local fixture; never navigate to the site.

    async def test_three_cards_selects_exact_target(self):
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [23])
        self.page.reload.assert_not_awaited()

    async def test_two_cards_report_window_and_notice_without_click(self):
        await self.page.set_content(fixture((21, 22)))
        with self.assertRaisesRegex(BookingError, "不在网站当前可选日期中") as caught:
            await self.app._select_target_date(self.page, PLAN)
        self.assertIn("10月21日、10月22日", str(caught.exception))
        self.assertIn("最早于10月22日 07:00可预约", str(caught.exception))
        self.assertEqual(await self.page.evaluate("clicked"), [])
        self.page.reload.assert_awaited_once()

    async def test_target_appears_after_single_reload(self):
        await self.page.set_content(fixture((21, 22)))
        async def reload_fixture(**kwargs):
            await self.page.set_content(fixture())
        self.page.reload.side_effect = reload_fixture
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [23])
        self.page.reload.assert_awaited_once()

    async def test_two_cards_support_explicit_next_day_plan(self):
        await self.page.set_content(fixture((21, 22)))
        await self.app._select_target_date(self.page, replace(PLAN, target_date=date(2026, 10, 22)))
        self.assertEqual(await self.page.evaluate("clicked"), [22])
        self.page.reload.assert_not_awaited()

    async def test_dom_order_does_not_choose_wrong_date(self):
        await self.page.set_content(fixture((23, 21, 22)))
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [23])

    async def test_active_date_does_not_require_another_response(self):
        await self.page.set_content(fixture(active=23))
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [])

    async def test_initial_loading_finishes_before_window_check(self):
        await self.page.set_content(fixture((21, 22)))
        await self.page.evaluate("""() => {
            document.querySelector('.loading').style.display='block';
            setTimeout(()=>{
                document.querySelector('.date_box').insertAdjacentHTML('beforeend',
                    '<div onclick="choose(this,23)"><span>10月23日</span></div>');
                document.querySelector('.loading').style.display='none';
            },200);
        }""")
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [23])
        self.page.reload.assert_not_awaited()

    async def test_target_table_update_is_awaited(self):
        await self.page.set_content(fixture(delay=200))
        await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.locator('.date_box > .active span').inner_text(), "10月23日")
        self.assertFalse(await self.page.locator('.loading').is_visible())

    async def test_duplicate_target_stops_without_click(self):
        await self.page.set_content(fixture((21, 23, 23)))
        with self.assertRaisesRegex(CalibrationRequired, "重复"):
            await self.app._select_target_date(self.page, PLAN)
        self.assertEqual(await self.page.evaluate("clicked"), [])

    async def test_authentication_error_is_preserved(self):
        await self.page.set_content('<div role="dialog">Token 已过期，请重新登录</div>')
        with self.assertRaises(AuthenticationExpired):
            await self.app._select_target_date(self.page, PLAN)
        self.page.reload.assert_not_awaited()

    async def test_data_error_is_preserved(self):
        await self.page.set_content('<div role="dialog">返回数据格式不正确</div>')
        with self.assertRaises(BookingPageLoadError):
            await self.app._select_target_date(self.page, PLAN)
        self.page.reload.assert_not_awaited()

    async def test_empty_control_is_loading_error_not_closed_window(self):
        await self.page.set_content('<div class="date_box"></div>')
        with self.assertRaisesRegex(CalibrationRequired, "等待日期控件和初始预约表格加载失败"):
            await self.app._select_target_date(self.page, PLAN)
        self.page.reload.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
