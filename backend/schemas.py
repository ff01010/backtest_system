# -*- coding: utf-8 -*-
"""Pydantic 响应模型"""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class BarData(BaseModel):
  time: str
  open: float
  high: float
  low: float
  close: float
  volume: int


class TradeMarker(BaseModel):
  id: str
  barTime: str
  sourceTime: str
  module: str
  side: str
  action: str
  price: float
  quantity: int
  pnl: float | None
  detail: str


class ChartSummary(BaseModel):
  aBuy: int
  aSell: int
  bBuy: int
  bSell: int
  barCount: int
  warnings: list[str]


class DataFreshness(BaseModel):
  klineMtime: str | None
  signalMtime: str | None


class ChartResponse(BaseModel):
  symbol: str
  date: str
  timeframe: str
  bars: list[BarData]
  markers: list[TradeMarker]
  summary: ChartSummary
  dataFreshness: DataFreshness


class InstrumentItem(BaseModel):
  symbol: str
  name: str | None = None
  market: str | None = None
  enabled: bool = True
  note: str | None = None
  hasKline: bool
  hasSignal: bool


class InstrumentCreate(BaseModel):
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  name: str = ""
  note: str = ""
  enabled: bool = True


class InstrumentUpdate(BaseModel):
  name: str = ""
  note: str = ""
  enabled: bool = True


class BacktestAccount(BaseModel):
  id: int
  name: str
  initialCapital: float
  commissionRate: float
  slippage: float
  positionMode: str
  orderCash: float | None
  orderPct: float | None
  allowShort: bool
  isDefault: bool


class BacktestAccountCreate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  slippage: float = Field(ge=0)
  positionMode: str = "fixed_cash"
  orderCash: float | None = Field(default=None, ge=0)
  orderPct: float | None = Field(default=None, ge=0)
  allowShort: bool = True
  isDefault: bool = False


class BacktestAccountUpdate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  slippage: float = Field(ge=0)
  positionMode: str = "fixed_cash"
  orderCash: float | None = Field(default=None, ge=0)
  orderPct: float | None = Field(default=None, ge=0)
  allowShort: bool = True
  isDefault: bool = False


class BacktestRunRequest(BaseModel):
  configId: str | None = None
  configVersionId: str | None = None
  scriptId: str | None = None
  accountId: int | None = None
  account: str = "default"
  mode: str = Field(default="backtest", pattern=r"^(backtest|live)$")
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  benchmarkSymbol: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  strategy: str = "user_script"
  timeframe: str = "5m"
  startDate: str = Field(pattern=r"^\d{8}$")
  endDate: str = Field(pattern=r"^\d{8}$")
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  slippage: float = Field(ge=0)
  positionMode: str = "fixed_cash"
  orderCash: float | None = Field(default=None, ge=0)
  orderPct: float | None = Field(default=None, ge=0)
  allowLong: bool = True
  allowShort: bool = True
  holdingPolicy: Literal["intraday", "overnight"] = "intraday"
  strategyParams: dict[str, Any] = Field(default_factory=dict)
  runDraft: bool = False

  @model_validator(mode="after")
  def validate_trade_direction(self) -> "BacktestRunRequest":
    allow_long = getattr(self, "allowLong", True)
    allow_short = getattr(self, "allowShort", True)
    if not allow_long and not allow_short:
      raise ValueError("至少选择一个交易方向")
    return self


class BacktestSummary(BaseModel):
  totalPnl: float | None
  totalReturn: float | None
  maxDrawdown: float | None
  maxDrawdownRate: float | None
  annualVolatility: float | None
  winRate: float | None
  dailyWinRate: float | None
  tradeCount: int
  winningTrades: int
  losingTrades: int
  avgWin: float | None
  avgLoss: float | None
  profitLossRatio: float | None
  finalEquity: float | None
  annualReturn: float | None
  annualSharpe: float | None
  sortino: float | None


class EquityPoint(BaseModel):
  date: str
  pnl: float
  value: float


class BenchmarkPoint(BaseModel):
  date: str
  close: float
  value: float
  returnRate: float


class BacktestTrade(BaseModel):
  time: str
  direction: str
  side: str
  anchor: float
  price: float
  quantity: int
  pnl: float
  note: str
  signalTime: str | None = None
  executionTime: str | None = None
  priceSource: str | None = None
  action: str | None = None
  status: str = "filled"


class DailyHoldingPoint(BaseModel):
  date: str
  close: float
  longQty: int
  shortQty: int
  cash: float
  longMarketValue: float
  shortMarketValue: float
  marketValue: float
  totalEquity: float
  dailyPnl: float


