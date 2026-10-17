from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import NotificationConfig
from .secrets import SecretStore


class NotificationError(RuntimeError):
    pass


class BarkRegistrationError(NotificationError):
    pass


def _raise_safe_bark_error(status_code: int, server_message: object) -> None:
    message = str(server_message or "").lower()
    if "device token" in message and ("failed to get" in message or "not found" in message):
        raise BarkRegistrationError(
            "Bark Device Key 尚未在当前服务器注册。请在 iPhone 打开 Bark，"
            "等待几秒后复制首页的完整测试地址，再运行 setup-secrets 覆盖旧 Key。"
        )
    raise NotificationError(
        f"Bark 拒绝了通知请求（HTTP {status_code}）。"
        "请检查 bark_server 与 Device Key 是否来自同一个 Bark 服务。"
    )


@dataclass(slots=True)
class BarkNotifier:
    config: NotificationConfig
    secrets: SecretStore

    def build_payload(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        silent: bool = False,
        url: str | None = None,
        url_title: str | None = None,
        image: str | None = None,
    ) -> dict[str, str | int]:
        if emergency and silent:
            raise ValueError("通知不能同时设置为紧急和静默")
        payload: dict[str, str | int] = {
            "device_key": self.secrets.get("bark_device_key", required=True) or "",
            "title": title,
            "body": message,
            "group": "北航羽毛球预约",
        }
        if silent:
            payload["level"] = "passive"
        elif emergency:
            payload["level"] = self.config.emergency_level
            payload["sound"] = self.config.emergency_sound
            payload["volume"] = self.config.emergency_volume
            if self.config.emergency_call:
                payload["call"] = "1"
        if url:
            payload["url"] = url
        if image:
            payload["image"] = image
        return payload

    def send(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        silent: bool = False,
        url: str | None = None,
        url_title: str | None = None,
        image: str | None = None,
    ) -> str | None:
        payload = self.build_payload(
            title,
            message,
            emergency=emergency,
            silent=silent,
            url=url,
            url_title=url_title,
            image=image,
        )
        endpoint = f"{self.config.bark_server.rstrip('/')}/push"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_body = {}
            try:
                _raise_safe_bark_error(exc.code, error_body.get("message") or exc.reason)
            except NotificationError as error:
                raise error from exc
        except urllib.error.URLError as exc:
            # 原始异常可能带请求 URL；只记录类型和数字错误码，避免泄露 Device Key。
            reason = exc.reason
            error_code = getattr(reason, "errno", None)
            detail = type(reason).__name__
            if isinstance(error_code, int):
                detail += f", errno={error_code}"
            raise NotificationError(f"Bark 网络连接失败（{detail}）") from exc
        except TimeoutError as exc:
            raise NotificationError("Bark 网络请求超时") from exc
        except json.JSONDecodeError as exc:
            raise NotificationError("Bark 返回了无效 JSON 响应") from exc
        if body.get("code") != 200:
            _raise_safe_bark_error(int(body.get("code") or 400), body.get("message"))
        timestamp = body.get("timestamp")
        return str(timestamp) if timestamp is not None else None


@dataclass(slots=True)
class ConsoleNotifier:
    def send(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        silent: bool = False,
        url: str | None = None,
        url_title: str | None = None,
        image: str | None = None,
    ) -> str | None:
        level = "紧急" if emergency else "静默通知" if silent else "通知"
        print(f"[{level}] {title}: {message}")
        if url:
            print(f"{url_title or '打开'}: {url}")
        if image:
            print(f"Bark image: {image}")
        return None
