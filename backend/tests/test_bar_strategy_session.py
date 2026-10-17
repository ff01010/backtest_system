from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path

import pandas as pd
import pytest

from backend import settings
from backend.bar_market_data_transport import BarMarketDataRuntime, ParentBarMarketData
from backend.bar_strategy_session import (
  BarStrategyProcessError,
  BarStrategySession,
  _process_exit_error,
)
from backend.services.backtest_errors import RemoteBacktestError
from backend.strategies.runtime_contract import RUNTIME_CAPABILITY_DIGEST, SCRIPT_RUNTIME_VERSION


def _payload(code: str) -> dict[str, object]:
  return {
    "scriptId": "session-test",
    "code": code,
    "params": {},
    "bars": [
      {
        "time": "20260901093500", "symbol": "688981.SH", "open": 10,
        "high": 10, "low": 10, "close": 10, "volume": 1000,
      },
    ],
    "initialCapital": 100000,
    "commissionRate": 0,
    "feeProfile": {},
    "slippage": 0,
    "holdingPolicy": "intraday",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "warmupBarCount": 0,
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }


def _run_session(
  payload: dict[str, object],
  *,
  session: BarStrategySession | None = None,
) -> dict[str, object]:
  control_payload = dict(payload)
  rows = control_payload.pop("bars")
  control_payload["symbol"] = rows[0]["symbol"]  # type: ignore[index]
  frame = pd.DataFrame([
    {key: value for key, value in row.items() if key != "symbol"}
    for row in rows  # type: ignore[union-attr]
  ])
  return (session or BarStrategySession()).run_script(control_payload, frame)


def test_bar_strategy_session_returns_isolated_result() -> None:
  result = _run_session(_payload("""
def on_bar(bar, state):
  state['seen'] = state.get('seen', 0) + 1
  return []
"""))

  assert result["trades"] == []
  assert result["skippedSignals"] == []


def test_bar_strategy_session_exposes_effective_strategy_parameters() -> None:
  payload = _payload("""
def on_bar(bar, state):
  if bar['time'] == '20260901093500':
    return [{'action': 'buy_open', 'quantity': state['params']['window']}]
  return []
""")
  payload["params"] = {"window": 200}
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 10,
    "high": 10, "low": 10, "close": 10, "volume": 1000,
  })

  result = _run_session(payload)

  assert result["trades"][0]["quantity"] == 200


def test_bar_strategy_session_supports_standard_tuple_builtin() -> None:
  result = _run_session(_payload("""
def on_bar(bar, state):
  state['items'] = tuple(enumerate(range(2)))
  return []
"""))

  assert len(result["portfolioSnapshots"]) == 1


def test_bar_strategy_session_supports_talib_with_bounded_numeric_threads() -> None:
  payload = _payload("""
import os
import numpy as np
import talib

NUMERIC_THREAD_NAMES = (
  'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
)


def on_bar(bar, state):
  values = np.asarray([1.0, 2.0, 3.0], dtype=float)
  sma = talib.SMA(values, timeperiod=3)[-1]
  threads_are_bounded = all(os.environ.get(name) == '1' for name in NUMERIC_THREAD_NAMES)
  if bar['time'] == '20260901093500' and np.isfinite(sma) and threads_are_bounded:
    return [{'action': 'buy_open', 'quantity': int(np.int64(200)), 'reason': 'TA-Lib SMA'}]
  return []
""")
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 10,
    "high": 10, "low": 10, "close": 10, "volume": 1000,
  })

  result = _run_session(payload)

  assert len(result["trades"]) == 1
  assert result["trades"][0]["quantity"] == 200


def test_bar_strategy_session_keeps_raw_script_exception_for_administrator() -> None:
  payload = _payload("def on_bar(bar, state):\n  return missing_variable\n")
  payload["scriptId"] = "diagnostic-script"

  with pytest.raises(RemoteBacktestError) as raised:
    _run_session(payload)

  diagnostic = raised.value.diagnostic
  assert diagnostic["exceptionType"] == "NameError"
  assert "missing_variable" in diagnostic["reason"]
  assert "missing_variable" in diagnostic["__internal_raw_message"]
  assert "strategy_script_diagnostic-script.py" in diagnostic["__internal_raw_traceback"]


