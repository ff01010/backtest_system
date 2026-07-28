"""按文件路径加载外部策略引擎，避免与系统内部策略目录同名冲突。"""

from __future__ import annotations

import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


def _code_root() -> Path:
  default_root = Path(__file__).resolve().parents[3] / "code"
  return Path(os.getenv("TREND_GRID_CODE_ROOT", default_root))


@lru_cache(maxsize=None)
def load_external_engine(strategy_id: str) -> type[Any]:
  """从外部策略目录加载 BacktestEngine，且每个策略仅加载一次。"""
  engine_path = _code_root() / "strategies" / strategy_id / "engine.py"
  if not engine_path.is_file():
    raise FileNotFoundError(f"未找到策略运行代码: {engine_path}")

  code_root = str(_code_root())
  if code_root not in sys.path:
    sys.path.insert(0, code_root)

  module_name = f"_backtest_external_{strategy_id}_engine"
  module = sys.modules.get(module_name)
  if module is None:
    spec = importlib.util.spec_from_file_location(module_name, engine_path)
    if spec is None or spec.loader is None:
      raise ImportError(f"无法加载策略运行代码: {engine_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
      spec.loader.exec_module(module)
    except Exception:
      sys.modules.pop(module_name, None)
      raise

  engine_class = getattr(module, "BacktestEngine", None)
  if not isinstance(engine_class, type):
    raise ImportError(f"策略运行代码未定义 BacktestEngine: {engine_path}")
  return engine_class
