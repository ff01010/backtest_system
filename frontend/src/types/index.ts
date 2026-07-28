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

export interface InstrumentItem {
  symbol: string;
  name?: string | null;
  market?: string | null;
  enabled?: boolean;
  note?: string | null;
  hasKline: boolean;
  hasSignal: boolean;
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
  maxDrawdown: NullableMetric;
  maxDrawdownRate: NullableMetric;
  annualVolatility: NullableMetric;
  winRate: NullableMetric;
  dailyWinRate: NullableMetric;
  tradeCount: number;
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
  priceSource?: string;
  action?: string;
  status?: 'filled' | 'skipped' | string;
  direction: string;
  side: string;
  anchor: number;
  price: number;
  quantity: number;
  pnl: number;
  note: string;
  strategyModule?: string;
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
}

export interface BacktestRunResponse {
  runId: number | null;
  configId: number | null;
  configVersionId: number | null;
  scriptId: number | null;
  outputDir: string;
  summary: BacktestSummary;
  equity: EquityPoint[];
  dailyHoldings: DailyHoldingPoint[];
  benchmarkSymbol: string | null;
  benchmarkEquity: BenchmarkPoint[];
  benchmarkReturn: NullableMetric;
  excessReturn: NullableMetric;
  trades: BacktestTrade[];
  skippedSignals?: BacktestTrade[];
  experimentManifest?: ExperimentManifest;
  warnings: string[];
}

export interface ExperimentManifest {
  experimentHash: string;
  engineVersion: string;
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
}

export interface StrategyScript {
  id: number;
  name: string;
  description: string;
  language: string;
  code: string;
  entryType: string;
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
  id: number;
  name: string;
  scriptId: number | null;
  strategy: string;
  mode: 'backtest' | 'live' | string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string | null;
  accountId: number | null;
  paramsJson: Record<string, unknown>;
  enabled: boolean;
  published: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface StrategyConfigVersion {
  id: number;
  configId: number;
  versionNo: number;
  name: string;
  scriptId: number | null;
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
  id: number;
  configId: number | null;
  configVersionId: number | null;
  scriptId: number | null;
  accountId: number | null;
  symbol: string;
  timeframe: string;
  fillModel: string;
  scriptSnapshot: Record<string, unknown> | null;
  paramsSnapshot: Record<string, unknown>;
  dataVersion: Record<string, unknown> | null;
  benchmarkSymbol: string | null;
  strategy: string;
  startDate: string;
  endDate: string;
  paramsJson: Record<string, unknown>;
  status: string;
  summaryJson: Partial<BacktestSummary> & Record<string, unknown>;
  outputDir: string;
  note: string;
  tags: string[];
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

export interface DataSyncResult {
  status: string;
  symbol: string;
  timeframe: string;
  from: string;
  to: string;
  new_rows: number;
  new_dates: string[];
  message?: string;
}

export interface DataSyncRun {
  id: number;
  symbol: string;
  timeframe: string;
  sourceTimeframe: string;
  startDate: string;
  endDate: string;
  mode: string;
  status: string;
  newRows: number;
  newDates: string[];
  outputPath: string;
  elapsedMs: number;
  message: string;
  createdAt: string;
}

export interface DataSyncTask {
  id: string;
  status: 'queued' | 'running' | 'ok' | 'partial_error' | 'error' | string;
  request: Record<string, unknown>;
  results: DataSyncResult[];
  error: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}
