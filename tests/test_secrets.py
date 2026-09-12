import pytest

from buaa_booking.secrets import normalize_bark_device_key


def test_normalize_bark_device_key_from_full_test_url() -> None:
    assert (
        normalize_bark_device_key("https://api.day.app/device-key/测试")
        == "device-key"
    )


def test_normalize_bark_device_key_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="没有 Device Key"):
        normalize_bark_device_key("https://api.day.app/")