def test_bar_strategy_session_applies_slippage_to_trade_price() -> None:
  payload = _payload(
    "def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100}]\n",
  )
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 11,
    "high": 11, "low": 11, "close": 11, "volume": 1000,
  })
  payload["slippage"] = 0.001
  payload["holdingPolicy"] = "overnight"

  result = _run_session(payload)

  assert round(result["trades"][0]["price"], 2) == 11.01
  assert result["trades"][0]["priceSource"] == "下一根 Bar 开盘价 + 固定滑点 10.00 bp"


def test_bar_strategy_session_terminates_blocked_strategy_on_cancel() -> None:
  checks = 0

  def abort_check() -> None:
    nonlocal checks
    checks += 1
    if checks >= 3:
      raise ValueError("Bar 任务租约丢失或已取消")

  started = time.monotonic()
  with pytest.raises(ValueError, match="已取消"):
    _run_session(_payload("""
def on_bar(bar, state):
  while True:
    pass
"""), session=BarStrategySession(abort_check))

  assert time.monotonic() - started < 5


def test_bar_strategy_session_records_nonzero_process_exit() -> None:
  with pytest.raises(BarStrategyProcessError) as raised:
    _run_session(_payload("""
import os

def on_bar(bar, state):
  os._exit(7)
"""))

  assert raised.value.exit_code == 7
  assert raised.value.termination_reason == "strategy_exit_nonzero"


@pytest.mark.parametrize("transport", ["shared_memory", "mmap"])
def test_bar_strategy_session_runs_through_market_data_reference(
  tmp_path: Path,
  transport: str,
) -> None:
  payload = _payload("""
def on_bar(bar, state):
  if bar['time'] == '20260901093500':
    return [{'action': 'buy_open', 'quantity': 100}]
  return []
""")
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 11,
    "high": 11, "low": 11, "close": 11, "volume": 1000,
  })
  runtime = BarMarketDataRuntime(
    tmp_path / transport,
    shared_memory_limit_bytes=1024 * 1024,
  )
  session = BarStrategySession(
    ipc_mode="auto",
    market_data_runtime=runtime,
    transport_override=transport,
  )

  result = _run_session(payload, session=session)

  assert result["trades"][0]["executionTime"] == "20260901094000"
  assert list(runtime.root.glob("bars-*.npy")) == []
  runtime.close()


@pytest.mark.parametrize("transport", ["shared_memory", "mmap"])
def test_parent_releases_transport_before_strategy_callback(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  transport: str,
) -> None:
  released = Path(tempfile.gettempdir()) / f"bar-ipc-released-{uuid.uuid4().hex}"
  original_close = ParentBarMarketData.close

  def record_close(resource: ParentBarMarketData) -> None:
    original_close(resource)
    released.write_text("closed", encoding="utf-8")

  monkeypatch.setattr(ParentBarMarketData, "close", record_close)
  payload = _payload(f"""
import os

def on_bar(bar, state):
  if not os.path.exists({str(released)!r}):
    raise RuntimeError('父进程尚未释放行情传输资源')
  return []
""")
  runtime = BarMarketDataRuntime(
    tmp_path / f"early-release-{transport}",
    shared_memory_limit_bytes=1024 * 1024,
  )
  try:
    result = _run_session(payload, session=BarStrategySession(
      ipc_mode="auto",
      market_data_runtime=runtime,
      transport_override=transport,
    ))
    assert result["trades"] == []
    assert released.read_text(encoding="utf-8") == "closed"
  finally:
    runtime.close()
    released.unlink(missing_ok=True)


@pytest.mark.parametrize("transport", ["shared_memory", "mmap"])
def test_user_strategy_cannot_reflect_transport_objects(
  tmp_path: Path,
  transport: str,
) -> None:
  payload = _payload("""
import sys

def on_bar(bar, state):
  frame = sys._getframe()
  while frame is not None:
    for value in frame.f_locals.values():
      if hasattr(value, '_shared_memory') or hasattr(value, '_source'):
        raise RuntimeError('反射取得行情映射对象')
      if hasattr(value, 'filename'):
        raise RuntimeError('反射取得 mmap 文件对象')
      if isinstance(value, dict) and {
        'schemaVersion', 'transport', 'location', 'rowCount'
      }.issubset(value):
        raise RuntimeError('反射取得行情传输引用')
      if isinstance(value, dict) and 'marketDataRef' in value:
        raise RuntimeError('反射取得含行情引用的控制载荷')
    frame = frame.f_back
  return []
""")
  runtime = BarMarketDataRuntime(
    tmp_path / f"reflection-{transport}",
    shared_memory_limit_bytes=1024 * 1024,
  )
  try:
    result = _run_session(payload, session=BarStrategySession(
      ipc_mode="auto",
      market_data_runtime=runtime,
      transport_override=transport,
    ))
  finally:
    runtime.close()

  assert result["trades"] == []


