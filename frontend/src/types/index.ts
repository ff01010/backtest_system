export interface BarData {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  id: string;
  barTime: string;
  sourceTime: string;
  module: 'A' | 'B';
  side: 'BUY' | 'SELL';
  action: 'OPEN' | 'CLOSE' | 'STOP' | 'FORCE';
  price: number;
  quantity: number;
  pnl: number | null;
  detail: string;
}

export interface ChartSummary {
  aBuy: number;
  aSell: number;
  bBuy: number;
  bSell: number;
  barCount: number;
  warnings: string[];
}

export interface DataFreshness {
  klineMtime: string | null;
  signalMtime: string | null;
}

export interface BarAnnotation {
  aPeakCand: boolean;
  aPeakConfirmed: number | null;
  aValleyCand: boolean;
  aValleyConfirmed: number | null;
  bPeakCand: boolean;
  bPeakConfirmed: number | null;
  bValleyCand: boolean;
  bValleyConfirmed: number | null;
  aPeakChain: number;
  aValleyChain: number;
  aCloseLineHit: string | null;
  bStopHit: string | null;
}

export interface TheoreticalSignal {
  barIndex: number;
  module: 'A' | 'B';
  type: string;
  price: number;
  chain: number;
}

export interface ChartResponse {
  symbol: string;
  date: string;
  timeframe: string;
  bars: BarData[];
  markers: TradeMarker[];
  annotations: BarAnnotation[];
  theoretical: TheoreticalSignal[];
  summary: ChartSummary;
  dataFreshness: DataFreshness;
}

export interface BacktestChartTrade {
  id: string;
  executionTime: string;
  signalTime: string;
  action: 'buy_open' | 'sell_close' | 'sell_open' | 'buy_close' | 'buy' | 'sell';
  side: 'BUY' | 'SELL';
  price: number;
  quantity: number;
  note: string;
}

export interface BacktestChartResponse {
  runId: string;
  displayNo: number;
  symbol: string;
  timeframe: string;
  date: string;
  strategyCategory: 'core_position_overlay' | 'directional_holding';
  bars: BarData[];
  trades: BacktestChartTrade[];
}

export interface InstrumentItem {
  symbol: string;
  name?: string | null;
  market?: string | null;
  enabled?: boolean;
  note?: string | null;
  hasKline: boolean;
  hasSignal: boolean;
}

export interface MarketInstrument {
  symbol: string;
  name: string;
  market: string;
  instrumentType: string;
  board: string;
}

export interface DateItem {
  date: string;
  hasKline: boolean;
  hasSignal: boolean;
}

export type DisplayModule = 'A' | 'B';
export type DisplaySide = 'BUY' | 'SELL';

export interface BacktestAccount {
  id: number;
  name: string;
  initialCapital: number;
  commissionRate: number;
  slippage: number;
  positionMode: 'fixed_cash' | 'full_available' | string;
  orderCash: number | null;
  orderPct: number | null;
  allowShort: boolean;
  isDefault: boolean;
}

export type NullableMetric = number | null;

export interface BacktestSummary {
  totalPnl: NullableMetric;
  totalReturn: NullableMetric;
  baseHoldingPnl?: NullableMetric;
  baseHoldingReturn?: NullableMetric;
  t0ExcessPnl?: NullableMetric;
  t0ExcessReturn?: NullableMetric;
  accountPnl?: NullableMetric;
  accountRealizedPnl?: NullableMetric;
  baseBenchmarkPnl?: NullableMetric;
  overlayPnl?: NullableMetric;
  realizedOverlayPnl?: NullableMetric;
  unrealizedOverlayPnl?: NullableMetric;
  completedCyclePnl?: NullableMetric;
  completedCycleCount?: number;
  openCycleCount?: number;
  winningCycles?: number;
  losingCycles?: number;
  breakevenCycles?: number;
  cycleWinRate?: NullableMetric;
  tradeMetricVersion?: number;
  completedTradeCount?: number;
  openTradeCount?: number;
  breakevenTrades?: number;
  maxDrawdown: NullableMetric;
  maxDrawdownRate: NullableMetric;
  annualVolatility: NullableMetric;
  winRate: NullableMetric;
  dailyWinRate: NullableMetric;
  tradeCount: number;
  orderCount?: number;
  fillCount?: number;
  winningTrades: number;
  losingTrades: number;
  avgWin: NullableMetric;
  avgLoss: NullableMetric;
  profitLossRatio: NullableMetric;
  finalEquity: NullableMetric;
  annualReturn: NullableMetric;
  annualSharpe: NullableMetric;
  sortino: NullableMetric;
}

