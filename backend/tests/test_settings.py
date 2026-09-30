# -*- coding: utf-8 -*-
"""测试按运行周期解析 K 线文件。"""

from pathlib import Path

import pytest

from backend import settings
from backend import app
from backend.services import data_repository
from backend.services import state_store


def test_resolve_kline_path_uses_timeframe_directory(tmp_path: Path, monkeypatch) -> None:
  symbol_dir = tmp_path / "1m" / "688981_SH"
  symbol_dir.mkdir(parents=True)
  minute_file = symbol_dir / "688981_SH_1m.csv"
  minute_file.write_text("datetime\n", encoding="utf-8")
  monkeypatch.setattr(settings, "KLINE_ROOT", str(tmp_path))

  resolved = settings.resolve_kline_path("688981.SH", "1m", "20260622")

  assert resolved == str(minute_file)


def test_missing_requested_timeframe_returns_none(tmp_path: Path, monkeypatch) -> None:
  symbol_dir = tmp_path / "5m" / "688981_SH"
  symbol_dir.mkdir(parents=True)
  (symbol_dir / "688981_SH_5m.csv").write_text("datetime\n", encoding="utf-8")
  monkeypatch.setattr(settings, "KLINE_ROOT", str(tmp_path))

  resolved = settings.resolve_kline_path("688981.SH", "1m")

  assert resolved is None


def test_backtest_timeframes_are_explicit() -> None:
  assert settings.validate_backtest_timeframe("1m")
  assert settings.validate_backtest_timeframe("5m")
  assert not settings.validate_backtest_timeframe("2m")


def test_quote_prefetch_size_has_safe_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("TEST_L2_PREFETCH_SIZE", "128")

  assert settings._read_bounded_int("TEST_L2_PREFETCH_SIZE", 16, 16, 512) == 128

  monkeypatch.setenv("TEST_L2_PREFETCH_SIZE", "513")
  with pytest.raises(ValueError, match="16 至 512"):
    settings._read_bounded_int("TEST_L2_PREFETCH_SIZE", 16, 16, 512)


