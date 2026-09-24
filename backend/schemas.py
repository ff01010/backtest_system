# -*- coding: utf-8 -*-
"""Pydantic 响应模型"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.date_codec import TradingDateError, format_api_date


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
  commissionRate: float = Field(default=0.00008, ge=0)
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
  signalDatasetId: str | None = None
  timeframe: str = "5m"
  startDate: str
  endDate: str
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  slippage: float = Field(ge=0)
  positionMode: str = "fixed_cash"
  orderCash: float | None = Field(default=None, ge=0)
  orderPct: float | None = Field(default=None, ge=0)
  allowLong: bool = True
  allowShort: bool = True
  strategyCategory: Literal["core_position_overlay", "directional_holding"] = "directional_holding"
  holdingPolicy: Literal["intraday", "overnight"] = "overnight"
  overlaySettlementPolicy: Literal["intraday_restore", "cross_day_carry"] = "intraday_restore"
  fillModel: Literal["bar_next_open", "l2_quote"] = "bar_next_open"
  executionAlgorithm: Literal["immediate", "twap"] = "immediate"
  executionParameters: dict[str, Any] = Field(default_factory=dict)
  baseQuantity: int = Field(default=0, ge=0)
  baseCostPrice: float = Field(default=0, ge=0)
  initialPositionEnabled: bool = False
  t0Enabled: bool = False
  warmupTradingDays: int = Field(default=0, ge=0, le=250)
  strategyParams: dict[str, Any] = Field(default_factory=dict)
  runDraft: bool = False

  @field_validator("startDate", "endDate", mode="before")
  @classmethod
  def normalize_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error

  @model_validator(mode="after")
  def validate_trade_direction(self) -> "BacktestRunRequest":
    allow_long = getattr(self, "allowLong", True)
    allow_short = getattr(self, "allowShort", True)
    if not allow_long and not allow_short:
      raise ValueError("至少选择一个交易方向")
    if self.strategyCategory == "core_position_overlay":
      if not self.initialPositionEnabled:
        raise ValueError("底仓增强策略必须启用初始底仓")
      if self.baseQuantity <= 0 or self.baseCostPrice <= 0:
        raise ValueError("底仓增强策略必须填写有效的底仓数量和成本价")
      if not self.t0Enabled:
        raise ValueError("底仓增强策略必须启用底仓做 T")
    elif self.strategyCategory == "directional_holding":
      if self.holdingPolicy != "overnight":
        raise ValueError("方向性持仓策略必须允许跨日持仓")
      if self.t0Enabled:
        raise ValueError("方向性持仓策略不能启用底仓做 T")
      if self.initialPositionEnabled:
        if self.baseQuantity <= 0 or self.baseCostPrice <= 0:
          raise ValueError("启用初始底仓时必须填写有效的底仓数量和成本价")
      elif self.baseQuantity != 0 or self.baseCostPrice != 0:
        raise ValueError("未启用初始底仓时底仓数量和成本价必须为 0")
    if self.baseQuantity == 0 and self.baseCostPrice != 0:
      raise ValueError("底仓数量为 0 时底仓成本价必须为 0")
    if self.baseQuantity > 0 and self.baseCostPrice <= 0:
      raise ValueError("底仓数量大于 0 时必须填写底仓成本价")
    if self.t0Enabled and self.baseQuantity <= 0:
      raise ValueError("启用底仓做 T 时底仓数量必须大于 0")
    if self.executionAlgorithm == "twap" and self.fillModel != "l2_quote":
      raise ValueError("TWAP 仅支持 L2 盘口回放")
    return self


class ApiKeyCreateRequest(BaseModel):
  name: str = Field(min_length=1, max_length=80)
  scopes: list[Literal["backtest:submit", "backtest:read", "backtest:cancel"]]
  expiresAt: datetime | None = None


class PublicBacktestOverrides(BaseModel):
  start_date: str | None = None
  end_date: str | None = None
  initial_capital: float | None = Field(default=None, gt=0)
  commission_rate: float | None = Field(default=None, ge=0)
  slippage: float | None = Field(default=None, ge=0)

  @field_validator("start_date", "end_date", mode="before")
  @classmethod
  def normalize_optional_date(cls, value: object) -> str | None:
    if value is None:
      return None
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error


class PublicBacktestCreateRequest(BaseModel):
  config_id: str = Field(min_length=1, max_length=64)
  overrides: PublicBacktestOverrides = Field(default_factory=PublicBacktestOverrides)


class BacktestSummary(BaseModel):
  totalPnl: float | None
  accountPnl: float | None = None
  accountRealizedPnl: float | None = None
  totalReturn: float | None
  baseHoldingPnl: float | None = None
  baseHoldingReturn: float | None = None
  t0ExcessPnl: float | None = None
  t0ExcessReturn: float | None = None
  baseBenchmarkPnl: float | None = None
  overlayPnl: float | None = None
  realizedOverlayPnl: float | None = None
  unrealizedOverlayPnl: float | None = None
  completedCyclePnl: float | None = None
  completedCycleCount: int = 0
  openCycleCount: int = 0
  winningCycles: int = 0
  losingCycles: int = 0
  breakevenCycles: int = 0
  cycleWinRate: float | None = None
  tradeMetricVersion: int | None = None
  completedTradeCount: int = 0
  openTradeCount: int = 0
  breakevenTrades: int = 0
  maxDrawdown: float | None
  maxDrawdownRate: float | None
  annualVolatility: float | None
  winRate: float | None
  dailyWinRate: float | None
  tradeCount: int
  orderCount: int | None = None
  fillCount: int | None = None
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
  researchSignalId: str | None = None
  researchSignalAction: Literal["long", "short", "flat"] | None = None
  researchSignalEffectiveAt: str | None = None
  fillId: str | None = None
  positionKey: str | None = None
  inventorySource: str | None = None
  cycleId: str | None = None
  cycleEffect: str | None = None
  overlayQuantityBefore: int | None = None
  overlayQuantityAfter: int | None = None
  accountRealizedPnl: float | None = None
  realizedOverlayPnl: float | None = None
  totalFee: float | None = None
  attributionSegments: list[dict[str, Any]] = Field(default_factory=list)


class BacktestChartTrade(BaseModel):
  id: str
  executionTime: str
  signalTime: str
  action: str
  side: str
  price: float
  quantity: int
  note: str


class BacktestChartResponse(BaseModel):
  runId: str
  displayNo: int
  symbol: str
  timeframe: str
  date: str
  strategyCategory: Literal["core_position_overlay", "directional_holding"]
  bars: list[dict[str, float | str | int]]
  trades: list[BacktestChartTrade]


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
  targetBaseQuantity: int | None = None
  actualQuantity: int | None = None
  overlayQuantity: int | None = None
  sellableQuantity: int | None = None
  dailyAccountPnl: float | None = None
  dailyBaseBenchmarkPnl: float | None = None
  dailyOverlayPnl: float | None = None
  dailyRealizedOverlayPnl: float | None = None
  unrealizedOverlayPnl: float | None = None


class BacktestRunResponse(BaseModel):
  runId: str | None
  displayNo: int | None = None
  configId: str | None = None
  configVersionId: str | None = None
  scriptId: str | None = None
  strategyCategory: Literal["core_position_overlay", "directional_holding"] | None = None
  resultSchemaVersion: int | None = None
  strategyMetrics: dict[str, Any] = Field(default_factory=dict)
  outputDir: str
  summary: BacktestSummary
  equity: list[EquityPoint]
  dailyHoldings: list[DailyHoldingPoint] = []
  cycles: list[dict[str, Any]] = Field(default_factory=list)
  executionPlans: list[dict[str, Any]] = Field(default_factory=list)
  benchmarkSymbol: str | None
  benchmarkEquity: list[BenchmarkPoint]
  benchmarkReturn: float | None
  excessReturn: float | None
  trades: list[BacktestTrade]
  skippedSignals: list[BacktestTrade] = Field(default_factory=list)
  experimentManifest: dict[str, Any] = Field(default_factory=dict)
  dataSnapshot: dict[str, Any] = Field(default_factory=dict)
  warnings: list[str]


class StrategyScript(BaseModel):
  id: str
  versionId: str | None = None
  versionNo: int = 1
  name: str
  description: str
  language: str
  code: str
  entryType: str
  signalRequired: bool = False
  status: str
  contentFingerprint: str = ""
  createdAt: str
  updatedAt: str


class StrategyScriptCreate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  description: str = ""
  code: str = Field(min_length=1)
  entryType: str = "on_bar"
  signalRequired: bool = False
  status: str = "draft"


class StrategyScriptUpdate(BaseModel):
  name: str = Field(min_length=1, max_length=64)
  description: str = ""
  code: str = Field(min_length=1)
  entryType: str = "on_bar"
  signalRequired: bool = False
  status: str = "draft"


class StrategyConfig(BaseModel):
  id: str
  name: str
  scriptId: str | None
  strategyVersionId: str | None = None
  scriptVersionNo: int = 1
  description: str = ""
  code: str = ""
  entryType: str = "on_bar"
  signalRequired: bool = False
  strategy: str
  mode: str
  symbol: str
  timeframe: str
  benchmarkSymbol: str | None
  accountId: int | None
  paramsJson: dict[str, Any]
  enabled: bool
  published: bool = True
  publishedVersionId: str | None = None
  revision: int = 1
  sharedSource: dict[str, Any] | None = None
  createdAt: str
  updatedAt: str


class StrategyConfigVersion(BaseModel):
  id: str
  configId: str
  strategyVersionId: str | None = None
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
  displayNo: int
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
  isFavorited: bool = False
  favoritedAt: str | None = None
  queuedAt: str = ""
  startedAt: str = ""
  cancelRequested: bool = False
  createdAt: str
  finishedAt: str
  publicationId: str | None = None


class SharedBacktestPublicationCreate(BaseModel):
  title: str = Field(min_length=1, max_length=128)
  description: str = Field(min_length=1, max_length=4000)
  tags: list[str] = Field(default_factory=list, max_length=10)

  @field_validator("title", "description")
  @classmethod
  def normalize_required_text(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("内容不能为空")
    return normalized

  @field_validator("tags")
  @classmethod
  def normalize_tags(cls, value: list[str]) -> list[str]:
    tags: list[str] = []
    for item in value:
      normalized = item.strip()
      if not normalized or normalized in tags:
        continue
      if len(normalized) > 32:
        raise ValueError("单个标签不能超过 32 个字符")
      tags.append(normalized)
    if len(tags) > 10:
      raise ValueError("标签不能超过 10 个")
    return tags


class SharedBacktestMetaUpdate(SharedBacktestPublicationCreate):
  """作者可修改的共享展示信息。"""


class SharedBacktestRemovalRequest(BaseModel):
  reason: str = Field(min_length=1, max_length=1000)

  @field_validator("reason")
  @classmethod
  def normalize_reason(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("下架原因不能为空")
    return normalized


class SharedBacktestListItem(BaseModel):
  id: str
  title: str
  authorName: str
  tags: list[str]
  symbol: str
  timeframe: str
  strategyCategory: str
  startDate: str
  endDate: str
  totalReturn: float | None
  maxDrawdownRate: float | None
  annualSharpe: float | None
  copyCount: int
  firstPublishedAt: str
  sourcePublicationId: str | None = None
  sourceAuthorName: str | None = None
  sourceTitle: str | None = None
  isFavorited: bool = False
  hasCopied: bool = False


class SharedBacktestManagementItem(SharedBacktestListItem):
  description: str
  status: str
  withdrawnAt: str | None
  removedAt: str | None
  removalReason: str | None
  updatedAt: str
  sourceRunAvailable: bool


class SharedBacktestArtifactMetadata(BaseModel):
  id: str
  type: str
  displayName: str
  contentType: str | None
  byteSize: int
  contentSha256: str
  snapshotSchemaVersion: int
  reproduction: dict[str, Any]
  downloadUrl: str


class SharedBacktestDetail(SharedBacktestListItem):
  description: str
  snapshotSchemaVersion: int
  snapshotDigest: str
  strategy: dict[str, Any]
  configuration: dict[str, Any]
  data: dict[str, Any]
  result: dict[str, Any]
  artifacts: list[SharedBacktestArtifactMetadata] = Field(default_factory=list)
  origin: dict[str, Any]
  riskNotice: str


class SharedBacktestCopyResponse(BaseModel):
  configId: str
  scriptId: str
  name: str
  created: bool
  accountCleared: bool
  sharedSource: dict[str, Any]


class BacktestRunMetaUpdate(BaseModel):
  note: str = ""
  tags: list[str] = Field(default_factory=list)


class BacktestRunFavoriteUpdate(BaseModel):
  favorited: bool


class BacktestRunIdSelection(BaseModel):
  mode: Literal["ids"]
  runIds: list[UUID] = Field(min_length=1)


class BacktestRunFilterSelection(BaseModel):
  mode: Literal["filter"]
  query: str = Field(default="", max_length=100)
  symbol: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
  configId: str | None = None
  favoritesOnly: bool = False
  excludedRunIds: list[UUID] = Field(default_factory=list)


BacktestRunSelection = Annotated[
  BacktestRunIdSelection | BacktestRunFilterSelection,
  Field(discriminator="mode"),
]


class BacktestRunBatchDeleteRequest(BaseModel):
  runIds: list[str] | None = Field(default=None, min_length=1, max_length=100)
  selection: BacktestRunSelection | None = None
  expectedSelectedRunCount: int | None = Field(default=None, gt=0)
  expectedSelectionDigest: str | None = Field(
    default=None, pattern=r"^[0-9a-f]{64}$",
  )

  @model_validator(mode="after")
  def validate_delete_shape(self) -> "BacktestRunBatchDeleteRequest":
    if (self.runIds is None) == (self.selection is None):
      raise ValueError("runIds 和 selection 必须且只能提供一个")
    if self.selection is not None and (
      self.expectedSelectedRunCount is None or self.expectedSelectionDigest is None
    ):
      raise ValueError("公共选择删除必须携带已确认的数量和摘要")
    return self


class BacktestRunSelectionRequest(BaseModel):
  selection: BacktestRunSelection


class SignalDetailExportCreateRequest(BaseModel):
  selection: BacktestRunSelection


class BacktestCapacityOverrideRequest(BaseModel):
  limit: int = Field(ge=8, le=12)
  ttlSeconds: int = Field(ge=60, le=86400)
  reason: str = Field(min_length=1, max_length=500)


class BacktestDiagnosticRun(BaseModel):
  runId: str
  displayNo: int
  status: str
  safeReason: str
  symbol: str
  timeframe: str
  fillModel: str
  createdAt: str
  startedAt: str
  finishedAt: str


class BacktestAttemptDiagnostic(BaseModel):
  attemptNo: int
  attemptToken: str
  jobKind: Literal["bar", "l2"]
  workerId: str
  containerId: str
  processId: int | None
  applicationBuildId: str
  runtimeVersion: str
  capabilityDigest: str
  status: Literal["running", "succeeded", "failed", "cancelled", "interrupted"]
  stage: str
  startedAt: str
  heartbeatAt: str
  finishedAt: str
  exitCode: int | None
  terminationReason: str | None
  errorCode: str | None
  logCorrelationId: str


class BacktestDiagnosticError(BaseModel):
  id: int
  stage: str
  code: str
  reason: str
  exceptionType: str
  category: str
  retryable: bool
  occurredAt: str
  attemptNo: int | None
  attemptToken: str | None
  terminationReason: str | None
  exitCode: int | None
  logCorrelationId: str | None


class BacktestDiagnosticTimelineEvent(BaseModel):
  source: str | None = None
  event: str
  timestamp: str = ""
  occurredAt: str | None = None
  message: str
  attemptNo: int | None = None
  attemptToken: str | None = None
  stage: str | None = None
  status: str | None = None
  errorCode: str | None = None
  exceptionType: str | None = None
  frameSummary: str | None = None
  exitCode: int | None = None
  terminationReason: str | None = None
  level: str | None = None
  service: str | None = None
  jobKind: str | None = None
  workerId: str | None = None
  containerId: str | None = None
  processId: int | None = None
  durationMs: float | None = None
  applicationBuildId: str | None = None
  runtimeVersion: str | None = None
  capabilityDigest: str | None = None
  logTimestampNs: str | None = None


class BacktestRunDiagnostics(BaseModel):
  run: BacktestDiagnosticRun
  attempts: list[BacktestAttemptDiagnostic]
  errors: list[BacktestDiagnosticError]
  logStatus: Literal["available", "unavailable", "expired", "disabled"]
  timeline: list[BacktestDiagnosticTimelineEvent]


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


class DateItem(BaseModel):
  date: str
  hasKline: bool
  hasSignal: bool

class CompleteStrategySave(BaseModel):
  script: StrategyScriptCreate
  config: StrategyConfigCreate
  scriptId: str | None = None
  configId: str | None = None
  expectedRevision: int | None = Field(default=None, ge=1)


class ReleaseAcknowledgeRequest(BaseModel):
  clientBuildId: str = Field(pattern=r"^(dev|[0-9a-f]{40})$")


class ReleaseDetail(BaseModel):
  id: str
  releaseNo: int
  version: str
  title: str
  summary: str
  highlights: list[str]
  level: Literal["normal", "important", "mandatory"]
  publishedAt: str


class ReleaseStatusResponse(BaseModel):
  serverBuildId: str
  requiredClientCompatibilityVersion: int
  clientOutdated: bool
  activationKind: Literal["release", "silent", "rollback"]
  presentationMode: Literal[
    "mandatory_gate", "release_notice", "version_mismatch", "rollback_mismatch", "none",
  ]
  acknowledged: bool
  unread: bool
  upgradeRequired: bool
  release: ReleaseDetail | None


class ReleaseHistoryResponse(BaseModel):
  items: list[ReleaseDetail]
  nextCursor: str | None



class ErrorResponse(BaseModel):
  error: str


class LoginRequest(BaseModel):
  username: str = Field(min_length=1, max_length=64)
  password: str = Field(min_length=1, max_length=256)


class ResearchAuthorizationCodeRequest(BaseModel):
  state: str = Field(min_length=16, max_length=512)


class ResearchAuthorizationCodeExchangeRequest(ResearchAuthorizationCodeRequest):
  authorizationCode: str = Field(min_length=32, max_length=512)


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
