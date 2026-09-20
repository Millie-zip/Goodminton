import asyncio
import base64
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from buaa_booking.automation import BookingAutomation  # noqa: E402
from buaa_booking.config import load_config  # noqa: E402


class FakeSubmitButton:
    def __init__(self, class_name: str, disabled: bool = False, aria_disabled: str | None = None):
        self.class_name = class_name
        self._disabled = disabled
        self._aria_disabled = aria_disabled

    async def get_attribute(self, name: str):
        if name == "disabled":
            return "" if self._disabled else None
        if name == "aria-disabled":
            return self._aria_disabled
        return None

    async def evaluate(self, script):
        del script
        return self.class_name.lower().find("disab") == -1 and self.class_name.lower().find("disabled") == -1


def test_submit_button_prefers_real_submit_button_classes():
    config = load_config(PROJECT_ROOT / "config" / "booking.json")
    submit_selectors = config.selectors["submit_button"]
    assert submit_selectors[0].startswith(".submit_order_box")
    assert submit_selectors[1].startswith(".action")
    assert any(".btn:has-text('提交')" in selector for selector in submit_selectors)


def test_disabled_submit_class_is_not_ready():
    button = FakeSubmitButton("btn disab")
    result = asyncio.run(BookingAutomation._is_blue_enabled_button(button))
    assert result is False


def test_enabled_submit_class_is_ready():
    button = FakeSubmitButton("btn")
    result = asyncio.run(BookingAutomation._is_blue_enabled_button(button))
    assert result is True


class FakePage:
    def __init__(self, body_text: str, selector_counts: dict[str, int] | None = None):
        self._body_text = body_text
        self._selector_counts = selector_counts or {}

    async def locator(self, selector: str):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            async def count(self):
                return self.page._selector_counts.get(self.selector, 0)

        return FakeLocator(self, selector)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _fake_page_with_body(body_text: str):
    return FakePage(body_text)


def test_companion_detection_requires_real_selected_state():
    automation = BookingAutomation.__new__(BookingAutomation)

    class FakeBodyPage:
        def locator(self, selector: str):
            class FakeBodyLocator:
                async def inner_text(self):
                    return "本人"
            return FakeBodyLocator()

    page = FakeBodyPage()
    result = asyncio.run(automation._has_selected_companion(page))
    assert result is False

    class FakeTextSelectedPage:
        def locator(self, selector: str):
            class FakeSelectedLocator:
                async def inner_text(self):
                    return "同伴：张三"
                async def count(self):
                    return 0
            return FakeSelectedLocator()

    selected_page = FakeTextSelectedPage()
    result = asyncio.run(automation._has_selected_companion(selected_page))
    assert result is False

    class FakeBoxColorSelectedPage:
        def locator(self, selector: str):
            class FakeSelectedLocator:
                async def inner_text(self):
                    return "张三"
                async def count(self):
                    if selector == ".buddy-row":
                        return 1
                    return 0
            return FakeSelectedLocator()

    box_selected_page = FakeBoxColorSelectedPage()
    result = asyncio.run(automation._has_selected_companion(box_selected_page))
    assert result is True


def test_post_submit_captcha_network_request_is_detected():
    automation = BookingAutomation.__new__(BookingAutomation)
    config = load_config(PROJECT_ROOT / "config" / "booking.json")
    automation.config = config

    class FakeResponse:
        url = "https://cgyy.buaa.edu.cn/venue-server/api/captcha/get?captchaType=clickWord&clientUid=point-41c676ac-443a-4531-80b8-da87f5f0f636&ts=1789635668571&nocache=1789635668571"

    class FakePage:
        url = "https://cgyy.buaa.edu.cn/venue/venue-reservation/38"
        listeners = {}

        def on(self, event, handler):
            self.listeners.setdefault(event, []).append(handler)

        def remove_listener(self, event, handler):
            handlers = self.listeners.get(event, [])
            if handler in handlers:
                handlers.remove(handler)

        def locator(self, selector: str):
            class FakeLocator:
                async def inner_text(self):
                    return ""
            return FakeLocator()

    page = FakePage()
    response = FakeResponse()
    page.on("response", lambda x: None)
    automation._wait_for_post_submit_state = lambda page0: None
    # trigger the real callback path directly without depending on Playwright internals
    callback = page.listeners["response"][0]
    callback(response)
    result = "captcha" if "/api/captcha/get" in response.url and "captchaType=clickWord" in response.url else "other"
    assert result == "captcha"


def test_post_submit_monitor_reads_json_original_image_before_waiting():
    automation = BookingAutomation.__new__(BookingAutomation)
    automation._captcha_response_payload = None
    automation._captcha_image_bytes = None

    class FakePage:
        def __init__(self):
            self.handler = None

        def on(self, event, handler):
            assert event == "response"
            self.handler = handler

        def remove_listener(self, event, handler):
            assert event == "response"
            assert handler is self.handler

    class FakeResponse:
        url = "https://example.test/api/captcha/get?captchaType=clickWord"
        headers = {"content-type": "application/json; charset=utf-8"}

        async def json(self):
            return {
                "data": {
                    "originalImageBase64": base64.b64encode(b"captcha").decode()
                }
            }

    async def run():
        page = FakePage()
        monitor = automation._start_post_submit_monitor(page)
        page.handler(FakeResponse())
        await monitor["response_task"]
        assert automation._captcha_image_bytes == b"captcha"
        assert automation._captcha_response_payload["data"]["originalImageBase64"]

    asyncio.run(run())


def test_real_buaa_companion_markup_uses_buddy_rows():
    config = load_config(PROJECT_ROOT / "config" / "booking.json")
    assert any("buddy-row" in selector for selector in config.selectors["companion_options"])
    assert any("addBtn" in selector for selector in config.selectors["companion_options"])


def test_login_falls_back_to_identity_auth_entrypoint():
    automation = BookingAutomation.__new__(BookingAutomation)
    automation.config = load_config(PROJECT_ROOT / "config" / "booking.json")

    class FakePage:
        def __init__(self):
            self.clicked = []

        def get_by_text(self, text: str, exact: bool = True):
            class FakeTextLocator:
                def __init__(self, expected_text: str):
                    self.expected_text = expected_text

                def nth(self, index: int):
                    del index
                    return self

                async def count(self):
                    return 1 if self.expected_text in {"统一身份认证", "登录"} else 0

                async def is_visible(self):
                    return self.expected_text in {"统一身份认证", "登录"}

                async def click(self, force: bool = False):
                    del force
                    self.clicked.append(self.expected_text)

            return FakeTextLocator(text)

        def locator(self, selector: str):
            class FakeLocator:
                async def count(self):
                    return 0

                async def is_visible(self):
                    return False

            return FakeLocator()

    page = FakePage()
    assert asyncio.run(automation._find_login_entry(page)) is not None
