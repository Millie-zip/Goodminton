"""Offline regression suite. No account, database, notification or real booking."""
import asyncio
import ast
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from buaa_booking.automation import (
    BookingAutomation, BookingError, CalibrationRequired, LocatedCandidate,
    SlotSelection, SYSTEM_BROWSER_EXECUTABLES,
)
from buaa_booking.config import load_config
from buaa_booking.models import DailyPlan, SlotCandidate
from playwright.async_api import async_playwright

PLAN = DailyPlan(date(2026, 10, 20), date(2026, 10, 22), True, "21:00", "22:00")

FIXTURE = r"""
<style>td{width:180px;height:40px}.reserveBlock{height:40px}
.free{background:#8ad16b}.selected_box i{display:inline-block;width:30px;height:24px}</style>
<div class="spaceTable"></div><div class="selected_box"></div>
<script>
window.hours=[17,18,19,20,21]; window.courts=[1,2]; window.pageSize=5; window.offset=0;
window.states={}; window.selected=[]; window.clicked=[]; window.failHour=null; window.delay=0;
window.price='35.00';
function label(h){return `${h}:00-${h+1}:00`}
function key(c,h){return `${c}|${h}`}
function renderSelected(){
 document.querySelector('.selected_box').innerHTML='<h5>已选</h5><div class="reserve-msg-content">'+
 selected.map(([c,h],i)=>`<div class="reserve-msg-row"><i class="ivu-icon-ios-close-circle"
 onclick="selected.splice(${i},1);renderSelected()"></i><span>${c}号：${label(h)} ￥${price}</span></div>`).join('')+'</div>';
}
function choose(c,h){
 clicked.push([c,h]);
 if(h===failHour){states[key(c,h)]='reserved';render();return}
 if((states[key(c,h)]||'free')!=='free')return;
 const update=()=>{selected.push([c,h]);renderSelected()};
 if(delay)setTimeout(update,delay);else update();
}
function move(n){offset+=n*pageSize;render()}
function render(){
 const visible=hours.slice(offset,offset+pageSize);
 const header='<tr><td>场地</td>'+visible.map((h,i)=>`<td>
 ${i===0&&offset>0?'<i class="ivu-icon-ios-arrow-back" onclick="move(-1)"></i>':''}
 <span>${label(h)}</span>
 ${i===visible.length-1&&offset+pageSize<hours.length?'<i class="ivu-icon-ios-arrow-forward" onclick="move(1)"></i>':''}
 </td>`).join('')+'</tr>';
 const rows=courts.map(c=>`<tr><td>${c}号</td>`+visible.map((h,i)=>
 `<td colspan="${i===visible.length-1?2:1}"><div class="reserveBlock position ${states[key(c,h)]||'free'}"
 onclick="choose(${c},${h})">￥25.00</div></td>`).join('')+'</tr>').join('');
 document.querySelector('.spaceTable').innerHTML='<table><thead>'+header+'</thead><tbody>'+rows+
 '</tbody><tfoot>'+header+'</tfoot></table>';
 renderSelected();
}
render();
</script>
"""


