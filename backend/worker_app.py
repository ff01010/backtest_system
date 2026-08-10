# -*- coding: utf-8 -*-
"""隔离策略执行容器的内部 HTTP 接口。"""

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.backtesting import Bar, BarBacktestEngine, RecordedPriceExecutionModel
from backend.backtesting.legacy import fill_to_api_trade
from backend.strategies.scripted import ScriptedBarStrategy


class WorkerRunRequest(BaseModel):
  scriptId: str = Field(min_length=1)
  code: str = Field(min_length=1)
  params: dict[str, Any] = Field(default_factory=dict)
  bars: list[dict[str, Any]]
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  slippage: float = Field(default=0, ge=0, lt=1)
  holdingPolicy: str = "intraday"


app = FastAPI(title="Quant Backtest Strategy Worker")


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
  return {
    "cash": float(snapshot.cash),
    "equity": float(snapshot.equity),
    "longPositions": {
      symbol: {"quantity": position.quantity, "averagePrice": position.average_price}
      for symbol, position in snapshot.long_positions.items()
    },
    "shortPositions": {
      symbol: {"quantity": position.quantity, "averagePrice": position.average_price}
      for symbol, position in snapshot.short_positions.items()
    },
  }


@app.get("/health")
def health() -> dict[str, str]:
  return {"status": "ok"}


@app.post("/run")
def run(payload: WorkerRunRequest) -> dict[str, Any]:
  """在无数据库凭据和无宿主机挂载的容器内执行单次策略脚本。"""
  try:
    bars = [Bar(
      time=str(item["time"]),
      symbol=str(item["symbol"]),
      open=float(item["open"]),
      high=float(item["high"]),
      low=float(item["low"]),
      close=float(item["close"]),
      volume=float(item.get("volume", 0)),
    ) for item in payload.bars]
    strategy = ScriptedBarStrategy(payload.scriptId, payload.code, payload.params)
    engine = BarBacktestEngine(
      initial_cash=payload.initialCapital,
      commission_rate=payload.commissionRate,
      execution_model=RecordedPriceExecutionModel(slippage=payload.slippage),
      holding_policy=payload.holdingPolicy,
    )
    result = engine.run(strategy, bars)
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  return {
    "trades": [fill_to_api_trade(fill) for fill in result.fills],
    "portfolioSnapshots": [_snapshot_to_dict(item) for item in result.portfolio_snapshots],
    "skippedSignals": strategy.skipped_signals,
    "warnings": strategy.direction_warnings,
  }
