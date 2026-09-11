"""用户策略的版本化 Python 运行时契约。"""

from __future__ import annotations

import builtins
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any


_MANIFEST_PATH = Path(__file__).with_name("runtime_manifest.json")
_LOCK_PATH = Path(__file__).resolve().parents[1] / "requirements.lock"


def _load_runtime_manifest() -> dict[str, Any]:
  value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
  if not isinstance(value, dict):
    raise RuntimeError("策略运行时能力清单格式无效")
  return value


_RUNTIME_MANIFEST = _load_runtime_manifest()
SCRIPT_RUNTIME_VERSION = str(_RUNTIME_MANIFEST["runtimeVersion"])
RUNTIME_CAPABILITY_DIGEST = str(_RUNTIME_MANIFEST["capabilityDigest"])
NUMERIC_THREAD_ENV = MappingProxyType({
  "MKL_NUM_THREADS": "1",
  "NUMEXPR_NUM_THREADS": "1",
  "OMP_NUM_THREADS": "1",
  "OPENBLAS_NUM_THREADS": "1",
})
SUPPORTED_QUANT_PACKAGE_VERSIONS = MappingProxyType({
  str(item["distribution"]): str(item["version"])
  for item in _RUNTIME_MANIFEST["supportedPackages"]
})
SUPPORTED_QUANT_IMPORTS = MappingProxyType({
  str(item["importName"]): str(item["distribution"])
  for item in _RUNTIME_MANIFEST["supportedPackages"]
})
SUPPORTED_STANDARD_LIBRARY_MODULES = frozenset(
  str(name) for name in _RUNTIME_MANIFEST["standardLibraryModules"]
)
BLOCKED_BUILTIN_NAMES = frozenset({
  "breakpoint",
  "compile",
  "eval",
  "exec",
  "exit",
  "help",
  "input",
  "open",
  "quit",
})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class StrategyRuntimePolicyError(ValueError):
  """策略调用了运行时产品契约明确禁止的内置能力。"""

  def __init__(self, name: str) -> None:
    super().__init__(f"策略运行环境不允许调用 {name}")
    self.name = name


class StrategyRuntimeVersionError(RuntimeError):
  """任务快照声明的策略运行时与当前执行器不兼容。"""

  def __init__(self, requested_version: str) -> None:
    super().__init__("任务策略运行时版本与当前执行器不兼容")
    self.requested_version = requested_version


class StrategyRuntimeCapabilityError(RuntimeError):
  """任务快照声明的能力摘要与当前执行器不兼容。"""

  def __init__(self, requested_digest: str) -> None:
    super().__init__("任务策略运行时能力摘要与当前执行器不兼容")
    self.requested_digest = requested_digest


class RuntimeDependencyMissingError(RuntimeError):
  """平台承诺的依赖或其锁定传递依赖缺失。"""

  def __init__(self, requested_module: str, missing_module: str, supported_package: str) -> None:
    super().__init__("平台策略运行时缺少承诺依赖")
    self.requested_module = requested_module
    self.missing_module = missing_module
    self.supported_package = supported_package


class RuntimeDependencyLoadError(RuntimeError):
  """平台承诺依赖存在，但二进制扩展无法加载。"""

  def __init__(self, requested_module: str, supported_package: str) -> None:
    super().__init__("平台策略运行时依赖加载失败")
    self.requested_module = requested_module
    self.supported_package = supported_package


class StrategyModuleUnavailableError(ValueError):
  """策略请求了当前固定镜像未提供且未承诺的模块。"""

  def __init__(self, requested_module: str) -> None:
    super().__init__("当前策略运行时不提供请求的模块")
    self.requested_module = requested_module


def _blocked_builtin(name: str) -> Callable[..., Any]:
  def reject(*_: Any, **__: Any) -> Any:
    raise StrategyRuntimePolicyError(name)

  return reject


def _runtime_builtins() -> MappingProxyType[str, Any]:
  """提供固定 Python 的标准内置能力；禁止项只定义产品行为。"""
  values = dict(vars(builtins))
  for name in BLOCKED_BUILTIN_NAMES:
    values[name] = _blocked_builtin(name)
  return MappingProxyType(values)


RUNTIME_BUILTINS = _runtime_builtins()


def restore_numeric_thread_environment() -> None:
  """策略子进程清空服务配置后恢复固定的数值线程上限。"""
  for name, value in NUMERIC_THREAD_ENV.items():
    os.environ[name] = value