def automation(output_dir):
    app = object.__new__(BookingAutomation)
    app.config = load_config(ROOT / "config/booking.example.json")
    app.artifacts_dir = Path(output_dir)
    app.store = SimpleNamespace(add_event=Mock())
    app._set_stage = Mock()
    return app


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = automation(self.tmp.name)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        executable = next((p for p in SYSTEM_BROWSER_EXECUTABLES if p.is_file()), None)
        self.browser = await self.pw.chromium.launch(
            headless=True, timeout=20000,
            **({"executable_path": str(executable)} if executable else {}),
        )
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        await self.page.set_content(FIXTURE)

    async def collect(self, plan=PLAN):
        return await self.app._collect_page_candidates("38", self.page, plan)

    async def test_last_column_and_previous_column(self):
        for courts in (list(range(1, 13)), list(range(17, 25))):
            await self.page.evaluate("(courts)=>{window.courts=courts;render()}", courts)
            found = await self.collect()
            control = await self.collect(replace(PLAN, start_time="20:00", end_time="21:00"))
            self.assertEqual(len(found), len(courts))
            self.assertEqual(len(control), len(courts))
            self.assertTrue(await self.app._select_candidate_slots(found[0]))
            self.assertEqual(await self.app._selected_slots(self.page), {SlotSelection(f"{courts[0]}号", "21:00-22:00")})

    async def test_two_hours_with_trailing_column(self):
        found = await self.collect(replace(PLAN, start_time="20:00", end_time="22:00"))
        self.assertEqual(len(found), 4)
        self.assertEqual(len({s.court for s in found[0].selections}), 1)
        self.assertTrue(await self.app._select_candidate_slots(found[0]))
        self.assertEqual(await self.app._selected_slots(self.page), set(found[0].selections))

    async def test_two_hours_across_pages(self):
        await self.page.evaluate("()=>{hours=[15,16,17,18];pageSize=2;render()}")
        found = await self.collect(replace(PLAN, start_time="16:00", end_time="18:00"))
        self.assertTrue(await self.app._select_candidate_slots(found[0]))
        self.assertEqual(await self.app._selected_slots(self.page), set(found[0].selections))
        self.assertEqual(await self.page.evaluate("clicked"), [[1, 16], [1, 17]])

    async def test_selected_row_prices_do_not_prefix_the_next_court(self):
        for courts, price in (([7, 7], "35.00"), ([7, 17], "25.50"), ([12, 24], "0.01")):
            await self.page.evaluate("""([courts, price])=>{
                window.price=price;selected=[[courts[0],16],[courts[1],17]];renderSelected();
            }""", [courts, price])
            self.assertEqual(await self.app._selected_slots(self.page), {
                SlotSelection(f"{courts[0]}号", "16:00-17:00"),
                SlotSelection(f"{courts[1]}号", "17:00-18:00"),
            })

    async def test_combined_selected_range_expands_to_hourly_slots(self):
        await self.page.locator(".selected_box").evaluate("""el=>{
            el.innerHTML='<div class="reserve-msg-row"><span>7号场：16:00-18:00 ￥70.00</span></div>';
        }""")
        self.assertEqual(await self.app._selected_slots(self.page), {
            SlotSelection("7号", "16:00-17:00"), SlotSelection("7号", "17:00-18:00"),
        })

    async def test_hidden_empty_selected_container_is_empty(self):
        await self.page.locator(".selected_box").evaluate("""el=>{
            el.style.display='none';el.innerHTML='<h5>已选</h5><div class="reserve-msg-content"></div>';
        }""")
        self.assertEqual(await self.app._selected_slots(self.page), set())

    async def test_missing_selected_container_stops_calibration(self):
        await self.page.locator(".selected_box").evaluate("el=>el.remove()")
        with self.assertRaises(CalibrationRequired):
            await self.app._selected_slots(self.page)

    async def test_selected_text_without_rows_stops_calibration(self):
        await self.page.locator(".selected_box").evaluate("""el=>{
            el.innerHTML='<span>7号：16:00-17:00 ￥35.00</span>';
        }""")
        with self.assertRaises(CalibrationRequired):
            await self.app._selected_slots(self.page)

    async def test_malformed_selected_rows_stop_calibration(self):
        for text in ("未知场地", "7号：待确认", "7号：16:30-17:00 ￥35.00",
                     "7号：17:00-16:00 ￥35.00", "7号：16:00-17:00 8号：17:00-18:00"):
            await self.page.locator(".selected_box").evaluate("""(el,text)=>{
                el.innerHTML='<div class="reserve-msg-row"></div>';el.firstChild.textContent=text;
            }""", text)
            with self.subTest(text=text), self.assertRaises(CalibrationRequired):
                await self.app._selected_slots(self.page)

    async def test_morning_two_hour_selection_reaches_submit_guard(self):
        plan = replace(PLAN, start_time="16:00", end_time="18:00")
        await self.page.evaluate("""()=>{
            hours=[12,13,14,15,16,17,18,19,20,21];pageSize=5;
            courts=Array.from({length:12},(_,i)=>i+1);
            states={'1|16':'reserved','2|16':'reserved'};render();
        }""")
        self.app._select_first_companion = AsyncMock()
        self.app._accept_terms = AsyncMock()
        submit = SimpleNamespace(click=AsyncMock())
        self.app._wait_for_ready_submit = AsyncMock(return_value=submit)
        self.app._confirm_submit = AsyncMock(side_effect=StopBeforeSubmit())
        async with asyncio.timeout(3):
            found = await self.collect(plan)
            with self.assertRaises(StopBeforeSubmit):
                await self.app._attempt_candidates(found, plan, {"38": self.page})
        self.assertEqual(await self.page.evaluate("clicked"), [[3, 16], [3, 17]])
        self.app._confirm_submit.assert_awaited_once()
        submit.click.assert_not_awaited()

    async def test_blocked_states_with_prices_are_not_selected(self):
        for state in ("disabled", "reservation", "reserved", "newReservation"):
            await self.page.evaluate("(state)=>{states={'1|21':state,'2|21':state};render()}", state)
            self.assertEqual(await self.collect(), [])
        records = Path(self.tmp.name, "candidate-snapshots.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("￥25.00", records)
        self.assertIn("不可预约状态", records)

    async def test_unknown_state_is_calibration_error(self):
        await self.page.evaluate("()=>{states={'1|21':'mystery'};render()}")
        with self.assertRaises(CalibrationRequired):
            await self.collect()

    async def test_hidden_or_aria_disabled_price_cells_are_excluded(self):
        await self.page.evaluate("""()=>{
          const blocks=[...document.querySelectorAll('tbody tr')].map(r=>r.lastElementChild.firstElementChild);
          blocks[0].style.visibility='hidden'; blocks[1].setAttribute('aria-disabled','true');
        }""")
        self.assertEqual(await self.collect(), [])

    async def test_multiple_blocks_in_one_cell_are_not_single_court(self):
        await self.page.evaluate("""()=>{
          const cell=document.querySelector('tbody tr').lastElementChild;
          cell.appendChild(cell.firstElementChild.cloneNode(true));
        }""")
        with self.assertRaises(CalibrationRequired):
            await self.collect()

    async def test_empty_slot_does_not_block_other_courts(self):
        await self.page.evaluate("()=>document.querySelector('tbody tr').lastElementChild.innerHTML=''")
        found = await self.collect()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].selections[0].court, "2号")

    async def test_middle_merged_price_cell_is_rejected(self):
        await self.page.evaluate("""()=>{
          const row=document.querySelector('tbody tr');
          row.cells[4].colSpan=2; row.cells[5].remove();
        }""")
        with self.assertRaises(CalibrationRequired):
            await self.collect()

    async def test_blocked_merge_does_not_shift_later_column(self):
        await self.page.evaluate("""()=>{
          const row=document.querySelector('tbody tr');
          row.cells[1].colSpan=2;
          row.cells[1].querySelector('.reserveBlock').className='reserveBlock disabled';
          row.cells[2].remove();
        }""")
        found = await self.collect()
        self.assertEqual(len(found), 2)
        self.assertTrue(await self.app._select_candidate_slots(found[0]))
        self.assertEqual(await self.page.evaluate("clicked"), [[1, 21]])

    async def test_missing_court_rows_are_not_no_availability(self):
        await self.page.evaluate("()=>{courts=['unknown'];render()}")
        with self.assertRaisesRegex(CalibrationRequired, "未解析到场地行"):
            await self.collect()

    async def test_duplicate_header_is_error(self):
        await self.page.evaluate("()=>document.querySelector('thead tr').cells[4].textContent='21:00-22:00'")
        with self.assertRaises(CalibrationRequired):
            await self.collect()

    async def test_missing_target_header_is_error(self):
        await self.page.evaluate("()=>{hours=[17,18];render()}")
        with self.assertRaisesRegex(CalibrationRequired, "没有显示目标时段"):
            await self.collect()

    async def test_stale_candidate_skipped_before_click(self):
        found = await self.collect()
        await self.page.evaluate("()=>{states['1|21']='reserved';render()}")
        self.assertFalse(await self.app._select_candidate_slots(found[0]))
        self.assertTrue(await self.app._select_candidate_slots(found[1]))
        self.assertEqual(await self.page.evaluate("clicked"), [[2, 21]])

    async def test_second_hour_fails_and_first_is_cleared_promptly(self):
        found = await self.collect(replace(PLAN, start_time="20:00", end_time="22:00"))
        await self.page.evaluate("()=>{failHour=21}")
        # A regression to the old 3-second polling path fails this outer timeout.
        async with asyncio.timeout(2):
            self.assertFalse(await self.app._select_candidate_slots(found[0]))
        self.assertEqual(await self.app._selected_slots(self.page), set())

    async def test_delayed_selection_is_awaited(self):
        found = await self.collect()
        await self.page.evaluate("()=>{delay=150}")
        self.assertTrue(await self.app._select_candidate_slots(found[0]))

    async def test_loading_waits_for_new_table(self):
        await self.page.evaluate("""()=>{
          document.body.insertAdjacentHTML('afterbegin','<div class="loading ivu-spin">loading</div>');
          states={'1|21':'reserved','2|21':'reserved'};render();
          setTimeout(()=>{states={};render();document.querySelector('.loading').style.display='none'},150);
        }""")
        self.assertEqual(len(await self.collect()), 2)

    async def test_standalone_conflict_detector_and_no_automatic_call(self):
        await self.page.set_content("<div role='dialog'>该场地已被预订</div>")
        self.assertTrue(await self.app._detect_submit_conflict(self.page))
        self.app._detect_submit_conflict = AsyncMock(side_effect=AssertionError("must stay dormant"))
        self.app._recover_submit_conflict = AsyncMock(side_effect=AssertionError("must stay dormant"))
        self.app.config = replace(self.app.config, browser=replace(
            self.app.config.browser, post_submit_detection_timeout_seconds=0.01))
        self.app._post_submit_monitor = {"captcha_seen": {"value": False}, "response_task": None, "handler": lambda r: None}
        self.app._has_companion_required_error = AsyncMock(return_value=False)
        self.app._is_payment_page = AsyncMock(return_value=False)
        self.assertIsNone(await self.app._wait_for_post_submit_state(self.page))
        self.app._detect_submit_conflict.assert_not_awaited()
        self.app._recover_submit_conflict.assert_not_awaited()


