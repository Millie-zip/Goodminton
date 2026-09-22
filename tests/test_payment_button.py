import inspect
import json
from pathlib import Path

from buaa_booking.automation import BookingAutomation, PAYMENT_BUTTON_FALLBACK_SELECTORS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_payment_selectors_match_dynamic_payment_label():
    assert "div.btn:not(.cancel):has-text('支付（')" in PAYMENT_BUTTON_FALLBACK_SELECTORS
    assert "div.btn:not(.cancel):has-text('支付(')" in PAYMENT_BUTTON_FALLBACK_SELECTORS
    assert "button:has-text('支付（')" in PAYMENT_BUTTON_FALLBACK_SELECTORS
    assert "button:has-text('支付(')" in PAYMENT_BUTTON_FALLBACK_SELECTORS
    assert "button:has-text('支付')" in PAYMENT_BUTTON_FALLBACK_SELECTORS
    assert "[role='button']:has-text('支付')" in PAYMENT_BUTTON_FALLBACK_SELECTORS

    for config_name in ("booking.example.json", "booking.debug.json"):
        config = json.loads((PROJECT_ROOT / "config" / config_name).read_text(encoding="utf-8"))
        selectors = config["selectors"]["payment_button"]
        assert "div.btn:not(.cancel):has-text('支付（')" in selectors
        assert "div.btn:not(.cancel):has-text('支付(')" in selectors
        assert "button:has-text('支付（')" in selectors
        assert "button:has-text('支付(')" in selectors
        assert "button:has-text('支付')" in selectors


def test_cross_frame_fallback_allows_enabled_filter():
    signature = inspect.signature(BookingAutomation._first_visible_across_frames_with_fallback)
    assert signature.parameters["require_enabled"].default is False
