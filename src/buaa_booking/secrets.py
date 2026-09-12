from __future__ import annotations

import getpass
import secrets as secrets_module
from dataclasses import dataclass
from urllib.parse import urlparse


SERVICE_NAME = "buaa-badminton-booking"


class SecretStoreError(RuntimeError):
    pass


def normalize_bark_device_key(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        parsed = urlparse(candidate)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            raise ValueError("Bark 测试地址中没有 Device Key")
        candidate = segments[0]
    if not candidate or any(character in candidate for character in "/?#&"):
        raise ValueError("Bark Device Key 格式不正确")
    return candidate


@dataclass(slots=True)
class SecretStore:
    service_name: str = SERVICE_NAME

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise SecretStoreError("缺少 keyring，请先安装项目依赖") from exc
        return keyring

    def get(self, key: str, *, required: bool = False) -> str | None:
        try:
            value = self._keyring().get_password(self.service_name, key)
        except Exception as exc:
            raise SecretStoreError(f"无法读取系统凭据：{key}") from exc
        if required and not value:
            raise SecretStoreError(f"系统凭据中缺少：{key}")
        return value

    def set(self, key: str, value: str) -> None:
        if not value:
            raise ValueError("不能保存空凭据")
        try:
            self._keyring().set_password(self.service_name, key, value)
        except Exception as exc:
            raise SecretStoreError(f"无法保存系统凭据：{key}") from exc

    def ensure_dashboard_token(self) -> str:
        current = self.get("dashboard_token")
        if current:
            return current
        token = secrets_module.token_urlsafe(32)
        self.set("dashboard_token", token)
        return token

    def interactive_setup(self, notification_provider: str = "bark") -> None:
        username = input("北航学号：").strip()
        password = getpass.getpass("统一身份认证密码（输入时不显示）：")
        self.set("buaa_username", username)
        self.set("buaa_password", password)

        if notification_provider.lower() == "bark":
            configure_push = input("是否配置免费的 Bark 通知？[Y/n]：").strip().lower()
            if configure_push not in {"n", "no"}:
                raw_key = getpass.getpass(
                    "Bark 测试地址或 Device Key（输入时不显示）："
                )
                device_key = normalize_bark_device_key(raw_key)
                self.set("bark_device_key", device_key)

        self.ensure_dashboard_token()
