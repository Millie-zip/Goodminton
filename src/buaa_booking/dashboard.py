from __future__ import annotations

import html
import secrets
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import AppConfig
from .models import DailyPlan, PlanStatus, validate_time_window
from .secrets import SecretStore
from .store import StateStore


PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>羽毛球预约设置</title>
  <style>
    :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f3f5f7; color: #182026; }
    main { max-width: 34rem; margin: 0 auto; padding: 1.2rem; }
    .card { background: white; border-radius: 18px; padding: 1.2rem; box-shadow: 0 8px 30px #18202612; }
    h1 { margin: 0 0 .5rem; font-size: 1.45rem; }
    .muted { color: #68737d; font-size: .92rem; }
    label { display: block; margin-top: 1rem; font-weight: 650; }
    input[type=time] { width: 100%; box-sizing: border-box; margin-top: .35rem; padding: .8rem; font-size: 1.1rem; border: 1px solid #ccd3d8; border-radius: 12px; }
    .switch { display: flex; align-items: center; gap: .7rem; padding: .8rem 0; }
    .switch input { width: 1.35rem; height: 1.35rem; }
    button { width: 100%; margin-top: 1.25rem; border: 0; border-radius: 12px; padding: .9rem; background: #1769e0; color: white; font-size: 1rem; font-weight: 700; }
    .ok { margin: 0 0 1rem; padding: .75rem; border-radius: 10px; background: #e7f7ed; color: #166534; }
    .error { margin: 0 0 1rem; padding: .75rem; border-radius: 10px; background: #feecec; color: #991b1b; }
    ul { padding-left: 1.2rem; }
    @media (prefers-color-scheme: dark) { body { background:#101418;color:#edf1f4 } .card {background:#1b2228} .muted{color:#aab4bc} input[type=time]{background:#101418;color:#edf1f4;border-color:#45515a} }
  </style>
</head>
<body><main>
  <section class="card">
    <h1>明早预约设置</h1>
    <p class="muted">程序运行日：{run_date}<br>计划预约：{target_date}<br>场馆顺序：{venues}</p>
    {notice}
    <form method="post" action="/plan">
      <input type="hidden" name="token" value="{token}">
      <div class="switch">
        <input id="enabled" name="enabled" type="checkbox" {checked}>
        <label for="enabled" style="margin:0">明早执行预约</label>
      </div>
      <label for="start_time">开始时间</label>
      <input id="start_time" name="start_time" type="time" value="{start_time}" required>
      <label for="end_time">结束时间</label>
      <input id="end_time" name="end_time" type="time" value="{end_time}" required>
      <button type="submit">保存并确认</button>
    </form>
    <p class="muted">验证码出现后，手机会持续提醒。你需要远程进入电脑手动点选。</p>
  </section>
  <section class="card" style="margin-top:1rem">
    <h1 style="font-size:1.1rem">最近事件</h1>
    <ul>{events}</ul>
  </section>
</main></body></html>"""


class DashboardApp:
    def __init__(self, config: AppConfig, store: StateStore, secret_store: SecretStore) -> None:
        self.config = config
        self.store = store
        self.secret_store = secret_store
        self.token = secret_store.ensure_dashboard_token()

    def tomorrow_plan(self) -> DailyPlan:
        run_date = datetime.now(self.config.tz).date() + timedelta(days=1)
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
        current = self.tomorrow_plan()
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

    def render(self, notice: str = "", error: bool = False) -> bytes:
        plan = self.tomorrow_plan()
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
            if parsed.path != "/" or not app.authenticate(query.get("token", [""])[0]):
                self._send_html("无权访问".encode("utf-8"), HTTPStatus.FORBIDDEN)
                return
            self._send_html(app.render())

        def do_POST(self) -> None:
            if self.path != "/plan":
                self._send_html("页面不存在".encode("utf-8"), HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096:
                    raise ValueError("请求过大")
                form = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"), keep_blank_values=True
                )
                app.save_form(form)
            except PermissionError:
                self._send_html("无权访问".encode("utf-8"), HTTPStatus.FORBIDDEN)
                return
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_html(app.render(str(exc), error=True), HTTPStatus.BAD_REQUEST)
                return
            self._send_html(app.render("设置已保存"))

    return Handler


def serve_dashboard(config: AppConfig, store: StateStore, secrets_store: SecretStore) -> None:
    app = DashboardApp(config, store, secrets_store)
    server = ThreadingHTTPServer(
        (config.dashboard.host, config.dashboard.port), make_handler(app)
    )
    print(
        f"手机设置页正在监听 http://{config.dashboard.host}:{config.dashboard.port}，"
        "请通过配置好的 Tailscale Serve 地址访问。"
    )
    server.serve_forever()


def dashboard_url(config: AppConfig, secret_store: SecretStore) -> str:
    token = secret_store.ensure_dashboard_token()
    return f"{config.dashboard.public_url}/?{urllib.parse.urlencode({'token': token})}"