export interface EquityPoint {
  date: string;
  pnl: number;
  value: number;
}

export interface BenchmarkPoint {
  date: string;
  close: number;
  value: number;
  returnRate: number;
}

export interface BacktestTrade {
  time: string;
  signalTime?: string;
  executionTime?: string;
  parentOrderId?: string;
  childIndex?: number;
  executionAlgorithm?: string;
  priceSource?: string;
  fundingLimited?: boolean;
  depthQuantity?: number;
  affordableQuantity?: number;
  remainingQuantity?: number;
  availableCashBefore?: number;
  action?: string;
  status?: 'filled' | 'skipped' | string;
  direction: string;
  side: string;
  anchor: number;
  fillId?: string;
  positionKey?: string;
  inventorySource?: string;
  cycleId?: string | null;
  cycleEffect?: 'open' | 'increase' | 'partial_close' | 'close' | 'reverse' | string;
  overlayQuantityBefore?: number;
  overlayQuantityAfter?: number;
  accountRealizedPnl?: number;
  realizedOverlayPnl?: number;
  totalFee?: number;
  attributionSegments?: Array<Record<string, unknown>>;
  price: number;
  quantity: number;
  pnl: number;
  note: string;
  strategyModule?: string;
  researchSignalId?: string;
  researchSignalAction?: 'long' | 'short' | 'flat';
  researchSignalEffectiveAt?: string;
}

export interface ExecutionPlan {
  parentOrderId: string;
  algorithm: string;
  signalTime: string;
  action: string;
  requestedQuantity: number;
  filledQuantity: number;
  cancelledQuantity: number;
  rejectedQuantity: number;
  remainingQuantity: number;
  status: string;
  reason: string;
  plannedChildCount: number;
}

export interface DailyHoldingPoint {
  date: string;
  close: number;
  longQty: number;
  shortQty: number;
  cash: number;
  longMarketValue: number;
  shortMarketValue: number;
  marketValue: number;
  totalEquity: number;
  dailyPnl: number;
  targetBaseQuantity?: number | null;
  actualQuantity?: number | null;
  overlayQuantity?: number | null;
  sellableQuantity?: number | null;
  dailyAccountPnl?: number | null;
  dailyBaseBenchmarkPnl?: number | null;
  dailyOverlayPnl?: number | null;
  dailyRealizedOverlayPnl?: number | null;
  unrealizedOverlayPnl?: number | null;
}

export interface OverlayCycle {
  cycleId: string;
  symbol: string;
  direction: 'long' | 'short';
  openedAt: string;
  closedAt: string | null;
  openedQuantity: number;
  closedQuantity: number;
  remainingQuantity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  netPnl: number;
  fillIds: string[];
  status: 'open' | 'completed';
}

export interface BacktestRunResponse {
  runId: string | null;
  displayNo?: number | null;
  configId: string | null;
  configVersionId: string | null;
  scriptId: string | null;
  strategyCategory?: 'core_position_overlay' | 'directional_holding';
  resultSchemaVersion?: number;
  strategyMetrics?: {
    t0Metrics?: Record<string, NullableMetric>;
    holdingMetrics?: Record<string, NullableMetric>;
  };
  outputDir: string;
  summary: BacktestSummary;
  equity: EquityPoint[];
  dailyHoldings: DailyHoldingPoint[];
  benchmarkSymbol: string | null;
  benchmarkEquity: BenchmarkPoint[];
  benchmarkReturn: NullableMetric;
  excessReturn: NullableMetric;
  trades: BacktestTrade[];
  executionPlans?: ExecutionPlan[];
  skippedSignals?: BacktestTrade[];
  cycles?: OverlayCycle[];
  experimentManifest?: ExperimentManifest;
  dataSnapshot?: Record<string, unknown>;
  warnings: string[];
}

