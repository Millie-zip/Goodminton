from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import NotificationConfig
from .secrets import SecretStore


PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"


class NotificationError(RuntimeError):
    pass


@dataclass(slots=True)
class PushoverNotifier:
    config: NotificationConfig
    secrets: SecretStore

    def build_payload(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        url: str | None = None,
        url_title: str | None = None,
    ) -> dict[str, str]:
        payload = {
            "token": self.secrets.get("pushover_app_token", required=True) or "",
            "user": self.secrets.get("pushover_user_key", required=True) or "",
            "title": title,
            "message": message,
            "priority": "2" if emergency else "0",
        }
        if emergency:
            payload["retry"] = str(self.config.emergency_retry_seconds)
            payload["expire"] = str(self.config.emergency_expire_seconds)
        if url:
            payload["url"] = url
            payload["url_title"] = url_title or "打开"
        return payload

    def send(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        url: str | None = None,
        url_title: str | None = None,
    ) -> str | None:
        payload = self.build_payload(
            title,
            message,
            emergency=emergency,
            url=url,
            url_title=url_title,
        )
        request = urllib.request.Request(
            PUSHOVER_ENDPOINT,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NotificationError("Pushover 通知发送失败") from exc
        if body.get("status") != 1:
            raise NotificationError("Pushover 拒绝了通知请求")
        receipt = body.get("receipt")
        return str(receipt) if receipt else None


@dataclass(slots=True)
class ConsoleNotifier:
    def send(
        self,
        title: str,
        message: str,
        *,
        emergency: bool = False,
        url: str | None = None,
        url_title: str | None = None,
    ) -> str | None:
        level = "紧急" if emergency else "通知"
        print(f"[{level}] {title}: {message}")
        if url:
            print(f"{url_title or '打开'}: {url}")
        return None