class StopBeforeSubmit(Exception):
    pass


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = automation(self.tmp.name)
        self.app.config = replace(self.app.config, browser=replace(
            self.app.config.browser, availability_refresh_interval_seconds=0))
        self.page = SimpleNamespace(bring_to_front=AsyncMock(), wait_for_timeout=AsyncMock())
        self.candidate = LocatedCandidate(SlotCandidate("38", 1, "1号"), self.page, "unused",
            selections=(SlotSelection("1号", "21:00-22:00"),))
        self.app._restart_booking_pages = AsyncMock()
        self.app._collect_candidates = AsyncMock(return_value=[])
        self.app._select_first_companion = AsyncMock()
        self.app._accept_terms = AsyncMock()
        self.submit = SimpleNamespace(click=AsyncMock())
        self.app._wait_for_ready_submit = AsyncMock(return_value=self.submit)
        self.app._selected_slots = AsyncMock(return_value=set(self.candidate.selections))
        self.app._confirm_submit = AsyncMock(side_effect=StopBeforeSubmit())

    async def test_exhausted_candidates_refresh_and_select_new_one(self):
        self.app._select_candidate_slots = AsyncMock(side_effect=[False, True])
        self.app._collect_candidates.return_value = [self.candidate]
        with self.assertRaises(StopBeforeSubmit):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page})
        self.app._restart_booking_pages.assert_awaited_once()
        self.app._collect_candidates.assert_awaited_once()
        self.submit.click.assert_not_awaited()

    async def test_empty_refreshes_stop_at_budget(self):
        self.app._select_candidate_slots = AsyncMock(return_value=False)
        with self.assertRaisesRegex(BookingError, "次数已用尽"):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page})
        self.assertEqual(self.app._restart_booking_pages.await_count, 2)
        self.app._confirm_submit.assert_not_awaited()

    async def test_selection_deadline(self):
        async def slow(*args):
            await asyncio.sleep(1)
        self.app._select_candidate_slots = slow
        with self.assertRaisesRegex(BookingError, "总时限"):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page},
                deadline=asyncio.get_running_loop().time() + 0.02)
        self.app._confirm_submit.assert_not_awaited()
        self.app._restart_booking_pages.assert_not_awaited()

    async def test_collection_deadline(self):
        async def slow(*args):
            await asyncio.sleep(1)
        self.app._collect_candidates = slow
        with self.assertRaisesRegex(BookingError, "读取可预约场地超时"):
            await self.app._load_candidates_with_refresh({"38": self.page}, PLAN,
                deadline=asyncio.get_running_loop().time() + 0.02)

    async def test_refresh_deadline(self):
        self.app._select_candidate_slots = AsyncMock(return_value=False)
        async def slow(*args):
            await asyncio.sleep(1)
        self.app._restart_booking_pages = slow
        with self.assertRaisesRegex(BookingError, "重新扫描可预约场地超时"):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page},
                deadline=asyncio.get_running_loop().time() + 0.02)
        self.app._confirm_submit.assert_not_awaited()

    async def test_initial_empty_scan_then_available(self):
        self.app._collect_candidates.side_effect = [[], [self.candidate]]
        found = await self.app._load_candidates_with_refresh({"38": self.page}, PLAN)
        self.assertEqual(found, [self.candidate])
        self.app._restart_booking_pages.assert_awaited_once()
        self.assertEqual(self.app._set_stage.call_args.args[0], "检测场地")

    async def test_calibration_failure_is_not_retried(self):
        self.app._collect_candidates.side_effect = CalibrationRequired("bad table")
        with self.assertRaises(CalibrationRequired):
            await self.app._load_candidates_with_refresh({"38": self.page}, PLAN)
        self.app._restart_booking_pages.assert_not_awaited()

    async def test_unknown_submit_result_does_not_retry(self):
        self.app._select_candidate_slots = AsyncMock(return_value=True)
        self.app._confirm_submit = AsyncMock()
        self.app._start_post_submit_monitor = Mock()
        self.app._wait_for_post_submit_state = AsyncMock(return_value=None)
        self.app._save_screenshot = AsyncMock()
        self.app._recover_submit_conflict = AsyncMock()
        with self.assertRaisesRegex(BookingError, "提交后的页面状态无法确认"):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page})
        self.submit.click.assert_awaited_once()
        self.app._restart_booking_pages.assert_not_awaited()
        self.app._recover_submit_conflict.assert_not_awaited()

    async def test_partial_selection_never_submits(self):
        self.app._select_candidate_slots = AsyncMock(return_value=True)
        self.app._selected_slots.return_value = set()
        self.app._clear_selected_slots = AsyncMock()
        with self.assertRaises(BookingError):
            await self.app._attempt_candidates([self.candidate], PLAN, {"38": self.page})
        self.app._confirm_submit.assert_not_awaited()
        self.submit.click.assert_not_awaited()

    async def test_dormant_conflict_recovery_can_be_called_explicitly(self):
        self.app._collect_candidates.return_value = [self.candidate]
        self.assertEqual(await self.app._recover_submit_conflict({"38": self.page}, PLAN), [self.candidate])
        self.app._restart_booking_pages.assert_awaited_once()
        source = ast.parse((ROOT / "src/buaa_booking/automation.py").read_text(encoding="utf-8"))
        calls = [n.func.attr for n in ast.walk(source) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertNotIn("_recover_submit_conflict", calls)
        self.assertNotIn("_detect_submit_conflict", calls)

    async def test_timeout_config_backward_compatibility_and_validation(self):
        raw = json.loads((ROOT / "config/booking.example.json").read_text(encoding="utf-8"))
        raw["browser"].pop("availability_timeout_seconds", None)
        config_path = Path(self.tmp.name, "config.json")
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(load_config(config_path).browser.availability_timeout_seconds, 15)
        for value in (0, -1, float("nan"), float("inf")):
            raw["browser"]["availability_timeout_seconds"] = value
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
