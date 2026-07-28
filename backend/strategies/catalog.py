"""系统实际可运行策略的目录与代码快照。"""

from __future__ import annotations

import copy
import hashlib
import os
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd

from backend.backtesting.engine import BarStrategy
from backend.strategies.base import StrategyDefinition
from backend.strategies.external_engine import load_external_engine
from backend.strategies.trend_grid import TrendGridBarStrategy


def _trend_grid_root() -> Path:
  code_root = Path(os.getenv("TREND_GRID_CODE_ROOT", Path(__file__).resolve().parents[3] / "code"))
  return code_root / "strategies" / "trend_grid"


_MODULE_FLAGS: dict[str, tuple[bool, bool]] = {
  "trend_follow": (True, False),
  "grid_mean_reversion": (False, True),
  "trend_grid": (True, True),
  "trend_grid_both": (True, True),
}


def _create_trend_strategy(
  strategy_id: str,
  config: dict[str, Any],
  htf_bars: pd.DataFrame | None,
  day_end_times: set[str],
) -> BarStrategy:
  enable_a, enable_b = _MODULE_FLAGS[strategy_id]
  strategy_config = copy.deepcopy(config)
  strategy_config.setdefault("strategy", {}).update({
    "enable_a": enable_a,
    "enable_b": enable_b,
  })
  return TrendGridBarStrategy(
    strategy_config,
    load_external_engine("trend_grid"),
    htf_bars,
    day_end_times,
  )


def _trend_grid_sources() -> tuple[str, ...]:
  return (
    str(Path(__file__).with_name("trend_grid.py")),
    str(_trend_grid_root() / "engine.py"),
  )


_TREND_FOLLOW = StrategyDefinition(
  strategy_id="trend_follow",
  display_name="趋势跟踪",
  description="仅运行 A 模块：顺势开仓与平仓线退出。",
  source_paths=_trend_grid_sources(),
  factory=partial(_create_trend_strategy, "trend_follow"),
)

_GRID_MEAN_REVERSION = StrategyDefinition(
  strategy_id="grid_mean_reversion",
  display_name="网格均值回归",
  description="仅运行 B 模块：拐点反转与止损递增仓位。",
  source_paths=_trend_grid_sources(),
  factory=partial(_create_trend_strategy, "grid_mean_reversion"),
)

_TREND_GRID = StrategyDefinition(
  strategy_id="trend_grid",
  display_name="趋势网格组合",
  description="同时运行 A 模块趋势跟踪与 B 模块网格均值回归。",
  source_paths=_trend_grid_sources(),
  factory=partial(_create_trend_strategy, "trend_grid"),
)

_BAR_STRATEGIES: dict[str, StrategyDefinition] = {
  "trend_follow": _TREND_FOLLOW,
  "grid_mean_reversion": _GRID_MEAN_REVERSION,
  "trend_grid": _TREND_GRID,
  "trend_grid_both": StrategyDefinition(
    strategy_id="trend_grid_both",
    display_name="趋势网格组合",
    description=_TREND_GRID.description,
    source_paths=_TREND_GRID.source_paths,
    factory=partial(_create_trend_strategy, "trend_grid_both"),
  ),
}


def has_bar_strategy(strategy_id: str) -> bool:
  return strategy_id in _BAR_STRATEGIES


def get_bar_strategy_definition(strategy_id: str) -> StrategyDefinition:
  try:
    return _BAR_STRATEGIES[strategy_id]
  except KeyError as exc:
    raise ValueError(f"未接入 Bar 策略: {strategy_id}") from exc


def create_bar_strategy(
  strategy_id: str,
  config: dict[str, Any],
  htf_bars: pd.DataFrame | None,
  day_end_times: set[str],
) -> BarStrategy:
  return get_bar_strategy_definition(strategy_id).factory(config, htf_bars, day_end_times)


def get_strategy_source_snapshot(strategy_id: str) -> dict[str, Any]:
  definition = get_bar_strategy_definition(strategy_id)
  sources: list[dict[str, str]] = []
  digest = hashlib.sha256()
  for raw_path in definition.source_paths:
    path = Path(raw_path)
    code = path.read_text(encoding="utf-8")
    digest.update(path.name.encode("utf-8"))
    digest.update(code.encode("utf-8"))
    sources.append({"path": str(path), "code": code})
  return {
    "id": definition.strategy_id,
    "name": definition.display_name,
    "description": definition.description,
    "codeHash": digest.hexdigest(),
    "sources": sources,
  }
