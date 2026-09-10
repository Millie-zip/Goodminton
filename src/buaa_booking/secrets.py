from __future__ import annotations

import getpass
import secrets as secrets_module
from dataclasses import dataclass


SERVICE_NAME = "buaa-badminton-booking"


class SecretStoreError(RuntimeError):
    pass


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

    def interactive_setup(self) -> None:
        username = input("北航学号：").strip()
        password = getpass.getpass("统一身份认证密码（输入时不显示）：")
        self.set("buaa_username", username)
        self.set("buaa_password", password)

        configure_push = input("是否配置 Pushover？[Y/n]：").strip().lower()
        if configure_push not in {"n", "no"}:
            user_key = getpass.getpass("Pushover User Key：").strip()
            app_token = getpass.getpass("Pushover App Token：").strip()
            self.set("pushover_user_key", user_key)
            self.set("pushover_app_token", app_token)

        self.ensure_dashboard_token()

