import json

from buaa_booking.config import NotificationConfig
import pytest

from buaa_booking.notifier import BarkNotifier, BarkRegistrationError, _raise_safe_bark_error


class FakeSecrets:
    values = {
        "bark_device_key": "device-key",
    }

    def get(self, key: str, *, required: bool = False) -> str | None:
        return self.values.get(key)


def test_emergency_payload_uses_bark_alarm_fields() -> None:
    config = NotificationConfig(
        provider="bark",
        bark_server="https://api.day.app",
        remote_control_url="https://example.test/remote",
        emergency_level="critical",
        emergency_sound="alarm",
        emergency_volume=10,
        emergency_call=True,
    )
    notifier = BarkNotifier(config, FakeSecrets())  # type: ignore[arg-type]
    payload = notifier.build_payload(
        "验证码",
        "请处理",
        emergency=True,
        url="https://example.test/remote",
    )
    assert payload["device_key"] == "device-key"
    assert payload["level"] == "critical"
    assert payload["sound"] == "alarm"
    assert payload["volume"] == 10
    assert payload["call"] == "1"
    assert payload["url"] == "https://example.test/remote"


def test_normal_payload_does_not_ring_continuously() -> None:
    config = NotificationConfig(
        provider="bark",
        bark_server="https://api.day.app",
        remote_control_url="https://example.test/remote",
        emergency_level="critical",
        emergency_sound="alarm",
        emergency_volume=10,
        emergency_call=True,
    )
    notifier = BarkNotifier(config, FakeSecrets())  # type: ignore[arg-type]
    payload = notifier.build_payload("提醒", "设置明早计划")
    assert "call" not in payload
    assert "level" not in payload


def test_bark_device_key_is_sent_in_json_not_url(monkeypatch) -> None:
    config = NotificationConfig(
        provider="bark",
        bark_server="https://api.day.app",
        remote_control_url="https://example.test/remote",
        emergency_level="critical",
        emergency_sound="alarm",
        emergency_volume=10,
        emergency_call=True,
    )
    notifier = BarkNotifier(config, FakeSecrets())  # type: ignore[arg-type]
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"code":200,"message":"success","timestamp":123}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    receipt = notifier.send("测试", "正文")
    assert captured["url"] == "https://api.day.app/push"
    assert "device-key" not in captured["url"]
    assert captured["body"]["device_key"] == "device-key"
    assert captured["timeout"] == 15
    assert receipt == "123"


def test_device_token_registration_error_does_not_echo_server_message() -> None:
    server_message = "failed to get [secret-device-key] device token from database"
    with pytest.raises(BarkRegistrationError) as error:
        _raise_safe_bark_error(400, server_message)
    assert "注册" in str(error.value)
    assert "secret-device-key" not in str(error.value)
