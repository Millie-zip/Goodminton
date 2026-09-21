from __future__ import annotations

import base64
import asyncio

import pytest

from buaa_booking.captcha import (
    AutoCaptchaUnavailable,
    LocalClickCaptchaSolver,
    OCRChar,
    decode_clickword_response,
)


def test_decode_nested_original_image_base64():
    raw = b"captcha-image"
    encoded = base64.b64encode(raw).decode("ascii")

    assert decode_clickword_response({"data": {"originalImageBase64": encoded}}) == raw


def test_decode_data_url_original_image_base64():
    raw = b"captcha-image"
    encoded = base64.b64encode(raw).decode("ascii")

    assert decode_clickword_response(
        {"originalImageBase64": f"data:image/png;base64,{encoded}"}
    ) == raw


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"originalImageBase64": "not-base64"}},
    ],
)
def test_decode_rejects_missing_or_invalid_image(payload):
    with pytest.raises(AutoCaptchaUnavailable):
        decode_clickword_response(payload)


def test_find_image_locator_supports_aj_captcha_verify_panel():
    solver = LocalClickCaptchaSolver.__new__(LocalClickCaptchaSolver)
    solver.image_selector = "#captcha-image"

    class FakeCandidate:
        async def is_visible(self):
            return True

        async def bounding_box(self):
            return {"x": 10, "y": 20, "width": 330, "height": 155}

    class FakeLocator:
        def __init__(self, count):
            self._count = count

        async def count(self):
            return self._count

        def nth(self, index):
            del index
            return FakeCandidate()

    class FakePage:
        main_frame = None
        frames = []

        def locator(self, selector):
            return FakeLocator(1 if selector == ".verify-img-panel" else 0)

    result = asyncio.run(solver._find_image_locator(FakePage()))
    assert isinstance(result, FakeCandidate)


def test_ocr_chars_slice_removes_last_four_items():
    chars = [OCRChar(char=str(index), x=index, y=0, score=0.9) for index in range(8)]

    result = chars[:-4]

    assert [item.char for item in result] == ["0", "1", "2", "3"]
