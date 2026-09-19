"""固定策略运行时能力与错误责任回归。"""

import os
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend.backtesting.models import Bar, PortfolioSnapshot
from backend.services.backtest_errors import StrategyExecutionError
from backend.strategies.runtime_contract import (
  NUMERIC_THREAD_ENV,
  RUNTIME_CAPABILITY_DIGEST,
  RuntimeDependencyLoadError,
  RuntimeDependencyMissingError,
  SCRIPT_RUNTIME_VERSION,
  restore_numeric_thread_environment,
)
from backend.strategies import scripted
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
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION, capability_digest=RUNTIME_CAPABILITY_DIGEST)

  assert strategy.on_bar(_bar(), _portfolio()) == []
  assert len(strategy._state["values"]) == 3
  assert strategy._state["window"] == [1, 2]


def test_blocked_builtin_has_policy_error_instead_of_name_error() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy("blocked", """
def on_bar(bar, state):
  open('forbidden.txt')
  return []
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION, capability_digest=RUNTIME_CAPABILITY_DIGEST)

  assert raised.value.detail.code == "strategy.runtime_policy_blocked"
  assert raised.value.detail.missing_name == "open"


def test_undefined_name_and_local_unbound_have_distinct_codes() -> None:
  undefined = ScriptedBarStrategy("undefined", """
def on_bar(bar, state):
  return missing_signal
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION, capability_digest=RUNTIME_CAPABILITY_DIGEST)
  with pytest.raises(StrategyExecutionError) as undefined_error:
    undefined.on_bar(_bar(), _portfolio())

  local = ScriptedBarStrategy("local", """
def on_bar(bar, state):
  if bar['close'] < 0:
    quantity = 100
  return quantity
""", {}, runtime_version=SCRIPT_RUNTIME_VERSION, capability_digest=RUNTIME_CAPABILITY_DIGEST)
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


def test_missing_runtime_capability_digest_is_platform_error() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy(
      "digest", "def on_bar(bar, state):\n  return []\n", {},
      runtime_version=SCRIPT_RUNTIME_VERSION,
    )

  assert raised.value.detail.category == "system_error"
  assert raised.value.detail.code == "runtime.version_mismatch"


def test_static_unknown_module_is_user_actionable() -> None:
  with pytest.raises(StrategyExecutionError) as raised:
    ScriptedBarStrategy(
      "unknown-module",
      "import module_that_is_not_installed_42\n\ndef on_bar(bar, state):\n  return []\n",
      {}, runtime_version=SCRIPT_RUNTIME_VERSION,
      capability_digest=RUNTIME_CAPABILITY_DIGEST,
    )

  assert raised.value.detail.category == "strategy_error"
  assert raised.value.detail.code == "strategy.module_unavailable"
  assert raised.value.detail.requested_module == "module_that_is_not_installed_42"


def test_supported_submodule_typo_is_not_platform_dependency_error() -> None:
  error = ModuleNotFoundError("No module named 'talib.nonexistent'", name="talib.nonexistent")

  detail = StrategyExecutionError.from_exception(error).detail

  assert detail.category == "strategy_error"
  assert detail.code == "strategy.module_unavailable"
  assert detail.requested_module == "talib.nonexistent"


def test_save_validation_returns_structured_unknown_module_error() -> None:
  with pytest.raises(HTTPException) as raised:
    api_app._validate_strategy_script({
      "code": "import module_that_is_not_installed_42\n\ndef on_bar(bar, state):\n  return []\n",
      "entryType": "on_bar",
      "status": "draft",
    })

  assert raised.value.status_code == 422
  detail = raised.value.detail["errorDetail"]
  assert detail["code"] == "strategy.module_unavailable"
  assert detail["requestedModule"] == "module_that_is_not_installed_42"


def test_save_validation_returns_503_when_supported_package_is_missing(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  real_find_spec = scripted.importlib.util.find_spec
  monkeypatch.setattr(
    scripted.importlib.util,
    "find_spec",
    lambda name: None if name == "talib" else real_find_spec(name),
  )

  with pytest.raises(HTTPException) as raised:
    api_app._validate_strategy_script({
      "code": "import talib\n\ndef on_bar(bar, state):\n  return []\n",
      "entryType": "on_bar",
      "status": "draft",
    })

  assert raised.value.status_code == 503
  assert raised.value.detail["errorDetail"]["code"] == "runtime.dependency_missing"


@pytest.mark.parametrize(
  ("error", "code"),
  [
    (RuntimeDependencyMissingError("talib", "talib._ta_lib", "TA-Lib"),
     "runtime.dependency_missing"),
    (RuntimeDependencyLoadError("talib", "TA-Lib"),
     "runtime.dependency_load_failed"),
  ],
)
def test_promised_dependency_failures_are_platform_errors(
  error: Exception,
  code: str,
) -> None:
  detail = StrategyExecutionError.from_exception(error).detail

  assert detail.category == "system_error"
  assert detail.code == code
  assert detail.supported_package == "TA-Lib"


def test_numeric_thread_environment_can_be_restored_after_cleanup(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  for name in NUMERIC_THREAD_ENV:
    monkeypatch.delenv(name, raising=False)

  restore_numeric_thread_environment()

  assert {name: os.environ.get(name) for name in NUMERIC_THREAD_ENV} == dict(
    NUMERIC_THREAD_ENV,
  )


def test_runtime_manifest_matches_lock_and_capability_digest() -> None:
  backend_dir = Path(__file__).resolve().parents[1]
  manifest = json.loads(
    (backend_dir / "strategies" / "runtime_manifest.json").read_text(encoding="utf-8"),
  )
  lock_digest = hashlib.sha256((backend_dir / "requirements.lock").read_bytes()).hexdigest()
  digest_input = {key: value for key, value in manifest.items() if key != "capabilityDigest"}
  canonical = json.dumps(
    digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")

  assert manifest["runtimeVersion"] == "python3.12-runtime-v3"
  assert manifest["requirementsLockSha256"] == lock_digest
  assert manifest["capabilityDigest"] == hashlib.sha256(canonical).hexdigest()


def test_runtime_manifest_base_image_matches_runtime_dockerfiles() -> None:
  repository_dir = Path(__file__).resolve().parents[2]
  manifest = json.loads(
    (repository_dir / "backend" / "strategies" / "runtime_manifest.json").read_text(
      encoding="utf-8",
    ),
  )

  for relative_path in ("deploy/Dockerfile.backend",):
    dockerfile = (repository_dir / relative_path).read_text(encoding="utf-8")
    from_line = next(
      line for line in dockerfile.splitlines() if line.strip().startswith("FROM ")
    )
    assert from_line.split()[1] == manifest["baseImage"]


def test_requirements_entry_remains_cross_platform() -> None:
  backend_dir = Path(__file__).resolve().parents[1]
  entries = [
    line.strip()
    for line in (backend_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
  ]

  assert entries == ["-r requirements.in"]
