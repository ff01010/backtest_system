"""平台内置量化库的版本与最小数值计算回归。"""

from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import scipy.stats
import statsmodels.api as sm
import talib

from backend.services.backtest_errors import StrategyExecutionError
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  SCRIPT_RUNTIME_VERSION,
  SUPPORTED_QUANT_PACKAGE_VERSIONS,
)
from backend.strategies.scripted import ScriptedBarStrategy


def test_supported_quant_package_versions_match_runtime_contract() -> None:
  actual = {
    distribution: version(distribution)
    for distribution in SUPPORTED_QUANT_PACKAGE_VERSIONS
  }

  assert actual == dict(SUPPORTED_QUANT_PACKAGE_VERSIONS)


def test_supported_quant_packages_execute_real_calculations() -> None:
  values = np.asarray([1.0, 2.0, 3.0, 4.0, 5.0], dtype=float)

  assert pd.Series(values).rolling(3).mean().iloc[-1] == pytest.approx(4.0)
  assert scipy.stats.linregress(values, values * 2).slope == pytest.approx(2.0)
  assert talib.SMA(values, timeperiod=3)[-1] == pytest.approx(4.0)

  design = sm.add_constant(np.asarray([0.0, 1.0, 2.0], dtype=float))
  result = sm.OLS(np.asarray([1.0, 2.0, 3.0], dtype=float), design).fit()
  assert result.params.tolist() == pytest.approx([1.0, 1.0])


def test_numerical_calculation_keeps_single_process_thread() -> None:
  status_path = Path("/proc/self/status")
  if not status_path.exists():
    pytest.skip("仅 Linux 运行容器提供 /proc 线程统计")

  values = np.arange(1.0, 65.0, dtype=float).reshape(8, 8)
  np.linalg.svd(values)
  scipy.stats.linregress(values[0], values[1])
  status = status_path.read_text(encoding="utf-8")
  threads_line = next(line for line in status.splitlines() if line.startswith("Threads:"))

  assert int(threads_line.split(":", 1)[1].strip()) == 1


def test_talib_indicator_windows_have_fixed_expected_values() -> None:
  values = np.arange(1.0, 41.0, dtype=float)
  macd, signal, histogram = talib.MACD(
    values, fastperiod=3, slowperiod=6, signalperiod=3,
  )

  assert np.isnan(talib.SMA(values[:2], timeperiod=3)).all()
  assert talib.EMA(values, timeperiod=3)[-1] == pytest.approx(39.0)
  assert talib.RSI(values, timeperiod=3)[-1] == pytest.approx(100.0)
  assert macd[-1] == pytest.approx(1.5)
  assert signal[-1] == pytest.approx(1.5)
  assert histogram[-1] == pytest.approx(0.0)


def test_quant_packages_handle_empty_constant_and_non_finite_inputs() -> None:
  empty = np.asarray([], dtype=float)
  constant = np.asarray([2.0, 2.0, 2.0, 2.0], dtype=float)
  non_finite = np.asarray([1.0, np.nan, np.inf], dtype=float)

  assert talib.SMA(empty, timeperiod=3).size == 0
  assert np.isnan(talib.SMA(constant[:2], timeperiod=3)).all()
  assert scipy.stats.variation(constant) == pytest.approx(0.0)
  assert np.isfinite(non_finite).tolist() == [True, False, False]
  with pytest.raises(Exception):
    talib.SMA(constant, timeperiod=0)


def test_supported_package_submodule_typo_is_user_error_end_to_end() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy(
      "talib-submodule",
      "import talib.nonexistent\n\ndef on_bar(bar, state):\n  return []\n",
      {}, runtime_version=SCRIPT_RUNTIME_VERSION,
      capability_digest=RUNTIME_CAPABILITY_DIGEST,
    )

  assert raised.value.detail.category == "strategy_error"
  assert raised.value.detail.code == "strategy.module_unavailable"
  assert raised.value.detail.requested_module == "talib.nonexistent"