def test_research_signal_payload_stays_on_legacy_transport(tmp_path: Path) -> None:
  class RejectTransportRuntime:
    root = tmp_path

    def create(self, *_args: object, **_kwargs: object) -> object:
      raise AssertionError("研究信号任务不得创建新 IPC 传输")

  payload = _payload("def on_bar(bar, state):\n  return []\n")
  payload["params"] = {
    "_researchSignalByBarTime": {"20260901093500": {"action": "long"}},
  }
  payload["strategy"] = "research_signal"
  session = BarStrategySession(
    ipc_mode="auto",
    market_data_runtime=RejectTransportRuntime(),  # type: ignore[arg-type]
  )

  result = _run_session(payload, session=session)

  assert result["trades"] == []


def test_legacy_shared_memory_and_mmap_results_are_identical(tmp_path: Path) -> None:
  payload = _payload("""
def on_bar(bar, state):
  state['seen'] = state.get('seen', 0) + 1
  if bar['time'] == '20260901093500':
    return [{'action': 'buy_open', 'quantity': 100, 'reason': 'same-result'}]
  return []
""")
  payload["bars"] = [
    {**payload["bars"][0], "time": "20260901093000", "open": 9.5, "close": 9.8},
    {**payload["bars"][0], "time": "20260901093500", "open": 10.0, "close": 10.1},
    {**payload["bars"][0], "time": "20260901094000", "open": 11.0, "close": 11.1},
  ]
  payload["warmupBarCount"] = 1
  legacy = _run_session(payload, session=BarStrategySession(ipc_mode="legacy"))
  results = []
  for transport in ("shared_memory", "mmap"):
    runtime = BarMarketDataRuntime(
      tmp_path / transport,
      shared_memory_limit_bytes=1024 * 1024,
    )
    try:
      results.append(_run_session(payload, session=BarStrategySession(
        ipc_mode="auto",
        market_data_runtime=runtime,
        transport_override=transport,
      )))
    finally:
      runtime.close()

  assert results == [legacy, legacy]


@pytest.mark.parametrize("transport", ["shared_memory", "mmap"])
def test_market_data_resource_is_cleaned_after_child_process_exit(
  tmp_path: Path,
  transport: str,
) -> None:
  payload = _payload("""
import os

def on_bar(bar, state):
  os._exit(9)
""")
  runtime = BarMarketDataRuntime(
    tmp_path / f"exit-{transport}",
    shared_memory_limit_bytes=1024 * 1024,
  )
  session = BarStrategySession(
    ipc_mode="auto",
    market_data_runtime=runtime,
    transport_override=transport,
  )

  with pytest.raises(BarStrategyProcessError) as raised:
    _run_session(payload, session=session)

  assert raised.value.exit_code == 9
  assert list(runtime.root.glob("bars-*.npy")) == []
  assert list(runtime.root.glob("shm-*.json")) == []
  runtime.close()


def test_owned_runtime_is_cleaned_when_market_data_creation_fails(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  managed_root = tmp_path / "create-failure"
  monkeypatch.setattr(settings, "BAR_IPC_TEMP_ROOT", str(managed_root))
  payload = _payload("def on_bar(bar, state):\n  return []\n")
  payload.pop("bars")
  payload["symbol"] = "688981.SH"
  empty = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

  with pytest.raises(ValueError, match="不能为空"):
    BarStrategySession(ipc_mode="auto").run_script(payload, empty)

  assert list(managed_root.iterdir()) == []


def test_bar_strategy_session_maps_negative_exit_code_to_signal() -> None:
  process = type("ExitedProcess", (), {"exitcode": -9})()

  error = _process_exit_error(process, "signal")  # type: ignore[arg-type]

  assert error.exit_code == -9
  assert error.termination_reason == "strategy_signal"
