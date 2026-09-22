from __future__ import annotations

import html
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import AppConfig
from .models import DailyPlan, PlanStatus, validate_time_window
from .secrets import SecretStore
from .store import StateStore


PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="theme-color" content="#f5f1e8">
  <title>羽毛球预约设置</title>
  <style>
        :root { font-family: "Aptos", "Segoe UI", sans-serif; color: #20302b; background: #f5f1e8; }
        * { box-sizing: border-box; }
        body { margin: 0; min-height: 100vh; background: radial-gradient(circle at top right, #dce9df 0, #f5f1e8 38rem); }
        main { max-width: 46rem; margin: 0 auto; padding: 2rem 1rem 3rem; }
        .hero { padding: .5rem .25rem 1.5rem; }
        .eyebrow { margin: 0 0 .55rem; color: #54705f; font-size: .75rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
        h1 { margin: 0; color: #18352b; font-size: clamp(1.8rem, 7vw, 2.7rem); line-height: 1.05; letter-spacing: -.03em; }
        .subtitle { max-width: 31rem; margin: .8rem 0 0; color: #627169; line-height: 1.6; }
        .card { margin-top: 1rem; padding: clamp(1.15rem, 4vw, 1.7rem); border: 1px solid #d8dfd7; border-radius: 20px; background: #fffdf9; box-shadow: 0 16px 36px #31483b14; }
        .summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: .6rem; margin: 1.2rem 0 1.4rem; }
        .summary-item { min-width: 0; padding: .8rem; border-radius: 12px; background: #edf3ed; }
        .summary-label { display: block; color: #718077; font-size: .72rem; }
        .summary-value { display: block; margin-top: .25rem; overflow: hidden; color: #234638; font-size: .94rem; font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
        .section-title { margin: 0 0 .8rem; color: #294b3c; font-size: 1rem; }
        .switch { display: flex; align-items: center; gap: .8rem; padding: 1rem; border: 1px solid #d8dfd7; border-radius: 14px; background: #f8faf6; }
        .switch input { width: 1.25rem; height: 1.25rem; accent-color: #2d7654; }
        .switch label { margin: 0; color: #244536; font-weight: 750; }
        .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: 1rem; }
        label { display: block; color: #52635a; font-size: .82rem; font-weight: 750; }
        input[type=time] { width: 100%; margin-top: .4rem; padding: .8rem .75rem; border: 1px solid #cbd6cc; border-radius: 12px; background: #fff; color: #20302b; font: inherit; font-size: 1.1rem; }
        input:focus-visible, button:focus-visible { outline: 3px solid #a9d2b8; outline-offset: 2px; }
        button { width: 100%; margin-top: 1.2rem; padding: .9rem 1rem; border: 0; border-radius: 12px; background: #28694b; color: white; font: inherit; font-weight: 800; cursor: pointer; box-shadow: 0 7px 0 #1b4c36; transition: transform .12s ease, box-shadow .12s ease; }
        button:hover { background: #215b40; }
        button:active { transform: translateY(4px); box-shadow: 0 3px 0 #1b4c36; }
        .hint { margin: 1rem 0 0; color: #718077; font-size: .85rem; line-height: 1.55; }
        .ok, .error { margin: 0 0 1rem; padding: .8rem 1rem; border-radius: 12px; font-weight: 700; }
        .ok { background: #e4f2e8; color: #21613e; }
        .error { background: #f9e5df; color: #98402f; }
        .events-heading { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
        .events-heading h2 { margin: 0; color: #294b3c; font-size: 1rem; }
        .events-heading span { color: #87928b; font-size: .75rem; }
        ul { display: grid; gap: .65rem; margin: 1rem 0 0; padding: 0; list-style: none; }
        li { padding: .7rem .8rem; border-left: 3px solid #9ac2a6; background: #f6f8f3; color: #637168; font-size: .85rem; line-height: 1.45; }
        @media (max-width: 30rem) { .summary { grid-template-columns: 1fr; } .field-row { grid-template-columns: 1fr; } }
  </style>
</head>
<body><main>
    <header class="hero">
        <p class="eyebrow">BUAA BADMINTON</p>
        <h1>明早预约设置</h1>
        <p class="subtitle">选好今晚的预约计划，明早由程序按时打开场馆页面。</p>
    </header>
    <section class="card">
        <div class="summary">
            <div class="summary-item"><span class="summary-label">计划日期</span><strong class="summary-value">{target_date}</strong></div>
            <div class="summary-item"><span class="summary-label">运行日期</span><strong class="summary-value">{run_date}</strong></div>
            <div class="summary-item"><span class="summary-label">场馆顺序</span><strong class="summary-value">{venues}</strong></div>
        </div>
    {notice}
    <form method="post" action="/plan">
      <input type="hidden" name="token" value="{token}">
      <input type="hidden" name="run_date" value="{run_date}">
      <div class="switch">
        <input id="enabled" name="enabled" type="checkbox" {checked}>
        <label for="enabled" style="margin:0">明早执行预约</label>
      </div>
            <div class="field-row">
                <label for="start_time">开始时间
                    <input id="start_time" name="start_time" type="time" value="{start_time}" required>
                </label>
                <label for="end_time">结束时间
                    <input id="end_time" name="end_time" type="time" value="{end_time}" required>
                </label>
            </div>
      <button type="submit">保存并确认</button>
    </form>
        <p class="hint">验证码出现后，Bark 会响铃提醒。你需要远程进入电脑手动点选。</p>
  </section>
  <section class="card" style="margin-top:1rem">
        <div class="events-heading"><h2>最近事件</h2><span>最多显示 10 条</span></div>
    <ul>{events}</ul>
  </section>
</main></body></html>"""


class DashboardApp:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        secret_store: SecretStore,
        artifacts_dir: Path,
    ) -> None:
        self.config = config
        self.store = store
        self.secret_store = secret_store
        self.artifacts_dir = artifacts_dir.resolve()
        self.token = secret_store.ensure_dashboard_token()

    def payment_qr_path(self, run_date: date) -> Path:
        return self.artifacts_dir / run_date.isoformat() / "payment-qr.png"

    def parse_run_date(self, value: str | None) -> date:
        if not value:
            raise ValueError("设置链接缺少运行日期，请使用最新通知中的链接")
        try:
            run_date = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("设置链接中的运行日期无效") from exc
        today = datetime.now(self.config.tz).date()
        if run_date not in (today, today + timedelta(days=1)):
            raise ValueError("设置链接已过期，请使用最新通知中的链接")
        return run_date

    def plan_for_run_date(self, run_date: date) -> DailyPlan:
        existing = self.store.get_plan(run_date)
        if existing:
            return existing
        plan = DailyPlan.for_run_date(
            run_date,
            self.config.days_ahead,
            self.config.default_start_time,
            self.config.default_end_time,
        )
        self.store.save_plan(plan)
        return plan

    def authenticate(self, candidate: str | None) -> bool:
        return bool(candidate) and secrets.compare_digest(candidate, self.token)

    def save_form(self, form: dict[str, list[str]]) -> DailyPlan:
        token = form.get("token", [""])[0]
        if not self.authenticate(token):
            raise PermissionError("访问令牌无效")
        start_time = form.get("start_time", [""])[0]
        end_time = form.get("end_time", [""])[0]
        validate_time_window(start_time, end_time)
        run_date = self.parse_run_date(form.get("run_date", [""])[0])
        current = self.plan_for_run_date(run_date)
        plan = replace(
            current,
            enabled="enabled" in form,
            start_time=start_time,
            end_time=end_time,
            status=PlanStatus.CONFIRMED,
        )
        self.store.save_plan(plan)
        self.store.add_event(
            "info",
            "plan_confirmed",
            f"已确认 {plan.target_date.isoformat()} {plan.start_time}-{plan.end_time}，执行={plan.enabled}",
        )
        return plan

    def render(self, run_date: date, notice: str = "", error: bool = False) -> bytes:
        plan = self.plan_for_run_date(run_date)
        events = self.store.recent_events(10)
        event_html = "".join(
            f"<li>{html.escape(event.created_at.strftime('%m-%d %H:%M'))} "
            f"{html.escape(event.message)}</li>"
            for event in events
        ) or "<li>暂无记录</li>"
        notice_html = ""
        if notice:
            css_class = "error" if error else "ok"
            notice_html = f'<p class="{css_class}">{html.escape(notice)}</p>'
        values = {
            "run_date": plan.run_date.isoformat(),
            "target_date": plan.target_date.isoformat(),
            "venues": " → ".join(self.config.venue_priority),
            "notice": notice_html,
            "token": html.escape(self.token, quote=True),
            "checked": "checked" if plan.enabled else "",
            "start_time": html.escape(plan.start_time, quote=True),
            "end_time": html.escape(plan.end_time, quote=True),
            "events": event_html,
        }
        body = PAGE_TEMPLATE
        for key, value in values.items():
            body = body.replace("{" + key + "}", value)
        return body.encode("utf-8")


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BookingDashboard/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # URL contains a secret token, so the default HTTP access log is disabled.
            return

        def _send_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _send_png(self, body: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                body = b"ok"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/payment-qr":
                if not app.authenticate(query.get("token", [""])[0]):
                    self._send_html("无权访问".encode("utf-8"), HTTPStatus.FORBIDDEN)
                    return
                try:
                    run_date = app.parse_run_date(query.get("run_date", [""])[0])
                    qr_path = app.payment_qr_path(run_date)
                    body = qr_path.read_bytes()
                except (ValueError, OSError):
                    self._send_html("支付二维码尚未生成".encode("utf-8"), HTTPStatus.NOT_FOUND)
                    return
                self._send_png(body)
                return
            if parsed.path != "/" or not app.authenticate(query.get("token", [""])[0]):
                self._send_html("无权访问".encode("utf-8"), HTTPStatus.FORBIDDEN)
                return
            try:
                run_date = app.parse_run_date(query.get("run_date", [""])[0])
            except ValueError as exc:
                self._send_html(html.escape(str(exc)).encode("utf-8"), HTTPStatus.BAD_REQUEST)
                return
            self._send_html(app.render(run_date))

        def do_POST(self) -> None:
            if self.path != "/plan":
                self._send_html("页面不存在".encode("utf-8"), HTTPStatus.NOT_FOUND)
                return
            form: dict[str, list[str]] = {}
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096:
                    raise ValueError("请求过大")
                form = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"), keep_blank_values=True
                )
                plan = app.save_form(form)
            except PermissionError:
                self._send_html("无权访问".encode("utf-8"), HTTPStatus.FORBIDDEN)
                return
            except (ValueError, UnicodeDecodeError) as exc:
                try:
                    run_date = app.parse_run_date(form.get("run_date", [""])[0])
                    body = app.render(run_date, str(exc), error=True)
                except ValueError:
                    body = html.escape(str(exc)).encode("utf-8")
                self._send_html(body, HTTPStatus.BAD_REQUEST)
                return
            self._send_html(app.render(plan.run_date, "设置已保存"))

    return Handler


def serve_dashboard(
    config: AppConfig,
    store: StateStore,
    secrets_store: SecretStore,
    artifacts_dir: Path,
) -> None:
    app = DashboardApp(config, store, secrets_store, artifacts_dir)
    server = ThreadingHTTPServer(
        (config.dashboard.host, config.dashboard.port), make_handler(app)
    )
    print(
        f"手机设置页正在监听 http://{config.dashboard.host}:{config.dashboard.port}，"
        "请通过配置好的 Tailscale Serve 地址访问。"
    )
    server.serve_forever()


def dashboard_url(config: AppConfig, secret_store: SecretStore, run_date: date) -> str:
    token = secret_store.ensure_dashboard_token()
    query = urllib.parse.urlencode({"token": token, "run_date": run_date.isoformat()})
    return f"{config.dashboard.public_url}/?{query}"


def payment_qr_url(config: AppConfig, secret_store: SecretStore, run_date: date) -> str:
    token = secret_store.ensure_dashboard_token()
    query = urllib.parse.urlencode({"token": token, "run_date": run_date.isoformat()})
    return f"{config.dashboard.public_url}/payment-qr?{query}"


def wait_for_dashboard(
    config: AppConfig,
    *,
    timeout_seconds: float = 20.0,
    interval_seconds: float = 0.5,
) -> bool:
    host = (
        f"[{config.dashboard.host}]"
        if ":" in config.dashboard.host
        else config.dashboard.host
    )
    health_url = f"http://{host}:{config.dashboard.port}/healthz"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            with urllib.request.urlopen(health_url, timeout=min(2.0, remaining)) as response:
                if response.status == HTTPStatus.OK and response.read() == b"ok":
                    return True
        except (OSError, TimeoutError, urllib.error.URLError):
            pass
        time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))
    return False
