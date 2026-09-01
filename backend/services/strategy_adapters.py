# -*- coding: utf-8 -*-
"""策略回测适配器注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class StrategyAdapter(Protocol):
  """策略接入 viewer 回测系统的最小接口。"""

  strategy_id: str
  display_name: str

  def get_parameter_schema(self) -> dict[str, Any]:
    """返回前端可渲染的参数说明。"""
    ...

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    """运行回测并返回 viewer 统一结果。"""
    ...


@dataclass(frozen=True)
class TrendGridAdapter:
  strategy_id: str
  display_name: str

  def get_parameter_schema(self) -> dict[str, Any]:
    return {}

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    from backend.services import backtest_runner

    return backtest_runner.run_trend_grid_backtest(payload)


@dataclass(frozen=True)
class MidcapEarningsAdapter:
  strategy_id: str
  display_name: str

  def get_parameter_schema(self) -> dict[str, Any]:
    return {
      "minMarketCap": {
        "type": "number",
        "label": "最小市值",
        "min": 0,
      },
      "maxMarketCap": {
        "type": "number",
        "label": "最大市值",
        "min": 0,
      },
      "minRevenueYoy": {
        "type": "number",
        "label": "营收增速下限",
      },
      "minNetProfitYoy": {
        "type": "number",
        "label": "净利润增速下限",
      },
      "maxCandidates": {
        "type": "number",
        "label": "最大候选数",
        "min": 1,
      },
      "maxPositions": {
        "type": "number",
        "label": "最大持仓数",
        "min": 1,
      },
      "singleWeight": {
        "type": "number",
        "label": "单股权重",
        "min": 0,
        "max": 1,
      },
      "maxTotalPct": {
        "type": "number",
        "label": "最大总仓位",
        "min": 0,
        "max": 1,
      },
      "rebalanceMonths": {
        "type": "text",
        "label": "调仓月份",
      },
      "minHoldingDays": {
        "type": "number",
        "label": "最短持有天数",
        "min": 0,
      },
      "stopLoss": {
        "type": "number",
        "label": "止损线",
        "max": 0,
      },
      "indexTrendFilter": {
        "type": "boolean",
        "label": "指数趋势过滤",
      },
    }

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    from backend.services import backtest_runner

    return backtest_runner.run_midcap_earnings_backtest(payload)


@dataclass(frozen=True)
class UserScriptAdapter:
  strategy_id: str = "user_script"
  display_name: str = "自定义脚本"

  def get_parameter_schema(self) -> dict[str, Any]:
    return {}

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    from backend.services import backtest_runner

    return backtest_runner.run_user_script_backtest(payload)


@dataclass(frozen=True)
class ResearchSignalAdapter:
  strategy_id: str = "research_signal"
  display_name: str = "研究信号数据集"

  def get_parameter_schema(self) -> dict[str, Any]:
    return {}

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    from backend.services import backtest_runner

    return backtest_runner.run_research_signal_backtest(payload)


_ADAPTERS: dict[str, StrategyAdapter] = {
  "user_script": UserScriptAdapter(),
  "research_signal": ResearchSignalAdapter(),
}

_VISIBLE_STRATEGY_IDS = ("user_script",)


def list_strategy_ids() -> list[str]:
  return list(_VISIBLE_STRATEGY_IDS)


def list_strategy_summaries() -> list[dict[str, str]]:
  return [
    {
      "id": adapter.strategy_id,
      "name": adapter.display_name,
    }
    for strategy_id in _VISIBLE_STRATEGY_IDS
    for adapter in (_ADAPTERS[strategy_id],)
  ]


def has_strategy(strategy_id: str) -> bool:
  return strategy_id in _ADAPTERS


def get_strategy_adapter(strategy_id: str) -> StrategyAdapter:
  return _ADAPTERS[strategy_id]


def get_parameter_schema(strategy_id: str) -> dict[str, Any]:
  return get_strategy_adapter(strategy_id).get_parameter_schema()
