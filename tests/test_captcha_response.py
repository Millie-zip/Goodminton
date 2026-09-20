from __future__ import annotations

import base64

import pytest

from buaa_booking.captcha import AutoCaptchaUnavailable, decode_clickword_response


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
