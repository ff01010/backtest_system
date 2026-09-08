"""固定策略运行时能力与错误责任回归。"""

import pytest

from backend.backtesting.models import Bar, PortfolioSnapshot
from backend.services.backtest_errors import StrategyExecutionError
from backend.strategies.runtime_contract import SCRIPT_RUNTIME_VERSION
from backend.strategies.scripted import ScriptedBarStrategy


def _portfolio() -> PortfolioSnapshot:
  return PortfolioSnapshot(cash=100_000, equity=100_000, positions={})


def _bar() -> Bar:
  return Bar("20260908093000", "688981.SH", 10, 10, 10, 10)


def test_standard_python_builtins_do_not_require_per_name_allowlist() -> None:
  strategy = ScriptedBarStrategy("builtins", """
def on_bar(bar, state):
  values = tuple(reversed(list(enumerate(zip(range(3), range(3))))))
  state['values'] = values
  state['window'] = list(range(10))[slice(1, 3)]
  return []
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION)

  assert strategy.on_bar(_bar(), _portfolio()) == []
  assert len(strategy._state["values"]) == 3
  assert strategy._state["window"] == [1, 2]


def test_blocked_builtin_has_policy_error_instead_of_name_error() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy("blocked", """
def on_bar(bar, state):
  open('forbidden.txt')
  return []
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION)

  assert raised.value.detail.code == "strategy.runtime_policy_blocked"
  assert raised.value.detail.missing_name == "open"


def test_undefined_name_and_local_unbound_have_distinct_codes() -> None:
  undefined = ScriptedBarStrategy("undefined", """
def on_bar(bar, state):
  return missing_signal
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION)
  with pytest.raises(StrategyExecutionError) as undefined_error:
    undefined.on_bar(_bar(), _portfolio())

  local = ScriptedBarStrategy("local", """
def on_bar(bar, state):
  if bar['close'] < 0:
    quantity = 100
  return quantity
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION)
  with pytest.raises(StrategyExecutionError) as local_error:
    local.on_bar(_bar(), _portfolio())

  assert undefined_error.value.detail.code == "strategy.undefined_name"
  assert undefined_error.value.detail.missing_name == "missing_signal"
  assert undefined_error.value.detail.strategy_line == 3
  assert local_error.value.detail.code == "strategy.local_unbound"
  assert local_error.value.detail.missing_name == "quantity"


def test_standard_builtin_name_error_is_platform_contract_error() -> None:
  error = NameError("name 'tuple' is not defined", name="tuple")
  detail = StrategyExecutionError.from_exception(error, "on_bar").detail

  assert detail.category == "system_error"
  assert detail.code == "runtime.contract_missing"
  assert detail.missing_name == "tuple"
  assert detail.runtime_version == SCRIPT_RUNTIME_VERSION
  assert detail.hint == "无需修改当前策略，请提供任务编号联系管理员。"


def test_mismatched_runtime_version_is_platform_error() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy(
      "version", "def on_bar(bar, state):\n  return []\n", {},
      runtime_version="python3.11-runtime-v1",
    )

  assert raised.value.detail.category == "system_error"
  assert raised.value.detail.code == "runtime.version_mismatch"
