"""Regression checks for automatic CAPTCHA failures and the response/DOM race."""
import asyncio
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
from buaa_booking.automation import BookingAutomation, BookingError
from buaa_booking.captcha import AutoCaptchaUnavailable
from buaa_booking.config import load_config
from buaa_booking.models import DailyPlan, SlotCandidate

PLAN = DailyPlan(date(2026,10,23),date(2026,10,25),True,"20:00","21:00")
SLOT = SlotCandidate("39",0,"CAPTCHA-only regression")


class AutomaticCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = object.__new__(BookingAutomation)
        config = load_config(ROOT / "config/booking.example.json")
        self.app.config = replace(config, browser=replace(config.browser,
            captcha_mode="auto",post_submit_detection_timeout_seconds=0.02))
        self.app.store = SimpleNamespace(add_event=Mock())
        self.app._set_stage = Mock()
        self.app._notify = Mock()
        self.app._find_payment_page = AsyncMock(return_value=None)
        self.app._wait_for_manual_captcha = AsyncMock()
        self.app.captcha_solver = SimpleNamespace(solve=AsyncMock(return_value=False),
            last_failure_reason="missing target")
        self.app.artifacts_dir = Path(tempfile.gettempdir())
        self.app._captcha_image_bytes = None
        self.app._captcha_response_payload = None
        self.app._captcha_word_list = None
        self.app._captcha_token = None
        self.app._captcha_secret_key = None

    async def check_no_manual_fallback(self):
        with self.assertRaisesRegex(BookingError,"人工点选已禁用"):
            await self.app._handle_captcha(Mock(),PLAN,SLOT)
        self.app._wait_for_manual_captcha.assert_not_awaited()
        self.app._notify.assert_not_called()

    async def test_missing_target_stops_without_manual_wait(self):
        await self.check_no_manual_fallback()
        self.assertIn("missing target",self.app.store.add_event.call_args.args[2])

    async def test_missing_dependency_stops_without_manual_wait(self):
        self.app.captcha_solver.solve.side_effect = AutoCaptchaUnavailable("dependency absent")
        await self.check_no_manual_fallback()

    async def test_unexpected_error_stops_without_manual_wait(self):
        self.app.captcha_solver.solve.side_effect = RuntimeError("OCR failed")
        await self.check_no_manual_fallback()

    async def test_submitted_clicks_are_not_assumed_success(self):
        self.app.captcha_solver.solve.return_value = True
        await self.check_no_manual_fallback()

    async def test_visible_captcha_waits_for_pending_response_body(self):
        self.app.config = replace(self.app.config,browser=replace(self.app.config.browser,
            post_submit_detection_timeout_seconds=0.5))
        async def response_body():
            await asyncio.sleep(0.01)
            self.app._captcha_response_payload = {"ready":True}
        task = asyncio.create_task(response_body())
        self.app._post_submit_monitor = {"captcha_seen":{"value":False},
            "response_task":task,"handler":Mock()}
        self.app._has_companion_required_error = AsyncMock(return_value=False)
        self.app._any_visible_across_frames = AsyncMock(return_value=True)
        self.app._is_payment_page = AsyncMock(return_value=False)
        state = await self.app._wait_for_post_submit_state(Mock())
        self.assertEqual(state,"captcha")
        self.assertFalse(task.cancelled())
        self.assertEqual(self.app._captcha_response_payload,{"ready":True})

    async def test_new_submit_does_not_reuse_previous_challenge_fields(self):
        self.app._captcha_word_list = ["old"]
        self.app._captcha_token = "old-token"
        self.app._captcha_secret_key = "old-key"
        self.app._start_post_submit_monitor(Mock())
        self.assertIsNone(self.app._captcha_word_list)
        self.assertIsNone(self.app._captcha_token)
        self.assertIsNone(self.app._captcha_secret_key)


if __name__ == "__main__":
    unittest.main()
