"""通用 Bar 策略的元数据与工厂契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

import pandas as pd

from backend.backtesting.engine import BarStrategy


class BarStrategyFactory(Protocol):
  """创建一份独立的 Bar 策略运行实例。"""

  def __call__(
    self,
    config: dict[str, Any],
    htf_bars: pd.DataFrame | None,
    day_end_times: set[str],
  ) -> BarStrategy:
    ...


@dataclass(frozen=True)
class StrategyDefinition:
  """系统可运行策略的固定定义。"""

  strategy_id: str
  display_name: str
  description: str
  source_paths: tuple[str, ...]
  factory: BarStrategyFactory
