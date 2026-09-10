from buaa_booking.config import NotificationConfig
from buaa_booking.notifier import PushoverNotifier


class FakeSecrets:
    values = {
        "pushover_app_token": "app-token",
        "pushover_user_key": "user-key",
    }

    def get(self, key: str, *, required: bool = False) -> str | None:
        return self.values.get(key)


def test_emergency_payload_requires_acknowledgement_fields() -> None:
    config = NotificationConfig(
        provider="pushover",
        remote_control_url="https://example.test/remote",
        emergency_retry_seconds=30,
        emergency_expire_seconds=600,
    )
    notifier = PushoverNotifier(config, FakeSecrets())  # type: ignore[arg-type]
    payload = notifier.build_payload("验证码", "请处理", emergency=True)
    assert payload["priority"] == "2"
    assert payload["retry"] == "30"
    assert payload["expire"] == "600"
