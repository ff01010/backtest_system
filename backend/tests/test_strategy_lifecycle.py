from __future__ import annotations

from typing import Any

import pytest

from backend.services import state_store, strategy_lifecycle


def _config() -> dict[str, Any]:
  return {
    "id": "config-1",
    "name": "参数策略",
    "symbol": "688981.SH",
    "paramsJson": {
      "params": {"window": 20, "threshold": 0.5},
      "strategyParameterDefinitions": {
        "window": {"type": "integer", "default": 10},
        "threshold": {"type": "number", "default": 0.25},
      },
      "strategyParams": {"window": 20, "threshold": 0.5},
    },
  }


def _command() -> strategy_lifecycle.StrategySaveCommand:
  return strategy_lifecycle.StrategySaveCommand(
    name="参数策略",
    description="",
    code=(
      'STRATEGY_PARAMETERS = {"window": 20}\n\n'
      "def on_bar(bar, state):\n"
      "  return []\n"
    ),
    entry_type="on_bar",
    signal_required=False,
    strategy="user_script",
    mode="backtest",
    symbol="688981.SH",
    timeframe="5m",
    benchmark_symbol="688981.SH",
    account_id=None,
    params_json={
      "params": {
        "startDate": "2026-06-01",
        "endDate": "2026-06-30",
        "fillModel": "bar_next_open",
      },
    },
    strategy_parameters={"window": 20},
    enabled=True,
  )


def test_effective_strategy_parameters_merges_partial_override() -> None:
  saved, overrides, effective = strategy_lifecycle.effective_strategy_parameters(
    _config(), {"window": 12},
  )

  assert saved == {"window": 20, "threshold": 0.5}
  assert overrides == {"window": 12}
  assert effective == {"window": 12, "threshold": 0.5}


@pytest.mark.parametrize(
  ("overrides", "message"),
  [
    ({"missing": 1}, "未声明"),
    ({"window": 1.5}, "类型不匹配"),
    ({"threshold": True}, "类型不匹配"),
  ],
)
def test_effective_strategy_parameters_rejects_invalid_override(
  overrides: dict[str, Any],
  message: str,
) -> None:
  with pytest.raises(strategy_lifecycle.StrategyLifecycleError, match=message):
    strategy_lifecycle.effective_strategy_parameters(_config(), overrides)


def test_public_save_replay_precedes_resource_validation(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  def replay(*_: Any, **__: Any) -> tuple[dict[str, Any], bool, int]:
    return {
      "strategy": {"config_id": "config-1", "revision": 1},
    }, True, 201

  monkeypatch.setattr(state_store, "execute_public_write", replay)
  monkeypatch.setattr(
    strategy_lifecycle,
    "_validated_payloads",
    lambda _: pytest.fail("幂等重放不应重新读取或校验已变化的资源"),
  )

  result = strategy_lifecycle.save_strategy(
    _command(),
    strategy_lifecycle.PublicWriteContext(
      api_key_id="key-1",
      idempotency_key="idem-12345678",
      request_hash="a" * 64,
      operation_type="strategy.create",
      request_id="req-1",
    ),
  )

  assert result["replayed"] is True
  assert result["response_status"] == 201


def test_parameter_coordination_preserves_same_type_and_drops_removed() -> None:
  values = strategy_lifecycle._coordinate_strategy_parameters(
    {
      "window": {"type": "integer", "default": 10},
      "enabled": {"type": "boolean", "default": True},
    },
    {},
    {
      "window": {"type": "integer", "default": 5},
      "removed": {"type": "string", "default": "old"},
      "enabled": {"type": "string", "default": "yes"},
    },
    {"window": 30, "removed": "value", "enabled": "no"},
  )

  assert values == {"window": 30, "enabled": True}


def test_web_catalog_strategy_keeps_existing_entry_type_compatibility(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  captured: dict[str, Any] = {}

  def save(
    script_payload: dict[str, Any],
    config_payload: dict[str, Any],
    *_: Any,
  ) -> dict[str, Any]:
    captured["script"] = script_payload
    captured["config"] = config_payload
    return {"script": {"id": "script-1"}, "config": {"id": "config-1"}}

  command = _command()
  command = strategy_lifecycle.StrategySaveCommand(
    **{
      **command.__dict__,
      "entry_type": "catalog:trend_follow",
      "strategy": "trend_follow",
      "code": "VALUE = 1\n",
      "strategy_parameters": {},
    },
  )
  monkeypatch.setattr(state_store, "save_complete_strategy", save)

  result = strategy_lifecycle.save_strategy(command)

  assert result["config"]["id"] == "config-1"
  assert captured["script"]["entryType"] == "catalog:trend_follow"
