import json
from pathlib import Path

import pytest

from buaa_booking.config import load_config


def _example() -> dict:
    return json.loads(Path("config/booking.example.json").read_text(encoding="utf-8"))


def test_load_example_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "booking.json"
    path.write_text(json.dumps(_example(), ensure_ascii=False), encoding="utf-8")
    config = load_config(path)
    assert config.venue_priority == ("38", "39")
    assert config.browser.profile_dir == (tmp_path / "data/browser-profile").resolve()
    assert config.dry_run is True


def test_rejects_public_dashboard_bind(tmp_path: Path) -> None:
    raw = _example()
    raw["dashboard"]["host"] = "0.0.0.0"
    path = tmp_path / "booking.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="回环地址"):
        load_config(path)


def test_rejects_fast_refresh(tmp_path: Path) -> None:
    raw = _example()
    raw["browser"]["availability_refresh_interval_seconds"] = 0.1
    path = tmp_path / "booking.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="不能小于 0.5 秒"):
        load_config(path)