export interface BacktestErrorDetail {
  category: 'strategy_error' | 'system_error';
  code: string;
  message: string;
  hint?: string | null;
  callback?: 'on_bar' | 'on_quote' | 'on_order_event' | null;
  eventTime?: string | null;
  symbol?: string | null;
  retryable: boolean;
  taskId?: string | null;
  missingName?: string | null;
  strategyLine?: number | null;
  runtimeVersion?: string | null;
  requestedModule?: string | null;
  missingModule?: string | null;
  supportedPackage?: string | null;
}

export interface ExperimentManifest {
  experimentHash: string;
  engineVersion: string;
  strategyRuntimeVersion?: string;
  strategyRuntimeCapabilityDigest?: string;
  signalModel: string;
  fillModel: string;
  randomSeed: number;
  strategyCodeHash: string;
  configHash: string;
  dataHash: string;
  barCount: number;
  barRange: {
    start: string;
    end: string;
    timeframe: string;
  };
  costModel: {
    commissionRate: number;
    slippage: number;
  };
  holdingPolicy?: 'intraday' | 'overnight';
  overlaySettlementPolicy?: 'intraday_restore' | 'cross_day_carry';
  strategyCategory?: 'core_position_overlay' | 'directional_holding';
  resultSchemaVersion?: number;
}

export interface StrategyScript {
  id: string;
  versionId: string | null;
  versionNo: number;
  name: string;
  description: string;
  language: string;
  code: string;
  entryType: string;
  signalRequired: boolean;
  contentFingerprint: string;
  createdAt: string;
  updatedAt: string;
}

export interface StrategySource {
  id: string;
  name: string;
  description: string;
  codeHash: string;
  sources: Array<{
    path: string;
    code: string;
  }>;
}

export interface StrategyParameterField {
  type: 'number' | 'select' | 'boolean' | string;
  label: string;
  options?: string[];
  min?: number;
  max?: number;
}

export type StrategyParameterSchema = Record<string, StrategyParameterField>;

export interface StrategyParameterSchemaResponse {
  strategy: string;
  parameters: StrategyParameterSchema;
}

export interface StrategyConfig {
  id: string;
  name: string;
  scriptId: string | null;
  strategyVersionId: string | null;
  scriptVersionNo: number;
  description: string;
  code: string;
  entryType: string;
  signalRequired: boolean;
  strategy: string;
  mode: 'backtest' | 'live' | string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string | null;
  accountId: number | null;
  paramsJson: Record<string, unknown>;
  enabled: boolean;
  published: boolean;
  publishedVersionId: string | null;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

export interface StrategyConfigVersion {
  id: string;
  configId: string;
  strategyVersionId: string | null;
  versionNo: number;
  name: string;
  scriptId: string | null;
  strategy: string;
  mode: 'backtest' | 'live' | string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string | null;
  accountId: number | null;
  paramsJson: Record<string, unknown>;
  changeNote: string;
  createdAt: string;
}

export interface BacktestRunHistory {
  id: string;
  displayNo: number;
  configId: string | null;
  configVersionId: string | null;
  scriptId: string | null;
  accountId: number | null;
  symbol: string;
  timeframe: string;
  fillModel: string;
  scriptSnapshot: Record<string, unknown> | null;
  paramsSnapshot: Record<string, unknown>;
  dataVersion: Record<string, unknown> | null;
  benchmarkSymbol: string | null;
  strategy: string;
  strategyCategory?: 'core_position_overlay' | 'directional_holding';
  startDate: string;
  endDate: string;
  paramsJson: Record<string, unknown>;
  status: string;
  summaryJson: Partial<BacktestSummary> & { errorDetail?: BacktestErrorDetail; error?: string } & Record<string, unknown>;
  outputDir: string;
  note: string;
  tags: string[];
  queuedAt: string;
  startedAt: string;
  cancelRequested: boolean;
  resultJson: BacktestRunResponse | null;
  createdAt: string;
  finishedAt: string;
}

export interface DataGapDate {
  date: string;
  barCount: number;
  expected: number;
  missing: number;
}

export interface KlinePreviewRow {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface DataStatus {
  symbol: string;
  timeframe: string;
  path: string;
  exists: boolean;
  firstDate: string | null;
  lastDate: string | null;
  barCount: number;
  fileSize: number;
  mtime: string | null;
  duplicateCount: number;
  nonTradingCount: number;
  gapDates: DataGapDate[];
  preview: KlinePreviewRow[];
  error?: string;
}