def _canonical_manifest_digest(manifest: dict[str, Any]) -> str:
  digest_input = {key: value for key, value in manifest.items() if key != "capabilityDigest"}
  encoded = json.dumps(
    digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  return hashlib.sha256(encoded).hexdigest()


def _installed_package_versions() -> dict[str, str]:
  versions: dict[str, str] = {}
  for import_name, distribution in SUPPORTED_QUANT_IMPORTS.items():
    try:
      actual_version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
      raise RuntimeDependencyMissingError(import_name, import_name, distribution) from error
    expected_version = SUPPORTED_QUANT_PACKAGE_VERSIONS[distribution]
    if actual_version != expected_version:
      raise RuntimeDependencyLoadError(import_name, distribution)
    try:
      available = importlib.util.find_spec(import_name) is not None
    except (ImportError, AttributeError, ValueError) as error:
      raise RuntimeDependencyLoadError(import_name, distribution) from error
    if not available:
      raise RuntimeDependencyMissingError(import_name, import_name, distribution)
    versions[distribution] = actual_version
  return versions


def declared_runtime_capabilities() -> dict[str, object]:
  """返回 manifest 声明的安全字段，不替代实际环境校验。"""
  return {
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    "pythonVersion": str(_RUNTIME_MANIFEST["pythonVersion"]),
    "platform": str(_RUNTIME_MANIFEST["platform"]),
    "packages": dict(SUPPORTED_QUANT_PACKAGE_VERSIONS),
  }


def runtime_capabilities() -> dict[str, object]:
  """轻量校验 manifest、锁文件和已安装依赖元数据。"""
  if _canonical_manifest_digest(_RUNTIME_MANIFEST) != RUNTIME_CAPABILITY_DIGEST:
    raise RuntimeError("策略运行时能力摘要校验失败")
  lock_digest = hashlib.sha256(_LOCK_PATH.read_bytes()).hexdigest()
  if lock_digest != _RUNTIME_MANIFEST["requirementsLockSha256"]:
    raise RuntimeError("策略运行时依赖锁摘要校验失败")
  python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
  machine = platform.machine().lower()
  current_platform = f"linux-{'x86_64' if machine == 'amd64' else machine}"
  if python_version != _RUNTIME_MANIFEST["pythonVersion"]:
    raise RuntimeError("策略运行时 Python 版本不一致")
  if current_platform != _RUNTIME_MANIFEST["platform"]:
    raise RuntimeError("策略运行时平台不一致")
  capabilities = declared_runtime_capabilities()
  capabilities["packages"] = _installed_package_versions()
  return capabilities


def _probe_calculations() -> None:
  import numpy as np
  import pandas as pd
  import scipy.stats
  import statsmodels.api as sm
  import talib

  values = np.array([1.0, 2.0, 3.0, 4.0], dtype=float)
  if float(values.sum()) != 10.0 or float(pd.Series(values).rolling(2).sum().iloc[-1]) != 7.0:
    raise RuntimeError("数值基础计算失败")
  if float(scipy.stats.norm.cdf(0.0)) != 0.5 or float(talib.SMA(values, timeperiod=2)[-1]) != 3.5:
    raise RuntimeError("量化指标计算失败")
  model = sm.OLS(values, np.ones((4, 1), dtype=float)).fit()
  if not np.isclose(float(model.params[0]), 2.5):
    raise RuntimeError("统计模型计算失败")


def run_runtime_deep_probe(timeout_seconds: int = 20) -> dict[str, object]:
  """在短生命周期子进程中真实导入承诺包并执行最小计算。"""
  capabilities = runtime_capabilities()
  environment = dict(os.environ)
  environment.update(NUMERIC_THREAD_ENV)
  completed = subprocess.run(
    [sys.executable, "-m", "backend.strategies.runtime_contract", "--deep-probe"],
    capture_output=True, check=False, env=environment, text=True, timeout=timeout_seconds,
  )
  if completed.returncode != 0:
    try:
      diagnostic = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
      raise RuntimeError("策略运行时深度探针失败") from error
    code = diagnostic.get("code")
    requested = str(diagnostic.get("requestedModule") or "unknown")
    supported = str(diagnostic.get("supportedPackage") or requested)
    if code == "runtime.dependency_missing":
      missing = str(diagnostic.get("missingModule") or requested)
      raise RuntimeDependencyMissingError(requested, missing, supported)
    raise RuntimeDependencyLoadError(requested, supported)
  return capabilities


def validate_runtime_version(version: object) -> None:
  """允许无版本的历史任务，新任务必须与当前固定运行时一致。"""
  if version in {None, "", "legacy", SCRIPT_RUNTIME_VERSION}:
    return
  raise StrategyRuntimeVersionError(str(version))


def validate_runtime_capability(version: object, capability_digest: object) -> None:
  """v3 任务必须同时匹配版本与能力摘要；历史任务仍按旧规则读取。"""
  validate_runtime_version(version)
  if version == SCRIPT_RUNTIME_VERSION and capability_digest != RUNTIME_CAPABILITY_DIGEST:
    raise StrategyRuntimeCapabilityError(str(capability_digest or "missing"))


def drop_strategy_process_privileges() -> None:
  """Linux Runner 以 root 启动时，将用户策略子进程降为 nobody。"""
  if os.name != "posix" or not hasattr(os, "getuid") or os.getuid() != 0:
    return
  os.setgroups([])
  os.setgid(65534)
  os.setuid(65534)


def is_standard_builtin_name(name: str | None) -> bool:
  return bool(name and name in vars(builtins))


def is_safe_module_name(name: str | None) -> bool:
  return bool(name and _DOTTED_IDENTIFIER.fullmatch(name))


def supported_package_for_import_error(error: BaseException) -> tuple[str, str] | None:
  """仅根据受控模块名和 traceback 位置判断平台承诺包。"""
  missing = getattr(error, "name", None)
  missing_module = str(missing) if is_safe_module_name(missing) else ""
  top_level = missing_module.partition(".")[0]
  if missing_module == top_level and top_level in SUPPORTED_QUANT_IMPORTS:
    return top_level, SUPPORTED_QUANT_IMPORTS[top_level]
  for frame in reversed(traceback.extract_tb(error.__traceback__)):
    normalized = frame.filename.replace("\\", "/")
    for import_name, distribution in SUPPORTED_QUANT_IMPORTS.items():
      if f"/{import_name}/" in normalized:
        return import_name, distribution
  return None


def missing_name(error: BaseException) -> str | None:
  """只提取单个 Python 标识符，不返回任意异常正文。"""
  value = getattr(error, "name", None)
  if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
    return value
  text = str(error)
  for pattern in (
    r"name ['\"]([^'\"]+)['\"] is not defined",
    r"local variable ['\"]([^'\"]+)['\"]",
    r"free variable ['\"]([^'\"]+)['\"]",
  ):
    match = re.search(pattern, text)
    if match and _IDENTIFIER.fullmatch(match.group(1)):
      return match.group(1)
  return None


def strategy_source_line(error: BaseException) -> int | None:
  """定位动态策略文件行号，不返回源码行。"""
  frames = traceback.extract_tb(error.__traceback__)
  strategy_frames = [frame for frame in frames if frame.filename.startswith("strategy_script_")]
  return strategy_frames[-1].lineno if strategy_frames else None


def _supported_package_for_error(error: BaseException) -> tuple[str, str]:
  supported = supported_package_for_import_error(error)
  return supported or ("unknown", "unknown")


def _deep_probe_main() -> int:
  restore_numeric_thread_environment()
  try:
    runtime_capabilities()
    _probe_calculations()
  except ModuleNotFoundError as error:
    requested, supported = _supported_package_for_error(error)
    print(json.dumps({
      "code": "runtime.dependency_missing",
      "requestedModule": requested,
      "missingModule": str(error.name or "unknown"),
      "supportedPackage": supported,
    }))
    return 1
  except RuntimeDependencyLoadError as error:
    print(json.dumps({
      "code": "runtime.dependency_load_failed",
      "requestedModule": error.requested_module,
      "supportedPackage": error.supported_package,
    }))
    return 1
  except (ImportError, OSError) as error:
    requested, supported = _supported_package_for_error(error)
    print(json.dumps({
      "code": "runtime.dependency_load_failed",
      "requestedModule": requested,
      "supportedPackage": supported,
    }))
    return 1
  except RuntimeDependencyMissingError as error:
    print(json.dumps({
      "code": "runtime.dependency_missing",
      "requestedModule": error.requested_module,
      "missingModule": error.missing_module,
      "supportedPackage": error.supported_package,
    }))
    return 1
  except Exception:
    print(json.dumps({"code": "runtime.dependency_load_failed"}))
    return 1
  print(json.dumps({"status": "ok", "capabilityDigest": RUNTIME_CAPABILITY_DIGEST}))
  return 0


if __name__ == "__main__" and "--deep-probe" in sys.argv:
  raise SystemExit(_deep_probe_main())