class BacktestRunResponse(BaseModel):
  runId: str | None
  configId: str | None = None
  configVersionId: str | None = None
  scriptId: str | None = None
  outputDir: str
  summary: BacktestSummary
  equity: list[EquityPoint]
  dailyHoldings: list[DailyHoldingPoint] = []
  benchmarkSymbol: str | None
  benchmarkEquity: list[BenchmarkPoint]
  benchmarkReturn: float | None
  excessReturn: float | None
  trades: list[BacktestTrade]
  skippedSignals: list[BacktestTrade] = Field(default_factory=list)
  warnings: list[str]


class StrategyScript(BaseModel):
  id: str
  name: str
  description: str
  language: str
  code: str
  entryType: str
  status: str
  createdAt: str
  updatedAt: str


class StrategyScriptCreate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  description: str = ""
  code: str = Field(min_length=1)
  entryType: str = "on_bar"
  status: str = "draft"


class StrategyScriptUpdate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  description: str = ""
  code: str = Field(min_length=1)
  entryType: str = "on_bar"
  status: str = "draft"


class StrategyConfig(BaseModel):
  id: str
  name: str
  scriptId: str | None
  strategy: str
  mode: str
  symbol: str
  timeframe: str
  benchmarkSymbol: str | None
  accountId: int | None
  paramsJson: dict[str, Any]
  enabled: bool
  published: bool = True
  createdAt: str
  updatedAt: str


class StrategyConfigVersion(BaseModel):
  id: str
  configId: str
  versionNo: int
  name: str
  scriptId: str | None
  strategy: str
  mode: str
  symbol: str
  timeframe: str
  benchmarkSymbol: str | None
  accountId: int | None
  paramsJson: dict[str, Any]
  changeNote: str
  createdAt: str


class BacktestRunHistory(BaseModel):
  id: str
  configId: str | None
  configVersionId: str | None
  scriptId: str | None
  accountId: int | None
  symbol: str
  timeframe: str = "5m"
  fillModel: str = "bar_close_signal_fill"
  scriptSnapshot: dict[str, Any] | None = None
  paramsSnapshot: dict[str, Any] = Field(default_factory=dict)
  dataVersion: dict[str, Any] | None = None
  benchmarkSymbol: str | None
  strategy: str
  startDate: str
  endDate: str
  paramsJson: dict[str, Any]
  status: str
  summaryJson: dict[str, Any]
  outputDir: str
  note: str
  tags: list[str]
  createdAt: str
  finishedAt: str


class BacktestRunMetaUpdate(BaseModel):
  note: str = ""
  tags: list[str] = Field(default_factory=list)


class StrategyConfigCreate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  scriptId: str | None = None
  strategy: str = "user_script"
  mode: str = Field(default="backtest", pattern=r"^(backtest|live)$")
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  timeframe: str = "5m"
  benchmarkSymbol: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  accountId: int | None = None
  paramsJson: dict[str, Any] = Field(default_factory=dict)
  enabled: bool = True
  publish: bool = False


class StrategyConfigUpdate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  scriptId: str | None = None
  strategy: str = "user_script"
  mode: str = Field(default="backtest", pattern=r"^(backtest|live)$")
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  timeframe: str = "5m"
  benchmarkSymbol: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  accountId: int | None = None
  paramsJson: dict[str, Any] = Field(default_factory=dict)
  enabled: bool = True


class DataSyncRequest(BaseModel):
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  timeframe: str = "5m"
  startDate: str | None = Field(default=None, pattern=r"^\d{8}$")
  endDate: str | None = Field(default=None, pattern=r"^\d{8}$")
  mode: str = "incremental"


class DataSyncBatchRequest(BaseModel):
  symbols: list[str]
  timeframe: str = "5m"
  startDate: str | None = Field(default=None, pattern=r"^\d{8}$")
  endDate: str | None = Field(default=None, pattern=r"^\d{8}$")
  mode: str = "incremental"


class DataRefillRequest(BaseModel):
  symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  timeframe: str = "5m"
  dates: list[str]


class DateItem(BaseModel):
  date: str
  hasKline: bool
  hasSignal: bool


class ErrorResponse(BaseModel):
  error: str


class LoginRequest(BaseModel):
  username: str = Field(min_length=1, max_length=64)
  password: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
  username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
  displayName: str = Field(min_length=1, max_length=128)
  password: str = Field(min_length=6, max_length=256)
  role: Literal["admin", "user"] = "user"


class UserUpdate(BaseModel):
  displayName: str = Field(min_length=1, max_length=128)
  role: Literal["admin", "user"]
  status: Literal["active", "disabled"]


class PasswordReset(BaseModel):
  password: str = Field(min_length=6, max_length=256)
