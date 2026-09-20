from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .models import parse_clock, validate_time_window


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str
    port: int
    public_url: str


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    provider: str
    bark_server: str
    remote_control_url: str
    emergency_level: str
    emergency_sound: str
    emergency_volume: int
    emergency_call: bool


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    profile_dir: Path
    navigation_timeout_ms: int
    captcha_mode: str
    post_submit_detection_timeout_seconds: int
    manual_captcha_timeout_seconds: int
    payment_hold_seconds: int
    availability_refresh_count: int
    availability_refresh_interval_seconds: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    timezone: str
    days_ahead: int
    open_time: str
    startup_time: str
    headed_browser: bool
    home_url: str
    venue_urls: dict[str, str]
    venue_priority: tuple[str, ...]
    default_start_time: str
    default_end_time: str
    dashboard: DashboardConfig
    notification: NotificationConfig
    browser: BrowserConfig
    selectors: dict[str, tuple[str, ...]]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _required(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"配置缺少字段：{key}")
    return data[key]


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    timezone = str(_required(raw, "timezone"))
    ZoneInfo(timezone)
    days_ahead = int(_required(raw, "days_ahead"))
    if not 1 <= days_ahead <= 14:
        raise ValueError("days_ahead 必须在 1 到 14 之间")

    open_time = str(_required(raw, "open_time"))
    startup_time = str(_required(raw, "startup_time"))
    parse_clock(open_time)
    parse_clock(startup_time)

    start_time = str(_required(raw, "default_start_time"))
    end_time = str(_required(raw, "default_end_time"))
    validate_time_window(start_time, end_time)

    venue_urls = {str(k): str(v) for k, v in _required(raw, "venue_urls").items()}
    venue_priority = tuple(str(item) for item in _required(raw, "venue_priority"))
    if not venue_priority or any(item not in venue_urls for item in venue_priority):
        raise ValueError("venue_priority 必须引用 venue_urls 中的场馆")

    dashboard_raw = _required(raw, "dashboard")
    notification_raw = _required(raw, "notification")
    browser_raw = _required(raw, "browser")
    selectors_raw = _required(raw, "selectors")
    dashboard_host = str(_required(dashboard_raw, "host"))
    if dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard.host 必须绑定本机回环地址，再通过 Tailscale Serve 开放")
    profile_dir = Path(str(_required(browser_raw, "profile_dir")))
    if not profile_dir.is_absolute():
        profile_dir = path.parent.parent / profile_dir

    bark_server = str(_required(notification_raw, "bark_server")).rstrip("/")
    parsed_bark_server = urlparse(bark_server)
    if parsed_bark_server.scheme not in {"http", "https"} or not parsed_bark_server.netloc:
        raise ValueError("bark_server 必须是有效的 HTTP(S) 地址")
    emergency_level = str(_required(notification_raw, "emergency_level"))
    if emergency_level not in {"critical", "active", "timeSensitive", "passive"}:
        raise ValueError("Bark emergency_level 不受支持")
    emergency_volume = int(_required(notification_raw, "emergency_volume"))
    if not 0 <= emergency_volume <= 10:
        raise ValueError("Bark emergency_volume 必须在 0 到 10 之间")
    navigation_timeout_ms = int(_required(browser_raw, "navigation_timeout_ms"))
    captcha_mode = str(browser_raw.get("captcha_mode", "manual")).lower()
    if captcha_mode not in {"manual", "auto"}:
        raise ValueError("captcha_mode 必须是 manual 或 auto")
    post_submit_detection_timeout = int(
        browser_raw.get("post_submit_detection_timeout_seconds", 15)
    )
    captcha_timeout = int(_required(browser_raw, "manual_captcha_timeout_seconds"))
    payment_hold = int(_required(browser_raw, "payment_hold_seconds"))
    refresh_count = int(_required(browser_raw, "availability_refresh_count"))
    refresh_interval = float(_required(browser_raw, "availability_refresh_interval_seconds"))
    if min(
        navigation_timeout_ms,
        post_submit_detection_timeout,
        captcha_timeout,
        payment_hold,
        refresh_count,
    ) <= 0:
        raise ValueError("浏览器超时、保留时间和刷新次数必须大于 0")
    if refresh_interval < 0.5:
        raise ValueError("空闲状态刷新间隔不能小于 0.5 秒")

    return AppConfig(
        timezone=timezone,
        days_ahead=days_ahead,
        open_time=open_time,
        startup_time=startup_time,
        headed_browser=bool(_required(raw, "headed_browser")),
        home_url=str(_required(raw, "home_url")),
        venue_urls=venue_urls,
        venue_priority=venue_priority,
        default_start_time=start_time,
        default_end_time=end_time,
        dashboard=DashboardConfig(
            host=dashboard_host,
            port=int(_required(dashboard_raw, "port")),
            public_url=str(_required(dashboard_raw, "public_url")).rstrip("/"),
        ),
        notification=NotificationConfig(
            provider=str(_required(notification_raw, "provider")),
            bark_server=bark_server,
            remote_control_url=str(_required(notification_raw, "remote_control_url")),
            emergency_level=emergency_level,
            emergency_sound=str(_required(notification_raw, "emergency_sound")),
            emergency_volume=emergency_volume,
            emergency_call=bool(_required(notification_raw, "emergency_call")),
        ),
        browser=BrowserConfig(
            profile_dir=profile_dir.resolve(),
            navigation_timeout_ms=navigation_timeout_ms,
            captcha_mode=captcha_mode,
            post_submit_detection_timeout_seconds=post_submit_detection_timeout,
            manual_captcha_timeout_seconds=captcha_timeout,
            payment_hold_seconds=payment_hold,
            availability_refresh_count=refresh_count,
            availability_refresh_interval_seconds=refresh_interval,
        ),
        selectors={str(k): tuple(str(item) for item in v) for k, v in selectors_raw.items()},
    )
