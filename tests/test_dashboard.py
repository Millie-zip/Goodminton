from datetime import date
from pathlib import Path

from buaa_booking.config import load_config
from buaa_booking.dashboard import DashboardApp
from buaa_booking.store import StateStore


class FakeSecrets:
    def ensure_dashboard_token(self) -> str:
        return "dashboard-secret"


def test_dashboard_saves_confirmed_plan(tmp_path: Path) -> None:
    config = load_config(Path("config/booking.example.json"))
    store = StateStore(tmp_path / "state.sqlite3")
    app = DashboardApp(config, store, FakeSecrets())  # type: ignore[arg-type]
    plan = app.save_form(
        {
            "token": ["dashboard-secret"],
            "enabled": ["on"],
            "start_time": ["19:00"],
            "end_time": ["21:00"],
        }
    )
    assert plan.enabled is True
    assert plan.status.value == "confirmed"
    assert store.get_plan(plan.run_date) == plan


def test_dashboard_render_does_not_break_on_css_braces(tmp_path: Path) -> None:
    config = load_config(Path("config/booking.example.json"))
    store = StateStore(tmp_path / "state.sqlite3")
    app = DashboardApp(config, store, FakeSecrets())  # type: ignore[arg-type]
    body = app.render().decode("utf-8")
    assert "明早预约设置" in body
    assert "dashboard-secret" in body