def test_read_bool_rejects_ambiguous_value(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("TEST_RESEARCH_REPORT_ENABLED", "yes")

  with pytest.raises(ValueError, match="true 或 false"):
    settings._read_bool("TEST_RESEARCH_REPORT_ENABLED", False)


def test_read_http_origins_normalizes_and_deduplicates(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setenv(
    "TEST_CSRF_TRUSTED_ORIGINS",
    "https://Example.com:443/,http://127.0.0.1:5174,https://example.com",
  )

  assert settings._read_http_origins("TEST_CSRF_TRUSTED_ORIGINS", ()) == (
    "https://example.com",
    "http://127.0.0.1:5174",
  )


def test_read_http_origins_rejects_path(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("TEST_CSRF_TRUSTED_ORIGINS", "https://example.com/app")

  with pytest.raises(ValueError, match="不能包含路径"):
    settings._read_http_origins("TEST_CSRF_TRUSTED_ORIGINS", ())


def test_research_reporting_requires_target_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "BACKTEST_INSTANCE_ID", "server")
  monkeypatch.setattr(settings, "RESEARCH_RESULT_REPORT_ENABLED", True)
  monkeypatch.setattr(settings, "RESEARCH_SIGNAL_API_URL", "")
  monkeypatch.setattr(settings, "RESEARCH_SIGNAL_SERVICE_TOKEN", "")

  with pytest.raises(ValueError, match="必须配置研究中心地址和服务令牌"):
    settings.validate_research_integration_configuration()


def test_observability_rejects_public_loki_url(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
  monkeypatch.setattr(settings, "LOKI_URL", "https://logs.example.com")

  with pytest.raises(ValueError, match="内部 HTTP|内部服务名"):
    settings.validate_observability_configuration()


def test_observability_accepts_internal_loki_service(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
  monkeypatch.setattr(settings, "LOKI_URL", "http://loki:3100")
  monkeypatch.setattr(settings, "LOG_LOCAL_MAX_SIZE", "20m")

  settings.validate_observability_configuration()


def test_observability_rejects_unsafe_compose_project(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "COMPOSE_PROJECT_NAME", "platform\"} | line_format \"secret\"")

  with pytest.raises(ValueError, match="安全的 Compose 项目名"):
    settings.validate_observability_configuration()


def test_l2_strategy_config_accepts_derived_l2_timeframe() -> None:
  assert settings.validate_l2_backtest_timeframe("3m")
  assert settings.validate_backtest_timeframe("3m")
  assert not settings.validate_l2_backtest_timeframe("2m")


def test_backtest_timeframe_availability_uses_database_cache(monkeypatch: pytest.MonkeyPatch) -> None:
  expected = [
    {"timeframe": "1m", "available": True, "reason": "", "dataStart": "20260727", "dataEnd": "20260727"},
    {"timeframe": "5m", "available": False, "reason": "东财不可用", "dataStart": None, "dataEnd": None},
  ]
  monkeypatch.setattr(data_repository.market_data, "timeframe_status", lambda *_args: expected)
  monkeypatch.setattr(data_repository.market_data, "ensure_cached", lambda *_args: {"status": "cache_hit"})

  items = data_repository.get_backtest_timeframes("688981.SH", "20260727", "20260727")

  assert items[0]["timeframe"] == "1m"
  assert items[0]["available"] is True
  assert items[1]["timeframe"] == "5m"
  assert items[1]["available"] is False
  data_repository.validate_backtest_coverage("688981.SH", "1m", "20260727", "20260727")


def test_backtest_run_insert_has_matching_placeholder_count() -> None:
  class Cursor:
    def __init__(self) -> None:
      self.query = ""

    def execute(self, query: str, params: tuple[object, ...]) -> None:
      assert query.count("%s") == len(params)
      self.query = query

    def fetchone(self) -> dict[str, object]:
      if "user_backtest_counter" in self.query:
        return {"display_no": 1}
      if "market_instrument" in self.query:
        return {"id": 1}
      return {"id": "00000000-0000-0000-0000-000000000002", "display_no": 1}

  class CursorContext:
    def __enter__(self) -> Cursor:
      return Cursor()

    def __exit__(self, *_args: object) -> None:
      return None

  class Connection:
    def cursor(self) -> CursorContext:
      return CursorContext()

  class ConnectionContext:
    def __enter__(self) -> Connection:
      return Connection()

    def __exit__(self, *_args: object) -> None:
      return None

  payload = {
    "symbol": "688981.SH",
    "timeframe": "1m",
    "strategy": "user_script",
    "startDate": "20260727",
    "endDate": "20260727",
  }
  original_db = state_store._db
  state_store._db = lambda: ConnectionContext()  # type: ignore[assignment]
  try:
    assert state_store.create_backtest_run(payload) == ("00000000-0000-0000-0000-000000000002", 1)
  finally:
    state_store._db = original_db  # type: ignore[assignment]


def test_experiment_global_switch_requires_bar_capability(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", False)

  with pytest.raises(ValueError, match="必须启用至少一个实验执行路径"):
    settings.validate_experiment_configuration()


def test_experiment_configuration_accepts_safe_disabled_default(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", False)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", False)

  settings.validate_experiment_configuration()


def test_experiment_configuration_requires_user_plan_limit(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 0)

  with pytest.raises(ValueError, match="用户计划 Trial 上限"):
    settings.validate_experiment_configuration()


def test_experiment_configuration_requires_safe_lease_relationship(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_LEASE_SECONDS", 15)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_HEARTBEAT_SECONDS", 5)

  with pytest.raises(ValueError, match="租约必须大于心跳间隔的三倍"):
    settings.validate_experiment_configuration()


def test_experiment_configuration_requires_bounded_window_and_cursor_secret(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 100)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 100)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_LEASE_SECONDS", 30)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_HEARTBEAT_SECONDS", 5)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 2)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 1)

  with pytest.raises(ValueError, match="单实验在途上限"):
    settings.validate_experiment_configuration()

  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_CURSOR_SIGNING_SECRET", "short")
  with pytest.raises(ValueError, match="游标签名密钥"):
    settings.validate_experiment_configuration()

  monkeypatch.setattr(settings, "EXPERIMENT_CURSOR_SIGNING_SECRET", "s" * 32)
  settings.validate_experiment_configuration()
