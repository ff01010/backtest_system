import {
  BookOpen,
  ChevronDown,
  Code2,
  Database,
  FileSliders,
  History,
  Play,
  Plus,
  RotateCcw,
  Save,
  Share2,
  Star,
  Trash2,
} from 'lucide-react';
import type { FormEvent, ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  createAccount,
  cancelBacktestRun,
  deleteBacktestRun,
  deleteBacktestRuns,
  deleteStrategyConfig,
  fetchAccounts,
  fetchBacktestRunPage,
  fetchBacktestRun,
  fetchBacktestTimeframes,
  fetchBacktestPreflight,
  fetchDataStatus,
  fetchResearchSignalDatasets,
  fetchStrategyParameterSchema,
  fetchStrategySource,
  fetchStrategyConfigs,
  fetchStrategyConfigVersions,
  BacktestRequestError,
  runBacktest,
  saveCompleteStrategy,
  publishBacktestRun,
  setBacktestRunFavorite,
  updateBacktestRunMeta,
} from '../api/client';
import type {
  BacktestRunPayload,
  BacktestTimeframeAvailability,
  ResearchSignalDataset,
} from '../api/client';
import type {
  BacktestAccount,
  BacktestErrorDetail,
  BacktestRunHistory,
  BacktestRunResponse,
  BacktestSummary,
  BacktestTrade,
  BenchmarkPoint,
  DataStatus,
  EquityPoint,
  InstrumentItem,
  StrategyConfig,
  StrategyParameterSchema,
  StrategyConfigVersion,
  StrategySource,
} from '../types';
import { typography } from '../styles/typography';
import { backtestErrorPresentation } from '../utils/backtestErrorPresentation';
import BacktestTabs from './BacktestTabs';
import InstrumentSearchField from './InstrumentSearchField';
import StrategyGuideDrawer from './StrategyGuideDrawer';

import BacktestDateRangePicker from './BacktestDateRangePicker';
interface Props {
  symbol: string;
  strategy: string;
  strategies: string[];
  instruments: InstrumentItem[];
  isAdmin: boolean;
  onWorkspaceSafetyStateChange?: (state: {
    hasUnsavedStrategy: boolean;
    isStrategySavePending: boolean;
  }) => void;
  onStrategySaveActionChange?: (action: () => Promise<boolean>) => void;
  onViewTradeChart: (selection: {
    runId: string;
    symbol: string;
    timeframe: string;
    date: string;
  }) => void;
  onViewSharedBacktest?: (publicationId: string) => void;
  requestedConfigId?: string | null;
}

const panelStyle = {
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 4,
} as const;

const labelStyle = {
  color: 'var(--text-muted)',
  fontSize: typography.base,
  marginBottom: 6,
} as const;

const fieldStyle = {
  width: '100%',
  height: 34,
  background: 'var(--surface-muted)',
  color: 'var(--text)',
  border: '1px solid var(--border-strong)',
  borderRadius: 4,
  padding: '0 10px',
  fontSize: typography.base,
} as const;

export function StrategyStudioErrorNotice({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div className="strategy-editor-alert" role="alert" aria-label="策略操作错误">
      <strong>操作未完成</strong>
      <span>{message}</span>
    </div>
  );
}

const OVERLAY_HOLDING_GRID_COLUMNS = 'minmax(110px, 1fr) repeat(3, minmax(88px, 0.8fr)) repeat(5, minmax(116px, 1.15fr))';
const STANDARD_HOLDING_GRID_COLUMNS = 'minmax(110px, 1fr) repeat(3, minmax(88px, 0.8fr)) repeat(4, minmax(116px, 1.15fr))';

const DEFAULT_PARAMS = {
  startDate: '2026-06-01',
  endDate: '2026-06-26',
  initialCapital: '10000000',
  commissionRate: '0.00008',
  slippage: '0.0001',
  allowLong: true,
  allowShort: true,
  strategyCategory: 'directional_holding',
  holdingPolicy: 'overnight',
  overlaySettlementPolicy: 'intraday_restore',
  fillModel: 'bar_next_open',
  executionAlgorithm: 'immediate',
  twapDurationSeconds: '300',
  twapIntervalSeconds: '30',
  twapMinChildQuantity: '100',
  baseQuantity: '0',
  baseCostPrice: '0',
  initialPositionEnabled: false,
  t0Enabled: false,
  minMarketCap: '10000000000',
  maxMarketCap: '80000000000',
  minRevenueYoy: '0.10',
  minNetProfitYoy: '0.40',
  maxCandidates: '25',
  maxPositions: '25',
  singleWeight: '',
  maxTotalPct: '0.95',
  rebalanceMonths: '5,8,11',
  minHoldingDays: '40',
  stopLoss: '-0.20',
  indexTrendFilter: true,
};

const L2_TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '45m', '60m', '1d'];

const EXPERIMENT_PARAM_KEYS = new Set([
  'startDate',
  'endDate',
  'initialCapital',
  'commissionRate',
  'slippage',
  'executionAlgorithm',
  'twapDurationSeconds',
  'twapIntervalSeconds',
  'twapMinChildQuantity',
]);

const EXECUTION_PARAM_KEYS = new Set([
  'executionAlgorithm',
  'twapDurationSeconds',
  'twapIntervalSeconds',
  'twapMinChildQuantity',
]);

const STRATEGY_LABELS: Record<string, string> = {
  trend_follow: '趋势跟踪',
  grid_mean_reversion: '网格均值回归',
  trend_grid: '趋势网格组合',
  trend_grid_both: '趋势网格组合',
  user_script: '自定义脚本',
  midcap_earnings: '中盘盈利增长',
};

function formatStrategyName(strategyId: string): string {
  return STRATEGY_LABELS[strategyId] || strategyId;
}

function getPnlColor(value: number): string {
  if (value > 0) return '#dc2626';
  if (value < 0) return '#16a34a';
  return '#64748b';
}

function getOrderCount(summary: Partial<BacktestSummary> | null | undefined): number | null {
  if (!summary) return null;
  return typeof summary.orderCount === 'number' ? summary.orderCount : summary.tradeCount ?? null;
}

function getFillCount(summary: Partial<BacktestSummary> | null | undefined): number | null {
  if (!summary) return null;
  return typeof summary.fillCount === 'number' ? summary.fillCount : summary.tradeCount ?? null;
}

type BacktestParamValue = string | boolean;
type BacktestParams = typeof DEFAULT_PARAMS & Record<string, BacktestParamValue>;
type WorkbenchPanel = 'overview' | 'params' | 'code' | 'trades' | 'positions' | 'logs' | 'history';
export type ConfigSectionKey = 'scope' | 'strategy' | 'inputs' | 'execution' | 'capital';
type ChartRange = '1m' | '1y' | 'all';
type TradeSideFilter = 'all' | 'BUY' | 'SELL';
type TradePnlFilter = 'all' | 'win' | 'loss';
type TradeStatusFilter = 'all' | 'filled' | 'skipped';
type HoldingStatusFilter = 'all' | 'long' | 'short' | 'flat';
type TradeColumnKey = 'time' | 'executionTime' | 'status' | 'direction' | 'action' | 'source' | 'note' | 'price' | 'pnl';
type HistoryParamKey =
  | 'configId'
  | 'configVersionId'
  | 'scriptId'
  | 'accountId'
  | 'symbol'
  | 'benchmarkSymbol'
  | 'strategy'
  | 'timeframe'
  | 'startDate'
  | 'endDate'
  | 'initialCapital'
  | 'commissionRate'
  | 'slippage'
  | 'allowLong'
  | 'allowShort'
  | 'strategyCategory'
  | 'holdingPolicy'
  | 'overlaySettlementPolicy'
  | 'fillModel'
  | 'executionAlgorithm'
  | 'baseQuantity'
  | 'baseCostPrice'
  | 'initialPositionEnabled'
  | 't0Enabled';

export function getPublicationEligibilityError(run: BacktestRunHistory): string {
  if (run.status !== 'ok' && run.status !== 'succeeded') return '只有成功完成的回测可以共享';
  if (!run.scriptSnapshot || typeof run.scriptSnapshot.code !== 'string' || !run.scriptSnapshot.code.trim()) {
    return '历史任务缺少策略代码快照，请重新运行后共享';
  }
  if (!run.paramsSnapshot || Object.keys(run.paramsSnapshot).length === 0 || !run.resultJson) {
    return '历史任务资料不完整，请重新运行后共享';
  }
  return '';
}

const HISTORY_PARAM_LABELS: Record<HistoryParamKey, string> = {
  configId: '策略方案 ID',
  configVersionId: '方案版本',
  scriptId: '脚本ID',
  accountId: '账号ID',
  symbol: '标的',
  benchmarkSymbol: '对比标的',
  strategy: '策略',
  timeframe: '运行周期',
  startDate: '开始日期',
  endDate: '结束日期',
  initialCapital: '初始资金',
  commissionRate: '手续费率',
  slippage: '滑点',
  allowLong: '允许做多',
  allowShort: '允许做空',
  strategyCategory: '策略类别',
  holdingPolicy: '持仓规则',
  overlaySettlementPolicy: '增强仓结算规则',
  fillModel: '成交模式',
  executionAlgorithm: '执行方式',
  baseQuantity: '底仓数量',
  baseCostPrice: '底仓成本价',
  initialPositionEnabled: '启用初始底仓',
  t0Enabled: '启用底仓做T',
};

interface NetSeriesPoint {
  date: string;
  value: number;
  netValue: number;
  order: number;
  x?: number;
  y?: number;
}

interface NetSeries {
  name: string;
  color: string;
  points: NetSeriesPoint[];
  dashed?: boolean;
}

interface ConfigSectionCardProps {
  title: string;
  summary: string;
  defaultOpen?: boolean;
  sectionRef: (element: HTMLDetailsElement | null) => void;
  children: ReactNode;
}

function ConfigSectionCard({
  title,
  summary,
  defaultOpen = false,
  sectionRef,
  children,
}: ConfigSectionCardProps): ReactNode {
  return (
    <details
      ref={(element) => {
        if (element && element.dataset.initialized !== 'true') {
          element.open = defaultOpen;
          element.dataset.initialized = 'true';
        }
        sectionRef(element);
      }}
      className="strategy-config-section"
    >
      <summary className="strategy-config-section-heading">
        <span>
          <strong>{title}</strong>
          <small>{summary}</small>
        </span>
        <ChevronDown className="strategy-config-section-chevron" size={15} aria-hidden="true" />
      </summary>
      <div className="strategy-config-section-body">{children}</div>
    </details>
  );
}

function getDefaultScriptCode(timeframe: string): string {
  return `# on_bar(bar, state) 会在每根已完成的 ${timeframe} K 线调用一次。
# 当前规则：本 Bar 收盘产生信号，并按下一根可交易 Bar 开盘价加滑点虚拟成交。
# 时间：time 为 YYYYMMDDHHMMSS；tradeDate 为 YYYY-MM-DD；hhmm 例如 931。
# bar 可读：time/datetime/tradeDate/hhmm/open/high/low/close/volume/cash/equity/position/params。
# action 支持 buy_open、sell_open、sell_close、buy_close。
def on_bar(bar, state):
  if bar['position'] == 0 and bar['close'] > bar['open']:
    return [{'action': 'buy_open', 'quantity': 100, 'reason': '阳线开多'}]
  if bar['position'] > 0 and bar['close'] < bar['open']:
    return [{'action': 'sell_close', 'quantity': bar['position'], 'reason': '阴线平多'}]
  return []
`;
}

function createEmptyScriptForm(timeframe = '5m') {
  return {
    name: '',
    description: '',
    code: getDefaultScriptCode(timeframe),
    entryType: 'on_bar',
    signalRequired: false,
  };
}

function isValidDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
}

function normalizeFillModel(value: unknown): 'bar_next_open' | 'l2_quote' {
  return value === 'l2_quote' ? 'l2_quote' : 'bar_next_open';
}

function normalizeOverlaySettlementPolicy(value: unknown): 'intraday_restore' | 'cross_day_carry' {
  return value === 'cross_day_carry' ? 'cross_day_carry' : 'intraday_restore';
}

function calendarMonthRange(month: string): { startDate: string; endDate: string } {
  const start = new Date(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 1, 1);
  const end = new Date(start.getFullYear(), start.getMonth() + 1, 0);
  const format = (value: Date): string => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
  return { startDate: format(start), endDate: format(end) };
}


export function normalizeApiDate(value: string) {
  return /^\d{8}$/.test(value) ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}` : value;
}

function getStrategyCategoryLabel(value: unknown): string {
  return value === 'core_position_overlay' ? '底仓增强策略' : '方向性持仓策略';
}

export function shouldFetchBacktestTimeframes(startDate: string, endDate: string): boolean {
  return isValidDate(startDate) && isValidDate(endDate);
}

export function buildTimeframeAvailabilityCacheKey(
  symbols: string[],
  startDate: string,
  endDate: string
): string {
  return `${startDate}:${endDate}:${[...symbols].sort().join(',')}`;
}

export function isCustomScriptStrategy(strategyId: string): boolean {
  return strategyId === 'user_script';
}

export function hasSavedStrategyId(strategyId: string): boolean {
  return strategyId.length > 0;
}

export function hasLegacyTimeSlice(code: string): boolean {
  return /bar\s*\[\s*['"]time['"]\s*\]\s*\[/.test(code);
}

export function hasEnabledTradeDirection(allowLong: boolean, allowShort: boolean): boolean {
  return allowLong || allowShort;
}

export function getResearchSignalSelectionError(
  signalRequired: boolean,
  signalDatasetId: string
): string | null {
  if (signalRequired && !signalDatasetId) {
    return '当前策略必须选择研究信号后才能运行';
  }
  if (!signalRequired && signalDatasetId) {
    return '当前策略未声明为研究信号驱动策略，不能附加研究信号';
  }
  return null;
}

const RESEARCH_TIMEFRAME_MINUTES: Record<string, number> = {
  '1m': 1,
  '3m': 3,
  '5m': 5,
  '15m': 15,
  '30m': 30,
  '45m': 45,
  '60m': 60,
  '1d': 24 * 60,
};

export function getResearchSignalTimeframeError(
  signalTimeframe: string | null | undefined,
  backtestTimeframe: string
): string | null {
  if (!signalTimeframe) return null;
  const signalMinutes = RESEARCH_TIMEFRAME_MINUTES[signalTimeframe];
  const backtestMinutes = RESEARCH_TIMEFRAME_MINUTES[backtestTimeframe];
  if (signalMinutes == null) return `研究信号周期 ${signalTimeframe} 暂不受支持`;
  if (backtestMinutes == null) return `回测周期 ${backtestTimeframe} 暂不支持研究信号对齐`;
  if (signalMinutes < backtestMinutes) {
    return `研究信号周期 ${signalTimeframe} 细于回测周期 ${backtestTimeframe}，不能无损对齐`;
  }
  return null;
}

export function getConfigSectionForError(message: string): ConfigSectionKey {
  if (/数据|日期|周期|标的/.test(message)) return 'scope';
  if (/研究信号/.test(message)) return 'inputs';
  if (/TWAP|执行时长|子单|成交模式|执行方式/.test(message)) return 'execution';
  if (/初始资金|手续费|滑点|费用/.test(message)) return 'capital';
  return 'strategy';
}

const TRADE_ACTION_LABELS: Record<string, string> = {
  BUY_OPEN: '买入开多',
  SELL_OPEN: '卖出开空',
  SELL_CLOSE: '卖出平多',
  BUY_CLOSE: '买入平空',
  SELL_COVER: '卖出平多',
  BUY_COVER: '买入平空',
  STOP_SELL: '止损平多',
  STOP_BUY: '止损平空',
  FORCE_SELL: '尾盘强平多头',
  FORCE_BUY: '尾盘强平空头',
};

const TRADE_NOTE_LABELS: Record<string, string> = {
  lower_peak_open_short: '低尖点确认，卖出开空',
  stop: '触及止损价，买入平空',
  take_profit_1: '达到第一档止盈，买入平空',
  take_profit_2: '达到第二档止盈，买入平空',
  morning_force: '午间强平，买入平空',
  afternoon_force: '尾盘分批强平，买入平空',
  final_force: '尾盘最终强平，买入平空',
  CLOSE_LINE: '触及平仓线',
};

export function formatTradeDirection(side: string): string {
  return side === 'BUY' ? '买入' : '卖出';
}

export function formatTradeNote(direction: string, note: string, fundingLimited = false): string {
  const normalizedNote = note.trim();
  const actionLabel = TRADE_ACTION_LABELS[direction];
  const baseNote = TRADE_NOTE_LABELS[normalizedNote]
    || (!normalizedNote
      ? actionLabel || '策略信号'
      : /[开平]|止损|止盈|强平/.test(normalizedNote)
        ? normalizedNote
        : actionLabel
          ? actionLabel + '：' + normalizedNote
          : normalizedNote);
  return fundingLimited ? baseNote + '；资金不足限制，已成交可负担部分' : baseNote;
}

const CLOSED_TRADE_ACTION_PATTERN = /(CLOSE|COVER|STOP|FORCE)/;
const OVERLAY_CLOSE_EFFECTS = new Set(['partial_close', 'close', 'reverse']);

export function getClosedLoopTradePnl(trade: BacktestTrade, isOverlayResult: boolean): number | null {
  if (trade.status && trade.status !== 'filled') return null;
  if (isOverlayResult) {
    if (!OVERLAY_CLOSE_EFFECTS.has(trade.cycleEffect || '')) return null;
    return typeof trade.realizedOverlayPnl === 'number' && Number.isFinite(trade.realizedOverlayPnl)
      ? trade.realizedOverlayPnl
      : null;
  }
  const action = String(trade.action || trade.direction).toUpperCase();
  if (!CLOSED_TRADE_ACTION_PATTERN.test(action) && trade.pnl === 0) return null;
  return Number.isFinite(trade.pnl) ? trade.pnl : null;
}

function getOverlayCycleEffectLabel(value: string): string {
  const labels: Record<string, string> = {
    open: '开启周期',
    increase: '增加增强仓',
    partial_close: '部分恢复',
    close: '完成周期',
    reverse: '反向成交',
  };
  return labels[value] || value || '--';
}

function getOverlayCycleTradeDisplay(trade: BacktestTrade, cycleId: string) {
  const segment = trade.attributionSegments?.find((item) => item.cycleId === cycleId);
  const effect = String(segment?.cycleEffect || trade.cycleEffect || '');
  const quantity = Number(segment?.quantity ?? trade.quantity);
  const realizedPnl = Number(segment?.realizedOverlayPnl ?? trade.realizedOverlayPnl);
  return {
    effectLabel: getOverlayCycleEffectLabel(effect),
    quantity: Number.isFinite(quantity) ? quantity : trade.quantity,
    realizedPnl: ['partial_close', 'close', 'reverse'].includes(effect) && Number.isFinite(realizedPnl)
      ? realizedPnl
      : null,
  };
}

function formatSignalTime(value: string): string {
  if (!value) return '-';
  const compact = value.replace(/\D/g, '');
  if (compact.length >= 12) {
    return `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)} ${compact.slice(8, 10)}:${compact.slice(10, 12)}`;
  }
  if (compact.length === 8) {
    return `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)}`;
  }
  return value;
}

function formatElapsedTime(startedAt: string, now: number): string {
  const started = Date.parse(startedAt);
  if (!Number.isFinite(started)) return '';
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}小时${minutes % 60}分`;
  if (minutes > 0) return `${minutes}分${seconds % 60}秒`;
  return `${seconds}秒`;
}

function getTradeSource(direction: string): '策略信号' | '系统结算' {
  return direction.startsWith('FORCE_') ? '系统结算' : '策略信号';
}

function validateParams(params: BacktestParams) {
  const errors: string[] = [];
  const initialCapital = Number(params.initialCapital);
  const commissionRate = Number(params.commissionRate);
  const slippage = Number(params.slippage);

  if (!isValidDate(params.startDate) || !isValidDate(params.endDate)) {
    errors.push('日期格式应为 YYYY-MM-DD');
  } else if (params.startDate > params.endDate) {
    errors.push('开始日期不能晚于结束日期');
  }
  if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
    errors.push('初始资金必须大于 0');
  }
  const isCorePositionOverlay = params.strategyCategory === 'core_position_overlay';
  const initialPositionEnabled = isCorePositionOverlay || params.initialPositionEnabled;
  const baseQuantity = Number(params.baseQuantity);
  const baseCostPrice = Number(params.baseCostPrice);
  if (initialPositionEnabled) {
    if (!Number.isInteger(baseQuantity) || baseQuantity <= 0) {
      errors.push('启用初始底仓时底仓数量必须是正整数');
    }
    if (!Number.isFinite(baseCostPrice) || baseCostPrice <= 0) {
      errors.push('启用初始底仓时必须填写有效的底仓成本价');
    }
  } else if (baseQuantity !== 0 || baseCostPrice !== 0) {
    errors.push('未启用初始底仓时底仓数量和成本价必须为 0');
  }
  if (isCorePositionOverlay && !params.t0Enabled) {
    errors.push('底仓增强策略必须启用底仓做 T');
  }
  if (!isCorePositionOverlay && params.t0Enabled) {
    errors.push('方向性持仓策略不能启用底仓做 T');
  }
  if (!Number.isFinite(commissionRate) || commissionRate < 0) {
    errors.push('手续费率不能小于 0');
  }
  if (!Number.isFinite(slippage) || slippage < 0 || slippage >= 1) {
    errors.push('滑点应大于等于 0 且小于 1');
  }
  if (params.executionAlgorithm === 'twap') {
    const durationSeconds = Number(params.twapDurationSeconds);
    const intervalSeconds = Number(params.twapIntervalSeconds);
    const minChildQuantity = Number(params.twapMinChildQuantity);
    if (params.fillModel !== 'l2_quote') {
      errors.push('TWAP 仅支持 L2 盘口回放');
    }
    if (!Number.isInteger(durationSeconds) || durationSeconds < 3) {
      errors.push('TWAP 执行时长必须是不少于 3 秒的整数');
    }
    if (!Number.isInteger(intervalSeconds) || intervalSeconds < 3 || intervalSeconds % 3 !== 0 || intervalSeconds > durationSeconds) {
      errors.push('TWAP 间隔必须是 3 秒倍数，且不大于执行时长');
    }
    if (!Number.isInteger(minChildQuantity) || minChildQuantity <= 0) {
      errors.push('TWAP 最小子单数量必须为正整数');
    }
  }
  if (!hasEnabledTradeDirection(params.allowLong, params.allowShort)) {
    errors.push('至少选择一个交易方向');
  }
  return errors;
}

function buildNetValueChart(
  strategyPoints: EquityPoint[],
  benchmarkPoints: BenchmarkPoint[],
  options: {
    showStrategy: boolean;
    showBenchmark: boolean;
    showExcess: boolean;
    useLogAxis: boolean;
  }
) {
  const width = 960;
  const height = 260;
  const padding = { top: 24, right: 30, bottom: 34, left: 54 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const actualDates = Array.from(new Set([
    ...strategyPoints.map((point) => point.date),
    ...benchmarkPoints.map((point) => point.date),
  ])).sort();
  const dates = actualDates.length > 0 ? ['__START__', ...actualDates] : [];
  const strategySeries = buildNetSeries('策略', strategyPoints, dates, '#0f766e');
  const benchmarkSeries = buildNetSeries('买入持有基准', benchmarkPoints, dates, '#f59e0b');
  const excessSeries = buildExcessNetSeries(strategySeries, benchmarkSeries, dates);
  const visibleSeries = [
    options.showStrategy ? strategySeries : null,
    options.showBenchmark ? benchmarkSeries : null,
    options.showExcess ? excessSeries : null,
  ].filter((series): series is NetSeries => Boolean(series && series.points.length > 0));
  const valueForScale = (value: number) => (
    options.useLogAxis ? Math.log(Math.max(value, 0.000001)) : value
  );
  const allValues = visibleSeries.flatMap((series) => series.points.map((point) => valueForScale(point.netValue)));
  const minRaw = allValues.length > 0 ? Math.min(...allValues) : valueForScale(0.98);
  const maxRaw = allValues.length > 0 ? Math.max(...allValues) : valueForScale(1.02);
  const paddingValue = Math.max((maxRaw - minRaw) * 0.12, options.useLogAxis ? 0.004 : 0.01);
  const minValue = minRaw - paddingValue;
  const maxValue = maxRaw + paddingValue;
  const range = Math.max(maxValue - minValue, options.useLogAxis ? 0.004 : 0.01);
  const xFor = (index: number) => (
    dates.length === 1
      ? padding.left + chartWidth / 2
      : padding.left + (index / (dates.length - 1)) * chartWidth
  );
  const yFor = (value: number) => (
    padding.top + (1 - (valueForScale(value) - minValue) / range) * chartHeight
  );
  const chartSeries = visibleSeries.map((item) => ({
    ...item,
    points: item.points.map((point) => ({
      ...point,
      x: xFor(dates.indexOf(point.date)),
      y: yFor(point.netValue),
    })),
  }));
  return { width, height, padding, chartHeight, dates, actualDates, minValue, maxValue, chartSeries };
}

function buildNetSeries(
  name: string,
  points: Array<EquityPoint | BenchmarkPoint>,
  dates: string[],
  color: string
) : NetSeries {
  if (points.length === 0) return { name, color, points: [] };
  const firstValue = points[0].value || 1;
  const dateOrder = new Map(dates.map((date, index) => [date, index]));
  const seriesPoints = [
    {
      date: '__START__',
      value: firstValue,
      netValue: 1,
      order: 0,
    },
    ...points.map((point) => ({
      date: point.date,
      value: point.value,
      netValue: point.value / firstValue,
      order: dateOrder.get(point.date) ?? 0,
    })),
  ].sort((a, b) => a.order - b.order);
  return { name, color, points: seriesPoints };
}

function buildExcessNetSeries(strategySeries: NetSeries, benchmarkSeries: NetSeries, dates: string[]) {
  const dateOrder = new Map(dates.map((date, index) => [date, index]));
  const benchmarkByDate = new Map(benchmarkSeries.points.map((point) => [point.date, point.netValue]));
  const points = strategySeries.points
    .map((point) => {
      const benchmarkNetValue = benchmarkByDate.get(point.date);
      if (benchmarkNetValue == null) return null;
      return {
        date: point.date,
        value: point.value,
        netValue: 1 + point.netValue - benchmarkNetValue,
        order: dateOrder.get(point.date) ?? 0,
      };
    })
    .filter((point): point is NetSeriesPoint => point != null);
  return { name: '超额净值', color: '#2563eb', points, dashed: true };
}

function toPolyline(points: Array<{ x: number; y: number }>) {
  return points.map((point) => `${point.x},${point.y}`).join(' ');
}

function getLastPoint<T>(points: T[]) {
  return points.length > 0 ? points[points.length - 1] : null;
}

function filterEquityByRange(points: EquityPoint[], range: ChartRange) {
  if (range === 'all' || points.length === 0) return points;
  const keep = range === '1m' ? 22 : 242;
  return points.slice(-keep);
}

function filterBenchmarkByDates(points: BenchmarkPoint[], dates: Set<string>) {
  return points.filter((point) => dates.has(point.date));
}

function buildDailyBars(points: EquityPoint[], benchmarkPoints: BenchmarkPoint[]) {
  const benchmarkByDate = new Map(benchmarkPoints.map((point) => [point.date, point.returnRate]));
  return points.map((point, index) => {
    const previousBenchmark = index > 0 ? benchmarkByDate.get(points[index - 1].date) || 0 : 0;
    const benchmarkReturn = benchmarkByDate.get(point.date) || 0;
    return {
      date: point.date,
      pnl: point.pnl,
      excess: point.pnl - (benchmarkReturn - previousBenchmark) * (points[index - 1]?.value || point.value),
    };
  });
}

function downloadCsv(filename: string, rows: Array<Record<string, string | number>>) {
  if (rows.length === 0) return;
  const headers = Object.keys(rows[0]);
  const escapeCell = (value: string | number) => `"${String(value).replace(/"/g, '""')}"`;
  const csv = [
    headers.join(','),
    ...rows.map((row) => headers.map((header) => escapeCell(row[header])).join(',')),
  ].join('\n');
  const blob = new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

const HISTORY_VALUE_LABELS: Record<string, string> = {
  completed: '已完成',
  succeeded: '已完成',
  ok: '已完成',
  running: '运行中',
  queued: '排队中',
  error: '运行失败',
  failed: '运行失败',
  cancelled: '已取消',
  cancelling: '正在取消',
  pending: '等待运行',
  overnight: '允许跨日持仓',
  intraday: '日内持仓',
  intraday_restore: '日内恢复目标底仓',
  cross_day_carry: '增强仓允许跨日',
  bar_next_open: '下一根K线开盘成交',
  l2_quote: 'L2盘口回放成交',
  immediate: '立即成交',
  twap: '时间加权拆单（TWAP）',
  core_position_overlay: '底仓增强策略',
  directional_holding: '方向性持仓策略',
  on_bar_script: '策略脚本逐K线产生信号',
  bar_close_signal: 'K线收盘产生信号',
  bar_close_signal_fill: '收盘信号成交',
  hit: '已命中缓存',
  miss: '未命中缓存',
  miss_unavailable: '缓存未命中，源数据不可用',
  complete: '完整',
  unknown: '未知',
};

const HISTORY_EXTRA_PARAM_LABELS: Record<string, string> = {
  maxCandidates: '最大候选数量',
  maxPositions: '最大持仓数量',
  singleWeight: '单标的权重',
  maxTotalPct: '最大总仓位',
  rebalanceMonths: '调仓月份',
  minHoldingDays: '最短持有天数',
  stopLoss: '止损比例',
  indexTrendFilter: '启用指数趋势过滤',
  twapDurationSeconds: '拆单执行时长（秒）',
  twapIntervalSeconds: '拆单间隔（秒）',
  twapMinChildQuantity: '最小子单数量',
};

function formatHistoryParamValue(value: unknown) {
  if (value == null || value === '') return '--';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(6);
  }
  if (typeof value === 'object') return JSON.stringify(value);
  return HISTORY_VALUE_LABELS[String(value)] || String(value);
}

function asHistoryRecord(value: unknown): Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function getHistoryRunParam(run: BacktestRunHistory, key: string): unknown {
  const snapshot = asHistoryRecord(run.paramsSnapshot);
  const requestStrategyParams = asHistoryRecord(run.paramsJson.strategyParams);
  return snapshot[key] ?? requestStrategyParams[key] ?? run.paramsJson[key];
}

function formatRunReference(displayNo: number | null | undefined): string {
  return `#${String(displayNo ?? 0).padStart(2, '0')}`;
}

interface ResultContextBarProps {
  runReference: string;
  configName: string;
  symbol: string;
  benchmarkSymbol: string | null;
  timeframe: string;
  startDate: string;
  endDate: string;
  commissionRate: number | null;
  slippage: number | null;
  holdingPolicy: string | undefined;
  overlaySettlementPolicy: string | undefined;
  strategyCategory: unknown;
}

function ResultContextBar({
  runReference,
  configName,
  symbol,
  benchmarkSymbol,
  timeframe,
  startDate,
  endDate,
  commissionRate,
  slippage,
  holdingPolicy,
  overlaySettlementPolicy,
  strategyCategory,
}: ResultContextBarProps): ReactNode {
  return (
    <section style={{ ...panelStyle, padding: '10px 12px', background: '#ffffff' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', color: '#475569', fontSize: 12, lineHeight: 1.6 }}>
        <strong style={{ color: '#0f766e' }}>{runReference}</strong>
        <span>{configName}</span>
        <span>{symbol} · {timeframe}</span>
        <span>{startDate} - {endDate}</span>
      </div>
      <details style={{ marginTop: 8, color: '#475569', fontSize: 12 }}>
        <summary style={{ color: '#0f766e', cursor: 'pointer', width: 'fit-content' }}>查看本次回测设置</summary>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', marginTop: 8, lineHeight: 1.6 }}>
          <span>基准：{benchmarkSymbol || symbol}</span>
          <span>手续费：{commissionRate == null ? '--' : `${(commissionRate * 10_000).toFixed(2)} bp`}</span>
          <span>滑点：{slippage == null ? '--' : `${(slippage * 10_000).toFixed(2)} bp`}</span>
          <span>策略类别：{getStrategyCategoryLabel(strategyCategory)}</span>
          <span>持仓规则：{strategyCategory === 'core_position_overlay'
            ? overlaySettlementPolicy === 'cross_day_carry' ? '增强仓允许跨日' : '日内恢复目标底仓'
            : holdingPolicy === 'overnight' ? '允许跨日' : '未记录'}
          </span>
        </div>
      </details>
    </section>
  );
}

function getHistoryDirectionLabel(run: BacktestRunHistory): string {
  const allowLong = getHistoryRunParam(run, 'allowLong') !== false;
  const allowShort = getHistoryRunParam(run, 'allowShort') !== false;
  if (allowLong && allowShort) return '允许做多、做空';
  if (allowLong) return '仅允许做多';
  if (allowShort) return '仅允许做空';
  return '未启用交易方向';
}

function formatHistoryBps(value: unknown): string {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? `${(numberValue * 10_000).toFixed(2)} bp` : '--';
}

function formatHistoryCurrency(value: unknown): string {
  const numberValue = Number(value);
  return Number.isFinite(numberValue)
    ? `¥${numberValue.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
    : '--';
}

function getHistoryParamLabel(key: string): string {
  return HISTORY_PARAM_LABELS[key as HistoryParamKey] || HISTORY_EXTRA_PARAM_LABELS[key] || key;
}

function getHistoryParamDisplayValue(key: string, value: unknown): string {
  if (key === 'commissionRate' || key === 'slippage') return formatHistoryBps(value);
  if (key === 'initialCapital' || key === 'baseCostPrice') return formatHistoryCurrency(value);
  return formatHistoryParamValue(value);
}

function getHistoryResearchSignal(run: BacktestRunHistory): Record<string, unknown> {
  const manifest = run.resultJson?.experimentManifest;
  const strategyParams = asHistoryRecord(run.paramsSnapshot);
  const manifestSignal = asHistoryRecord(asHistoryRecord(manifest).researchSignal);
  return Object.keys(manifestSignal).length > 0 ? manifestSignal : asHistoryRecord(strategyParams.researchSignal);
}

function HistoryKeySettings({ run, configName }: { run: BacktestRunHistory; configName: string }): ReactNode {
  const manifest = run.resultJson?.experimentManifest;
  const researchSignal = getHistoryResearchSignal(run);
  const strategyCategory = run.strategyCategory || getHistoryRunParam(run, 'strategyCategory');
  const holdingRule = strategyCategory === 'core_position_overlay'
    ? getHistoryRunParam(run, 'overlaySettlementPolicy')
    : getHistoryRunParam(run, 'holdingPolicy');
  const signalVersion = researchSignal.datasetVersion == null
    ? '未使用研究信号'
    : `研究信号版本 ${formatHistoryParamValue(researchSignal.datasetVersion)}`;
  const entries = [
    ['策略方案', configName],
    ['策略类别', getStrategyCategoryLabel(strategyCategory)],
    ['交易方向', getHistoryDirectionLabel(run)],
    ['持仓规则', formatHistoryParamValue(holdingRule)],
    ['成交方式', formatHistoryParamValue(run.fillModel || manifest?.fillModel)],
    ['执行方式', formatHistoryParamValue(getHistoryRunParam(run, 'executionAlgorithm') || 'immediate')],
    ['初始资金', formatHistoryCurrency(getHistoryRunParam(run, 'initialCapital'))],
    ['交易成本', `手续费 ${formatHistoryBps(getHistoryRunParam(run, 'commissionRate'))}，滑点 ${formatHistoryBps(getHistoryRunParam(run, 'slippage'))}`],
    ['研究信号', signalVersion],
  ];

  return (
    <section className="history-key-settings">
      <div className="history-section-title">关键设置</div>
      <div className="history-key-grid">
        {entries.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function HistoryFullParameters({ run }: { run: BacktestRunHistory }): ReactNode {
  const strategyParams = asHistoryRecord(run.paramsSnapshot);
  const snapshotEntries = Object.entries(strategyParams)
    .filter(([key]) => !EXPERIMENT_PARAM_KEYS.has(key) && key !== 'researchSignal')
    .map(([key, value]) => ({ key, label: getHistoryParamLabel(key), value }));

  return (
    <details className="history-disclosure">
      <summary>查看完整参数（{snapshotEntries.length}项）</summary>
      {snapshotEntries.length > 0 ? (
        <div className="history-parameter-grid">
          {snapshotEntries.map((entry) => (
            <div key={entry.key}>
              <span>{entry.label}</span>
              <strong>{getHistoryParamDisplayValue(entry.key, entry.value)}</strong>
            </div>
          ))}
        </div>
      ) : (
        <div className="history-disclosure-empty">该旧记录未保存策略参数快照。</div>
      )}
    </details>
  );
}

function HistoryTechnicalDetails({ run }: { run: BacktestRunHistory }): ReactNode {
  const manifest = run.resultJson?.experimentManifest;
  const scriptSnapshot = asHistoryRecord(run.scriptSnapshot);
  const dataVersion = asHistoryRecord(run.dataVersion);
  const barSnapshot = asHistoryRecord(dataVersion.barSnapshot);
  const researchSignal = getHistoryResearchSignal(run);
  const dataSnapshot = asHistoryRecord(run.resultJson?.dataSnapshot);
  const temporaryAggregation = asHistoryRecord(dataSnapshot.temporaryBarAggregation);
  const temporaryDays = Array.isArray(temporaryAggregation.days) ? temporaryAggregation.days : [];
  const cacheSummary = temporaryDays.map((item) => {
    const day = asHistoryRecord(item);
    return `${String(day.tradeDate ?? '--')}：${formatHistoryParamValue(day.cacheStatus)}`;
  }).join('；');
  const qualitySummary = temporaryDays.map((item) => {
    const day = asHistoryRecord(item);
    return `${String(day.tradeDate ?? '--')}：${formatHistoryParamValue(day.quality)}`;
  }).join('；');
  const sourceLabel = barSnapshot.derivation ? '由完整1分钟数据派生' : barSnapshot.sourcePeriod ? 'QMT原始数据' : null;
  const entries = [
    ['任务唯一编号', run.id],
    ['方案版本编号', run.configVersionId],
    ['脚本编号', run.scriptId],
    ['实验指纹', manifest?.experimentHash],
    ['代码指纹', scriptSnapshot.codeHash ?? manifest?.strategyCodeHash],
    ['数据指纹', dataVersion.dataHash ?? manifest?.dataHash],
    ['研究信号数据集编号', researchSignal.datasetId],
    ['研究信号快照指纹', researchSignal.snapshotHash],
    ['信号生成规则', manifest?.signalModel],
    ['K线数据范围', manifest?.barRange ? `${manifest.barRange.start} - ${manifest.barRange.end}` : null],
    ['K线数量', manifest?.barCount],
    ['数据来源', sourceLabel],
    ['数据文件', Array.isArray(dataVersion.files) ? `${dataVersion.files.length}个` : null],
    ['L2聚合版本', temporaryAggregation.aggregationVersion],
    ['L2映射版本', temporaryAggregation.mappingVersion],
    ['L2缓存状态', cacheSummary || null],
    ['L2数据质量', qualitySummary || null],
  ].filter((entry) => entry[1] != null && entry[1] !== '');

  return (
    <details className="history-disclosure history-technical-disclosure">
      <summary>复现与技术信息</summary>
      {entries.length > 0 ? (
        <div className="history-technical-list">
          {entries.map(([label, value]) => (
            <div className="history-technical-row" key={String(label)}>
              <span>{String(label)}</span>
              <code>{formatHistoryParamValue(value)}</code>
              <button type="button" onClick={() => void navigator.clipboard?.writeText(String(value))}>复制</button>
            </div>
          ))}
        </div>
      ) : (
        <div className="history-disclosure-empty">该记录没有可用的技术快照。</div>
      )}
    </details>
  );
}

function getRunErrorDetail(run: BacktestRunHistory): BacktestErrorDetail | null {
  const detail = run.summaryJson.errorDetail;
  if (detail) return detail;
  if (run.status === 'cancelled') {
    return {
      category: 'system_error', code: 'task.cancelled', message: '回测任务已取消。',
      hint: '可在确认参数后重新运行。', retryable: false, taskId: run.id,
    };
  }
  if (run.status === 'error') {
    return {
      category: 'system_error', code: 'system.legacy_error', message: '该历史回测未保存可定位错误信息。',
      hint: '请重新运行；如持续失败，请提供任务编号给维护人员。', retryable: true, taskId: run.id,
    };
  }
  return null;
}

function BacktestErrorCard({ detail }: { detail: BacktestErrorDetail }): ReactNode {
  const presentation = backtestErrorPresentation(detail);
  const isStrategy = presentation.isUserActionable;
  return (
    <div style={{ border: `1px solid ${isStrategy ? '#fdba74' : '#fecaca'}`, background: isStrategy ? '#fff7ed' : '#fef2f2', borderRadius: 6, padding: 12, color: '#7c2d12', fontSize: 13, lineHeight: 1.6 }}>
      <div style={{ fontWeight: 700, color: isStrategy ? '#9a3412' : '#b91c1c' }}>
        {presentation.title}
      </div>
      {presentation.location && <div style={{ color: '#9a3412', marginTop: 3 }}>{presentation.location}</div>}
      <div style={{ marginTop: 5 }}>原因：{detail.message}</div>
      {detail.hint && <div style={{ marginTop: 3 }}>建议：{detail.hint}</div>}
      {detail.taskId && <div style={{ marginTop: 6, color: '#64748b', fontSize: 12 }}>任务编号：<code>{detail.taskId}</code></div>}
    </div>
  );
}

function getHistoryParamPairs(run: BacktestRunHistory) {
  const params = run.paramsJson;
  const pairs: Array<[HistoryParamKey, unknown]> = [
    ['configId', run.configId],
    ['configVersionId', run.configVersionId],
    ['scriptId', run.scriptId],
    ['accountId', run.accountId],
    ['symbol', run.symbol],
    ['benchmarkSymbol', run.benchmarkSymbol],
    ['strategy', run.strategy],
    ['timeframe', run.timeframe],
    ['startDate', run.startDate],
    ['endDate', run.endDate],
    ['initialCapital', getHistoryRunParam(run, 'initialCapital')],
    ['commissionRate', getHistoryRunParam(run, 'commissionRate')],
    ['slippage', getHistoryRunParam(run, 'slippage')],
    ['allowLong', getHistoryRunParam(run, 'allowLong')],
    ['allowShort', getHistoryRunParam(run, 'allowShort')],
    ['strategyCategory', getHistoryRunParam(run, 'strategyCategory')],
    ['holdingPolicy', getHistoryRunParam(run, 'holdingPolicy')],
    ['overlaySettlementPolicy', getHistoryRunParam(run, 'overlaySettlementPolicy')],
    ['fillModel', run.fillModel],
    ['executionAlgorithm', params.executionAlgorithm],
    ['baseQuantity', getHistoryRunParam(run, 'baseQuantity')],
    ['baseCostPrice', getHistoryRunParam(run, 'baseCostPrice')],
    ['initialPositionEnabled', getHistoryRunParam(run, 'initialPositionEnabled')],
    ['t0Enabled', getHistoryRunParam(run, 't0Enabled')],
  ];
  return pairs;
}

function compareHistoryRuns(left: BacktestRunHistory | null, right: BacktestRunHistory | null) {
  if (!left || !right) return [];
  const hiddenKeys = new Set<HistoryParamKey>(['configId', 'configVersionId', 'scriptId', 'accountId']);
  const leftPairs = new Map(getHistoryParamPairs(left));
  const rightPairs = new Map(getHistoryParamPairs(right));
  return (Object.keys(HISTORY_PARAM_LABELS) as HistoryParamKey[])
    .filter((key) => !hiddenKeys.has(key))
    .map((key) => {
      const leftValue = leftPairs.get(key);
      const rightValue = rightPairs.get(key);
      return {
        key,
        label: HISTORY_PARAM_LABELS[key],
        left: formatHistoryParamValue(leftValue),
        right: formatHistoryParamValue(rightValue),
        changed: formatHistoryParamValue(leftValue) !== formatHistoryParamValue(rightValue),
      };
    })
    .filter((item) => item.changed);
}

function sortFingerprintValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortFingerprintValue);
  if (value != null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortFingerprintValue(item)])
    );
  }
  return value;
}

export function buildStrategyDraftFingerprint(input: {
  strategy: string;
  mode: string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string;
  accountId: number | '';
  params: Record<string, unknown>;
  script: { code: string; entryType: string; signalRequired: boolean };
}): string {
  return JSON.stringify(sortFingerprintValue({
    strategy: input.strategy,
    mode: input.mode,
    symbol: input.symbol,
    timeframe: input.timeframe,
    benchmarkSymbol: input.benchmarkSymbol,
    accountId: input.accountId || null,
    params: input.params,
    script: input.script,
  }));
}

export function buildSavedStrategyFingerprint(config: StrategyConfig): string {
  const savedParams = config.paramsJson.params as Record<string, unknown> | undefined;
  return buildStrategyDraftFingerprint({
    strategy: config.strategy,
    mode: config.mode,
    symbol: config.symbol,
    timeframe: config.timeframe,
    benchmarkSymbol: config.benchmarkSymbol || config.symbol,
    accountId: config.accountId || '',
    params: Object.fromEntries(
      Object.entries(savedParams || {}).filter(([key]) => key !== 'warmupTradingDays')
    ),
    script: {
      code: config.code,
      entryType: config.entryType,
      signalRequired: config.signalRequired,
    },
  });
}

export default function BacktestPage({
  symbol,
  strategy,
  strategies,
  instruments,
  isAdmin,
  onWorkspaceSafetyStateChange,
  onStrategySaveActionChange,
  onViewTradeChart,
  onViewSharedBacktest,
  requestedConfigId = null,
}: Props) {
  const [instrumentList, setInstrumentList] = useState<InstrumentItem[]>(instruments);
  const [accounts, setAccounts] = useState<BacktestAccount[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState(symbol);
  const [selectedTimeframe, setSelectedTimeframe] = useState('5m');
  const [backtestTimeframes, setBacktestTimeframes] = useState<BacktestTimeframeAvailability[]>([]);
  const [selectedBenchmarkSymbol, setSelectedBenchmarkSymbol] = useState(symbol);
  const [preflight, setPreflight] = useState<Awaited<ReturnType<typeof fetchBacktestPreflight>> | null>(null);
  const [preflightNotice, setPreflightNotice] = useState('');
  const [selectedStrategy, setSelectedStrategy] = useState('user_script');
  const [selectedAccountId, setSelectedAccountId] = useState<number | ''>('');
  const [newAccountName, setNewAccountName] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [backtestErrorDetail, setBacktestErrorDetail] = useState<BacktestErrorDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [cancellingRun, setCancellingRun] = useState(false);
  const [result, setResult] = useState<BacktestRunResponse | null>(null);
  const [pendingRun, setPendingRun] = useState<{ runId: string; displayNo: number } | null>(null);
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null);
  const [benchmarkDataStatus, setBenchmarkDataStatus] = useState<DataStatus | null>(null);
  const [dataStatusError, setDataStatusError] = useState('');
  const [benchmarkDataStatusError, setBenchmarkDataStatusError] = useState('');
  const [loadingDataStatus, setLoadingDataStatus] = useState(false);
  const [loadingBenchmarkDataStatus, setLoadingBenchmarkDataStatus] = useState(false);
  const [params, setParams] = useState<BacktestParams>(DEFAULT_PARAMS);
  const configSectionRefs = useRef<Partial<Record<ConfigSectionKey, HTMLDetailsElement | null>>>({});
  const [calendarMonth, setCalendarMonth] = useState(DEFAULT_PARAMS.startDate.slice(0, 7));
  const [calendarPreflight, setCalendarPreflight] = useState<Awaited<ReturnType<typeof fetchBacktestPreflight>> | null>(null);
  const [strategyParameterSchema, setStrategyParameterSchema] = useState<StrategyParameterSchema | null>(null);
  const [strategySchemaError, setStrategySchemaError] = useState('');
  const [loadingStrategySchema, setLoadingStrategySchema] = useState(false);
  const [strategyConfigs, setStrategyConfigs] = useState<StrategyConfig[]>([]);
  const [researchSignalDatasets, setResearchSignalDatasets] = useState<ResearchSignalDataset[]>([]);
  const [researchSignalLoadError, setResearchSignalLoadError] = useState('');
  const [loadingResearchSignals, setLoadingResearchSignals] = useState(false);
  const [selectedSignalDatasetId, setSelectedSignalDatasetId] = useState('');
  const [strategySource, setStrategySource] = useState<StrategySource | null>(null);
  const [strategySourceError, setStrategySourceError] = useState('');
  const [configVersions, setConfigVersions] = useState<StrategyConfigVersion[]>([]);
  const [backtestRuns, setBacktestRuns] = useState<BacktestRunHistory[]>([]);
  const [historyRunDetails, setHistoryRunDetails] = useState<Record<string, BacktestRunHistory>>({});
  const [selectedHistoryRunIds, setSelectedHistoryRunIds] = useState<Set<string>>(new Set());
  const [selectedHistoryRunId, setSelectedHistoryRunId] = useState<string | ''>('');
  const [compareHistoryRunId, setCompareHistoryRunId] = useState<string | ''>('');
  const [historyQuery, setHistoryQuery] = useState('');
  const [historySymbolFilter, setHistorySymbolFilter] = useState('');
  const [historyFavoritesOnly, setHistoryFavoritesOnly] = useState(false);
  const [favoritingHistoryRunIds, setFavoritingHistoryRunIds] = useState<Set<string>>(new Set());
  const [historyCompareQuery, setHistoryCompareQuery] = useState('');
  const [historyCompareOptions, setHistoryCompareOptions] = useState<BacktestRunHistory[]>([]);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState(10);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historySymbols, setHistorySymbols] = useState<string[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyLoadError, setHistoryLoadError] = useState('');
  const historyRequestIdRef = useRef(0);
  const historyCompareRequestIdRef = useRef(0);
  const [historyNoteDraft, setHistoryNoteDraft] = useState('');
  const [historyTagsDraft, setHistoryTagsDraft] = useState('');
  const [publicationRun, setPublicationRun] = useState<BacktestRunHistory | null>(null);
  const [publicationTitle, setPublicationTitle] = useState('');
  const [publicationDescription, setPublicationDescription] = useState('');
  const [publicationTags, setPublicationTags] = useState('');
  const [publicationError, setPublicationError] = useState('');
  const [publishingRun, setPublishingRun] = useState(false);
  const [selectedScriptId, setSelectedScriptId] = useState<string>('');
  const [selectedConfigId, setSelectedConfigId] = useState<string>('');
  const lastRequestedConfigIdRef = useRef<string | null>(null);
  const [versionDirty, setVersionDirty] = useState(false);
  const [strategySavePending, setStrategySavePending] = useState(false);
  const strategySaveActionRef = useRef<() => Promise<string | null>>(async () => null);
  const [publishedFingerprint, setPublishedFingerprint] = useState('');
  const [configName, setConfigName] = useState('');
  const [scriptForm, setScriptForm] = useState(createEmptyScriptForm());
  const [strategyGuideOpen, setStrategyGuideOpen] = useState(false);
  const hasSelectedConfig = hasSavedStrategyId(selectedConfigId);

  useEffect(() => {
    onWorkspaceSafetyStateChange?.({
      hasUnsavedStrategy: versionDirty,
      isStrategySavePending: strategySavePending,
    });
  }, [onWorkspaceSafetyStateChange, strategySavePending, versionDirty]);

  useEffect(() => {
    onStrategySaveActionChange?.(async () => (await strategySaveActionRef.current()) != null);
  }, [onStrategySaveActionChange]);

  useEffect(() => {
    if (!versionDirty) return undefined;
    const preventUnsavedReload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', preventUnsavedReload);
    return () => window.removeEventListener('beforeunload', preventUnsavedReload);
  }, [versionDirty]);

  const [activePanel, setActivePanel] = useState<WorkbenchPanel>('overview');
  const [chartRange, setChartRange] = useState<ChartRange>('all');
  const [showStrategyCurve, setShowStrategyCurve] = useState(true);
  const [showBenchmarkCurve, setShowBenchmarkCurve] = useState(true);
  const [showExcessCurve, setShowExcessCurve] = useState(false);
  const [useLogAxis, setUseLogAxis] = useState(false);
  const [tradeSideFilter, setTradeSideFilter] = useState<TradeSideFilter>('all');
  const [tradePnlFilter, setTradePnlFilter] = useState<TradePnlFilter>('all');
  const [tradeStatusFilter, setTradeStatusFilter] = useState<TradeStatusFilter>('all');
  const [tradePage, setTradePage] = useState(1);
  const [tradePageSize, setTradePageSize] = useState(50);
  const [groupTradesByDay, setGroupTradesByDay] = useState(false);
  const [holdingStatusFilter, setHoldingStatusFilter] = useState<HoldingStatusFilter>('all');
  const [holdingStartDate, setHoldingStartDate] = useState('');
  const [holdingEndDate, setHoldingEndDate] = useState('');
  const [holdingPage, setHoldingPage] = useState(1);
  const [holdingPageSize, setHoldingPageSize] = useState(20);
  const [tradeColumns, setTradeColumns] = useState<Record<TradeColumnKey, boolean>>({
    time: true,
    executionTime: true,
    status: true,
    direction: true,
    action: true,
    source: true,
    note: true,
    price: true,
    pnl: true,
  });
  const strategyParameterFields = useMemo(
    () => Object.entries(strategyParameterSchema || {}),
    [strategyParameterSchema]
  );
  const selectedResearchSignal = useMemo(
    () => researchSignalDatasets.find((item) => item.id === selectedSignalDatasetId) ?? null,
    [researchSignalDatasets, selectedSignalDatasetId]
  );
  const researchSignalTimeframeError = getResearchSignalTimeframeError(
    selectedResearchSignal?.timeframe,
    selectedTimeframe
  );
  const strategySourceCode = useMemo(() => (
    strategySource?.sources
      .map((source) => `# ${source.path}\n\n${source.code}`)
      .join('\n\n# ----------------------------------------------------------------\n\n') || ''
  ), [strategySource]);
  const activeStrategyCode = selectedStrategy === 'user_script' ? scriptForm.code : strategySourceCode;
  const hasQuoteCallback = /\bdef\s+on_quote\s*\(/.test(activeStrategyCode);
  const draftFingerprint = useMemo(() => buildStrategyDraftFingerprint({
    strategy: selectedStrategy,
    mode: 'backtest',
    symbol: selectedSymbol,
    timeframe: selectedTimeframe,
    benchmarkSymbol: selectedBenchmarkSymbol,
    accountId: selectedAccountId,
    params,
    script: {
      code: scriptForm.code,
      entryType: scriptForm.entryType,
      signalRequired: scriptForm.signalRequired,
    },
  }), [
    params,
    scriptForm.code,
    scriptForm.entryType,
    scriptForm.signalRequired,
    selectedAccountId,
    selectedBenchmarkSymbol,
    selectedStrategy,
    selectedSymbol,
    selectedTimeframe,
  ]);

  useEffect(() => {
    setInstrumentList(instruments);
  }, [instruments]);

  useEffect(() => {
    refreshStrategyAssets();
    const signalDatasetId = new URLSearchParams(window.location.search).get('signalDatasetId');
    if (signalDatasetId) setSelectedSignalDatasetId(signalDatasetId);
    setLoadingResearchSignals(true);
    setResearchSignalLoadError('');
    fetchResearchSignalDatasets()
      .then((items) => {
        setResearchSignalDatasets(items);
        if (signalDatasetId && !items.some((item) => item.id === signalDatasetId)) {
          setResearchSignalLoadError('链接中的研究信号不存在、尚未发布或当前账号无权访问');
        }
      })
      .catch((loadError: unknown) => {
        setResearchSignalDatasets([]);
        setResearchSignalLoadError(
          loadError instanceof Error ? loadError.message : '研究信号列表加载失败'
        );
      })
      .finally(() => setLoadingResearchSignals(false));
  }, []);

  useEffect(() => {
    historyRequestIdRef.current += 1;
    const timer = window.setTimeout(
      () => void refreshBacktestRuns(),
      historyQuery.trim() ? 250 : 0,
    );
    return () => window.clearTimeout(timer);
  }, [historyPage, historyPageSize, historyQuery, historySymbolFilter, historyFavoritesOnly]);

  useEffect(() => {
    if (historyCompareQuery.trim()) return;
    setHistoryCompareOptions(backtestRuns);
  }, [backtestRuns, historyCompareQuery]);

  useEffect(() => {
    const query = historyCompareQuery.trim();
    const requestId = ++historyCompareRequestIdRef.current;
    if (!query) return undefined;
    const timer = window.setTimeout(() => {
      fetchBacktestRunPage({ limit: 20, offset: 0, query, symbol: '' })
        .then((page) => {
          if (requestId === historyCompareRequestIdRef.current) {
            setHistoryCompareOptions(page.runs);
          }
        })
        .catch(() => {
          if (requestId === historyCompareRequestIdRef.current) {
            setHistoryCompareOptions([]);
          }
        });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [historyCompareQuery]);

  useEffect(() => {
    if (!pendingRun) return undefined;
    let disposed = false;
    const poll = async () => {
      try {
        const run = await fetchBacktestRun(pendingRun.runId);
        if (disposed) return;
        setBacktestRuns((prev) => prev.map((item) => (item.id === run.id ? run : item)));
        if (run.status === 'ok' && run.resultJson) {
          setResult({
            ...run.resultJson,
            runId: run.id,
            displayNo: run.displayNo,
            configId: run.configId,
            configVersionId: run.configVersionId,
            scriptId: run.scriptId,
          });
          setHoldingStartDate('');
          setHoldingEndDate('');
          setHoldingStatusFilter('all');
          setPendingRun(null);
          setActivePanel('overview');
          setStatus(`回测 ${formatRunReference(run.displayNo)} 已完成，成交 ${getFillCount(run.resultJson.summary) ?? 0} 笔`);
          refreshBacktestRuns();
          return;
        }
        if (run.status === 'error' || run.status === 'cancelled') {
          setPendingRun(null);
          setBacktestErrorDetail(getRunErrorDetail(run));
          setSelectedHistoryRunId(run.id);
          setActivePanel('history');
          setStatus('');
          setError('');
          refreshBacktestRuns();
        }
      } catch {
        // 网络瞬时失败不应中断正在运行的回测轮询。
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [pendingRun]);

  const backgroundActiveRunIds = backtestRuns
    .filter((run) => (
      run.id !== pendingRun?.runId
      && (run.status === 'queued' || run.status === 'running' || run.status === 'cancelling')
    ))
    .map((run) => run.id)
    .join(',');

  useEffect(() => {
    if (!backgroundActiveRunIds) return undefined;
    let disposed = false;
    const poll = async () => {
      const settled = await Promise.allSettled(
        backgroundActiveRunIds.split(',').map((runId) => fetchBacktestRun(runId)),
      );
      if (disposed) return;
      const updates = new Map(
        settled.flatMap((item) => item.status === 'fulfilled' ? [[item.value.id, item.value] as const] : []),
      );
      if (updates.size > 0) {
        setBacktestRuns((prev) => prev.map((item) => updates.get(item.id) || item));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [backgroundActiveRunIds]);

  useEffect(() => {
    if (!hasSelectedConfig || !publishedFingerprint) return;
    setVersionDirty(draftFingerprint !== publishedFingerprint);
  }, [draftFingerprint, publishedFingerprint, selectedConfigId]);

  useEffect(() => {
    refreshDataStatus(selectedSymbol, selectedTimeframe);
  }, [selectedSymbol, selectedTimeframe]);

  useEffect(() => {
    if (selectedBenchmarkSymbol !== selectedSymbol) return;
    setBenchmarkDataStatus(dataStatus);
    setBenchmarkDataStatusError(dataStatusError);
    setLoadingBenchmarkDataStatus(loadingDataStatus);
  }, [
    selectedBenchmarkSymbol,
    selectedSymbol,
    dataStatus,
    dataStatusError,
    loadingDataStatus,
  ]);

  useEffect(() => {
    if (selectedBenchmarkSymbol === selectedSymbol) return;
    refreshBenchmarkDataStatus(selectedBenchmarkSymbol, selectedTimeframe);
  }, [selectedBenchmarkSymbol, selectedSymbol, selectedTimeframe]);

  useEffect(() => {
    if (!shouldFetchBacktestTimeframes(params.startDate, params.endDate)) {
      setBacktestTimeframes([]);
      return;
    }
    let cancelled = false;
    fetchBacktestTimeframes(selectedSymbol, params.startDate, params.endDate).then((items) => {
      if (!cancelled) {
        setBacktestTimeframes(items);
      }
    }).catch((requestError) => {
      if (!cancelled) setDataStatusError(
        requestError instanceof Error ? requestError.message : '读取可用周期失败'
      );
    });
    return () => { cancelled = true; };
  }, [params.startDate, params.endDate, selectedSymbol]);

  useEffect(() => {
    if (!shouldFetchBacktestTimeframes(params.startDate, params.endDate)) {
      setPreflight(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      fetchBacktestPreflight(selectedSymbol, selectedTimeframe, params.startDate, params.endDate, normalizeFillModel(params.fillModel))
        .then((value) => {
          if (!cancelled) setPreflight(value);
        })
        .catch(() => {
          if (!cancelled) setPreflight(null);
        });
    }, 280);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [selectedSymbol, selectedTimeframe, params.startDate, params.endDate, params.fillModel]);
  useEffect(() => {
    const range = calendarMonthRange(calendarMonth);
    let cancelled = false;
    fetchBacktestPreflight(selectedSymbol, selectedTimeframe, range.startDate, range.endDate, normalizeFillModel(params.fillModel))
      .then((value) => { if (!cancelled) setCalendarPreflight(value); })
      .catch(() => { if (!cancelled) setCalendarPreflight(null); });
    return () => { cancelled = true; };
  }, [calendarMonth, params.fillModel, selectedSymbol, selectedTimeframe]);


  useEffect(() => {
    if (isCustomScriptStrategy(selectedStrategy)) {
      setStrategySource(null);
      setStrategySourceError('');
      return;
    }
    let cancelled = false;
    fetchStrategySource(selectedStrategy)
      .then((source) => {
        if (cancelled) return;
        setStrategySource(source);
        setStrategySourceError('');
      })
      .catch((error) => {
        if (cancelled) return;
        setStrategySource(null);
        setStrategySourceError(error instanceof Error ? error.message : '读取实际策略代码失败');
      });
    return () => { cancelled = true; };
  }, [selectedStrategy]);

  useEffect(() => {
    setTradePage(1);
  }, [tradeSideFilter, tradePnlFilter, tradeStatusFilter, tradePageSize, result]);

  useEffect(() => {
    setHoldingPage(1);
  }, [holdingStartDate, holdingEndDate, holdingStatusFilter, holdingPageSize, result]);

  useEffect(() => {
    if (!selectedConfigId) {
      setConfigVersions([]);
      return;
    }
    let cancelled = false;
    fetchStrategyConfigVersions(selectedConfigId)
      .then((versions) => {
        if (!cancelled) setConfigVersions(versions);
      })
      .catch(() => {
        if (!cancelled) setConfigVersions([]);
      });
    return () => { cancelled = true; };
  }, [selectedConfigId]);

  useEffect(() => {
    if (backtestRuns.length === 0) {
      setSelectedHistoryRunId('');
      setCompareHistoryRunId('');
      return;
    }
    if (!selectedHistoryRunId) {
      setSelectedHistoryRunId(backtestRuns[0].id);
    }
    if (!compareHistoryRunId) {
      setCompareHistoryRunId(backtestRuns[1]?.id || backtestRuns[0].id);
    }
  }, [backtestRuns]);

  useEffect(() => {
    setSelectedHistoryRunIds((previous) => {
      const available = new Set(backtestRuns.map((run) => run.id));
      const next = new Set([...previous].filter((runId) => available.has(runId)));
      return next.size === previous.size ? previous : next;
    });
  }, [backtestRuns]);

  useEffect(() => {
    if (selectedHistoryRunId === '' || compareHistoryRunId === '') return;
    if (selectedHistoryRunId === compareHistoryRunId) {
      const nextCompare = backtestRuns.find((item) => item.id !== selectedHistoryRunId);
      setCompareHistoryRunId(nextCompare?.id || selectedHistoryRunId);
    }
  }, [selectedHistoryRunId, compareHistoryRunId, backtestRuns]);

  useEffect(() => {
    if (!selectedHistoryRunId) {
      setHistoryNoteDraft('');
      setHistoryTagsDraft('');
      return;
    }
    let cancelled = false;
    fetchBacktestRun(selectedHistoryRunId)
      .then((run) => {
        if (cancelled) return;
        setBacktestRuns((prev) => prev.map((item) => (item.id === run.id ? run : item)));
        setHistoryRunDetails((previous) => ({ ...previous, [run.id]: run }));
        setHistoryNoteDraft(run.note || '');
        setHistoryTagsDraft(run.tags.join(', '));
      })
      .catch(() => {
        const selected = backtestRuns.find((item) => item.id === selectedHistoryRunId) || null;
        if (cancelled || selected == null) return;
        setHistoryNoteDraft(selected.note || '');
        setHistoryTagsDraft(selected.tags.join(', '));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedHistoryRunId]);

  useEffect(() => {
    if (!compareHistoryRunId) return undefined;
    let cancelled = false;
    fetchBacktestRun(compareHistoryRunId)
      .then((run) => {
        if (!cancelled) {
          setHistoryRunDetails((previous) => ({ ...previous, [run.id]: run }));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [compareHistoryRunId]);

  const applyAccount = (account: BacktestAccount) => {
    setParams((prev) => ({
      ...prev,
      initialCapital: String(account.initialCapital),
      commissionRate: String(account.commissionRate),
      slippage: String(account.slippage),
      allowShort: account.allowShort,
    }));
  };

  const refreshAccounts = async () => {
    const data = await fetchAccounts();
    setAccounts(data);
    const picked = data.find((item) => item.isDefault) || data[0];
    if (picked) {
      setSelectedAccountId(picked.id);
      applyAccount(picked);
    }
  };

  const refreshStrategyAssets = async () => {
    const configs = await fetchStrategyConfigs();
    setStrategyConfigs(configs);
  };

  const refreshBacktestRuns = async (targetPage = historyPage) => {
    const requestId = ++historyRequestIdRef.current;
    setHistoryLoading(true);
    setHistoryLoadError('');
    try {
      const page = await fetchBacktestRunPage({
        limit: historyPageSize,
        offset: (targetPage - 1) * historyPageSize,
        query: historyQuery,
        symbol: historySymbolFilter,
        ...(historyFavoritesOnly ? { favoritesOnly: true } : {}),
      });
      if (requestId !== historyRequestIdRef.current) return;
      const lastPage = Math.max(1, Math.ceil(page.total / historyPageSize));
      setHistoryTotal(page.total);
      setHistorySymbols(page.symbols);
      if (targetPage > lastPage) {
        setHistoryPage(lastPage);
        return;
      }
      setBacktestRuns(page.runs);
    } catch (loadError) {
      if (requestId === historyRequestIdRef.current) {
        setHistoryLoadError(loadError instanceof Error ? loadError.message : '加载历史回测失败');
      }
    } finally {
      if (requestId === historyRequestIdRef.current) setHistoryLoading(false);
    }
  };

  const refreshDataStatus = async (targetSymbol: string, timeframe: string) => {
    if (!targetSymbol) return;
    setLoadingDataStatus(true);
    setDataStatusError('');
    try {
      const statusData = await fetchDataStatus(targetSymbol, timeframe);
      setDataStatus(statusData);
    } catch (e) {
      setDataStatus(null);
      setDataStatusError(e instanceof Error ? e.message : '读取数据状态失败');
    } finally {
      setLoadingDataStatus(false);
    }
  };

  const refreshStrategyParameterSchema = async (targetStrategy: string) => {
    if (!targetStrategy) {
      setStrategyParameterSchema(null);
      setStrategySchemaError('');
      return;
    }
    setLoadingStrategySchema(true);
    setStrategySchemaError('');
    try {
      const schema = await fetchStrategyParameterSchema(targetStrategy);
      setStrategyParameterSchema(schema.parameters);
    } catch (e) {
      setStrategyParameterSchema(null);
      setStrategySchemaError(e instanceof Error ? e.message : '读取策略参数模板失败');
    } finally {
      setLoadingStrategySchema(false);
    }
  };
  useEffect(() => {
    refreshStrategyParameterSchema(selectedStrategy);
  }, [selectedStrategy]);


  const applyStrategyConfig = (config: StrategyConfig) => {
    const savedParams = config.paramsJson.params as Partial<BacktestParams> | undefined;
    setSelectedConfigId(config.id);
    setConfigVersions([]);
    setPublishedFingerprint('');
    setConfigName(config.name);
    setSelectedSymbol(config.symbol);
    setSelectedTimeframe(config.timeframe || '5m');
    setSelectedBenchmarkSymbol(config.benchmarkSymbol || config.symbol);
    setSelectedStrategy(config.strategy);
    setSelectedScriptId(config.scriptId || '');
    setSelectedAccountId(config.accountId || '');
    if (savedParams) {
      setParams((prev) => {
        const next: BacktestParams = { ...prev };
        Object.entries(savedParams).forEach(([key, value]) => {
          if (key !== 'warmupTradingDays' && value !== undefined) next[key] = value;
        });
        return next;
      });
    }
    setVersionDirty(false);
    setScriptForm({
      name: config.name,
      description: config.description,
      code: config.code,
      entryType: config.entryType,
      signalRequired: config.signalRequired,
    });
    setSelectedSignalDatasetId(
      config.signalRequired && typeof config.paramsJson.signalDatasetId === 'string'
        ? config.paramsJson.signalDatasetId
        : ''
    );
    setPublishedFingerprint(buildSavedStrategyFingerprint(config));
    setStatus(`已加载策略 ${config.name}`);
    setError('');
  };

  useEffect(() => {
    if (!requestedConfigId || lastRequestedConfigIdRef.current === requestedConfigId) return;
    let cancelled = false;
    setLoadingResearchSignals(true);
    setResearchSignalLoadError('');
    fetchResearchSignalDatasets()
      .then((items) => {
        if (!cancelled) setResearchSignalDatasets(items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setResearchSignalLoadError(
            reason instanceof Error ? reason.message : '加载研究信号列表失败',
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingResearchSignals(false);
      });
    fetchStrategyConfigs()
      .then((configs) => {
        if (cancelled) return;
        setStrategyConfigs(configs);
        const requested = configs.find((item) => item.id === requestedConfigId);
        if (!requested) {
          setError('复制的策略当前不可用');
          return;
        }
        lastRequestedConfigIdRef.current = requestedConfigId;
        applyStrategyConfig(requested);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : '加载复制策略失败');
        }
      });
    return () => { cancelled = true; };
  }, [requestedConfigId]);

  const buildStrategyConfigPayload = (
    scriptId: string,
    name: string,
    strategyId = selectedStrategy,
  ) => ({
    name,
    scriptId,
    strategy: strategyId,
    mode: 'backtest',
    symbol: selectedSymbol,
    timeframe: selectedTimeframe,
    benchmarkSymbol: selectedBenchmarkSymbol || selectedSymbol,
    accountId: typeof selectedAccountId === 'number' ? selectedAccountId : null,
    paramsJson: {
      params: Object.fromEntries(Object.entries(params).filter(([key]) => key !== 'warmupTradingDays')),
      scriptName: scriptForm.name.trim(),
    },
    enabled: true,
  });

  const setParam = (key: string, value: BacktestParamValue) => {
    setParams((prev) => ({ ...prev, [key]: value }));
  };

  const revealConfigSection = (section: ConfigSectionKey) => {
    const element = configSectionRefs.current[section];
    if (!element) return;
    element.open = true;
    window.requestAnimationFrame(() => {
      element.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  };

  const setStrategyCategory = (strategyCategory: 'core_position_overlay' | 'directional_holding') => {
    setParams((prev) => strategyCategory === 'core_position_overlay'
      ? {
        ...prev,
        strategyCategory,
        holdingPolicy: 'intraday',
        overlaySettlementPolicy: normalizeOverlaySettlementPolicy(prev.overlaySettlementPolicy),
        initialPositionEnabled: true,
        t0Enabled: true,
      }
      : {
        ...prev,
        strategyCategory,
        holdingPolicy: 'overnight',
        baseQuantity: '0',
        baseCostPrice: '0',
        initialPositionEnabled: false,
        t0Enabled: false,
      });
    if (strategyCategory === 'core_position_overlay' && !L2_TIMEFRAMES.includes(selectedTimeframe)) {
      setSelectedTimeframe('1m');
    }
  };

  const setInitialPositionEnabled = (enabled: boolean) => {
    setParams((prev) => enabled
      ? { ...prev, initialPositionEnabled: true }
      : { ...prev, initialPositionEnabled: false, baseQuantity: '0', baseCostPrice: '0' });
  };

  const updateScriptForm = (changes: Partial<typeof scriptForm>) => {
    setScriptForm((prev) => ({ ...prev, ...changes }));
  };

  const handleUseStrategyGuideExample = (code: string) => {
    if (!window.confirm('写入示例会覆盖当前未保存的策略代码，是否继续？')) return;
    updateScriptForm({ code });
    setStatus('已将指南示例写入编辑器，请检查并保存后再运行回测');
    setError('');
  };

  const setVersionTimeframe = (timeframe: string) => {
    setSelectedTimeframe(timeframe);
  };

  const setFillModel = (fillModel: 'bar_next_open' | 'l2_quote') => {
    setParam('fillModel', fillModel);
    if (fillModel !== 'l2_quote') {
      setParam('executionAlgorithm', 'immediate');
    }
    if (fillModel === 'l2_quote' && !L2_TIMEFRAMES.includes(selectedTimeframe)) {
      setSelectedTimeframe('1m');
    }
  };

  const setExecutionAlgorithm = (algorithm: 'immediate' | 'twap') => {
    setParam('executionAlgorithm', algorithm);
    if (algorithm === 'twap') {
      setFillModel('l2_quote');
    }
  };

  const refreshBenchmarkDataStatus = async (targetSymbol: string, timeframe: string) => {
    if (!targetSymbol) return;
    setLoadingBenchmarkDataStatus(true);
    setBenchmarkDataStatusError('');
    try {
      const statusData = await fetchDataStatus(targetSymbol, timeframe);
      setBenchmarkDataStatus(statusData);
    } catch (e) {
      setBenchmarkDataStatus(null);
      setBenchmarkDataStatusError(e instanceof Error ? e.message : '读取基准数据状态失败');
    } finally {
      setLoadingBenchmarkDataStatus(false);
    }
  };

  const renderStrategyDirectionControls = () => (
    <section style={{ borderTop: '1px solid #e2e8f0', paddingTop: 10 }}>
      <div style={{ ...labelStyle, marginBottom: 7 }}>策略交易方向</div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 7, minHeight: 30, color: '#334155', fontSize: 13 }}>
          <input
            type="checkbox"
            checked={params.allowLong}
            disabled={!params.allowShort}
            onChange={(e) => setParam('allowLong', e.target.checked)}
          />
          允许做多
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 7, minHeight: 30, color: '#334155', fontSize: 13 }}>
          <input
            type="checkbox"
            checked={params.allowShort}
            disabled={!params.allowLong}
            onChange={(e) => setParam('allowShort', e.target.checked)}
          />
          允许做空
        </label>
      </div>
      <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5, marginTop: 6 }}>
        保存策略后随方案版本固定；回测页面仅使用已保存配置。
      </div>
    </section>
  );

  const renderStrategyParameterControl = ([key, field]: typeof strategyParameterFields[number]) => {
    const rawValue = params[key];
    if (field.type === 'boolean') {
      return (
        <label
          key={key}
          title={key}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            minHeight: 34,
            color: '#64748b',
            fontSize: 13,
          }}
        >
          <input
            type="checkbox"
            checked={Boolean(rawValue)}
            onChange={(e) => setParam(key, e.target.checked)}
          />
          {field.label}
        </label>
      );
    }
    if (field.type === 'select') {
      return (
        <label key={key} title={key}>
          <div style={labelStyle}>{field.label}</div>
          <select
            value={typeof rawValue === 'string' ? rawValue : ''}
            onChange={(e) => setParam(key, e.target.value)}
            style={fieldStyle}
          >
            {(field.options || []).map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      );
    }
    return (
      <label key={key} title={key}>
        <div style={labelStyle}>{field.label}</div>
        <input
          type={field.type === 'number' ? 'number' : 'text'}
          min={field.min}
          max={field.max}
          value={typeof rawValue === 'string' ? rawValue : ''}
          onChange={(e) => setParam(key, e.target.value)}
          style={fieldStyle}
        />
      </label>
    );
  };

  const renderStrategyParameterForm = () => {
    if (loadingStrategySchema) {
      return (
        <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.5 }}>
          正在读取策略参数模板...
        </div>
      );
    }
    if (strategySchemaError) {
      return (
        <div style={{ color: '#b91c1c', fontSize: 12, lineHeight: 1.5 }}>
          {strategySchemaError}
        </div>
      );
    }
    if (strategyParameterFields.length === 0) {
      return null;
    }
    return (
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {strategyParameterFields.map(renderStrategyParameterControl)}
      </div>
    );
  };

  const handleAccountSelect = (value: string) => {
    const accountId = Number(value);
    setSelectedAccountId(accountId);
    const account = accounts.find((item) => item.id === accountId);
    if (account) applyAccount(account);
  };

  const handleConfigSelect = (value: string) => {
    if (!value) {
      setSelectedConfigId('');
      setConfigName('');
      return;
    }
    const config = strategyConfigs.find((item) => item.id === value);
    if (config) applyStrategyConfig(config);
  };

  const handleNewStrategy = () => {
    setSelectedConfigId('');
    setSelectedScriptId('');
    setConfigName('');
    setConfigVersions([]);
    setPublishedFingerprint('');
    setVersionDirty(false);
    setSelectedStrategy('user_script');
    setScriptForm(createEmptyScriptForm(selectedTimeframe));
    setStatus('已新建策略草稿，请编辑后保存');
    setError('');
  };

  const handleCopyStrategy = () => {
    if (!hasSelectedConfig) return;
    const sourceName = configName.trim() || scriptForm.name.trim() || '未命名策略';
    const existingNames = new Set(strategyConfigs.map((item) => item.name));
    let copyNumber = 1;
    let copiedName = `${sourceName} 副本`;
    while (existingNames.has(copiedName)) {
      copyNumber += 1;
      copiedName = `${sourceName} 副本 ${copyNumber}`;
    }
    setSelectedConfigId('');
    setSelectedScriptId('');
    setConfigVersions([]);
    setConfigName(copiedName);
    setScriptForm((previous) => ({ ...previous, name: copiedName }));
    setPublishedFingerprint('');
    setVersionDirty(true);
    setStatus('已复制为新策略草稿；保存后会创建独立脚本和独立版本链');
    setError('');
  };

  const showPreflightNotice = (message: string): void => {
    setPreflightNotice(message);
    window.setTimeout(() => setPreflightNotice(''), 4000);
  };

  const handleSaveConfig = async (): Promise<string | null> => {

    setError('');
    setStatus('');
    const strategyName = configName.trim() || scriptForm.name.trim();
    if (!strategyName) {
      setError('策略名称不能为空');
      return null;
    }
    const validationErrors = validateParams(params);
    if (validationErrors.length > 0) {
      setError(validationErrors.join('；'));
      return null;
    }
    if (selectedStrategy === 'user_script' && scriptForm.entryType !== 'on_bar') {
      setError('自定义脚本策略必须选择或新建一个 on_bar 策略代码');
      return null;
    }
    const selectedPreflight = preflight?.timeframes.find((item) => item.timeframe === selectedTimeframe);
    if (!selectedPreflight?.available) {
      showPreflightNotice(selectedPreflight?.reason || '正在检查当前策略的数据可用性');
      return null;
    }
    setStrategySavePending(true);
    try {
      const isCustomScript = isCustomScriptStrategy(selectedStrategy);
      const sourceCode = isCustomScript ? scriptForm.code : (strategySourceCode || scriptForm.code);
      const scriptPayload = {
        name: strategyName,
        description: isCustomScript
          ? scriptForm.description.trim()
          : (strategySource?.description || scriptForm.description.trim()),
        code: sourceCode,
        entryType: isCustomScript
          ? 'on_bar'
          : (strategySource ? `catalog:${strategySource.id}` : scriptForm.entryType),
        signalRequired: isCustomScript ? scriptForm.signalRequired : false,
      };
      const strategyId = isCustomScript ? 'user_script' : selectedStrategy;
      const configPayload = {
        ...buildStrategyConfigPayload(
          '',
          strategyName,
          strategyId,
        ),
        publish: false,
      };
      const currentConfig = strategyConfigs.find((item) => item.id === selectedConfigId);
      const targetConfigId = hasSelectedConfig ? selectedConfigId : null;
      const { script: savedScript, config } = await saveCompleteStrategy(
        scriptPayload,
        configPayload,
        hasSelectedConfig ? selectedScriptId : null,
        targetConfigId,
        currentConfig?.revision ?? null,
      );
      await refreshStrategyAssets();
      setSelectedConfigId(config.id);
      setSelectedScriptId(savedScript.id);
      setSelectedStrategy(strategyId);
      setConfigName(config.name);
      setScriptForm({
        name: savedScript.name,
        description: savedScript.description,
        code: savedScript.code,
        entryType: savedScript.entryType,
        signalRequired: savedScript.signalRequired,
      });
      setPublishedFingerprint(buildSavedStrategyFingerprint(config));
      setVersionDirty(false);
      setStatus(`已保存策略 ${config.name}`);
      return config.id;
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存策略失败');
      return null;
    } finally {
      setStrategySavePending(false);
    }
  };

  strategySaveActionRef.current = handleSaveConfig;

  const handleDeleteConfig = async () => {
    if (!hasSelectedConfig) return;
    setError('');
    setStatus('');
    try {
      await deleteStrategyConfig(selectedConfigId);
      await refreshStrategyAssets();
      setSelectedConfigId('');
      setSelectedScriptId('');
      setConfigVersions([]);
      setConfigName('');
      setScriptForm(createEmptyScriptForm(selectedTimeframe));
      setStatus('已删除策略');
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除策略失败');
    }
  };

  const handleCreateAccount = async () => {
    setError('');
    setStatus('');
    const validationErrors = validateParams(params);
    if (validationErrors.length > 0) {
      setError(validationErrors.join('；'));
      return;
    }
    try {
      const account = await createAccount({
        name: newAccountName.trim(),
        initialCapital: Number(params.initialCapital),
        commissionRate: Number(params.commissionRate),
        slippage: Number(params.slippage),
        positionMode: 'full_available',
        orderCash: null,
        orderPct: null,
        allowShort: params.allowShort,
        isDefault: accounts.length === 0,
      });
      await refreshAccounts();
      setSelectedAccountId(account.id);
      setNewAccountName('');
      setStatus(`已添加账号 ${account.name}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '添加账号失败');
    }
  };

  const handleRunBacktest = async () => {
    setError('');
    setStatus('');
    setBacktestErrorDetail(null);
    if (!hasSelectedConfig) {
      setError('请先选择策略方案，再运行回测');
      revealConfigSection('strategy');
      return;
    }
    if (versionDirty) {
      setError('策略内容已修改，请先保存策略');
      revealConfigSection('strategy');
      return;
    }
    const researchSignalError = getResearchSignalSelectionError(scriptForm.signalRequired, selectedSignalDatasetId);
    if (researchSignalError) {
      setError(researchSignalError);
      revealConfigSection('inputs');
      return;
    }
    if (researchSignalTimeframeError) {
      setError(researchSignalTimeframeError);
      revealConfigSection('inputs');
      return;
    }
    const selectedPreflight = preflight?.timeframes.find((item) => item.timeframe === selectedTimeframe);
    if (!selectedPreflight?.available) {
      showPreflightNotice(selectedPreflight?.reason || '正在检查当前策略的数据可用性');
      revealConfigSection('scope');
      return;
    }
    const validationErrors = validateParams(params);
    if (validationErrors.length > 0) {
      setError(validationErrors.join('；'));
      revealConfigSection(getConfigSectionForError(validationErrors[0]));
      return;
    }
    setRunning(true);
    try {
      const account = accounts.find((item) => item.id === selectedAccountId);
      const payload: BacktestRunPayload = {
        configId: selectedConfigId || null,
        configVersionId: null,
        scriptId: selectedScriptId || null,
        accountId: typeof selectedAccountId === 'number' ? selectedAccountId : null,
        account: account?.name || 'default',
        mode: 'backtest',
        symbol: selectedSymbol,
        benchmarkSymbol: selectedBenchmarkSymbol || selectedSymbol,
        strategy: selectedStrategy,
        signalDatasetId: selectedSignalDatasetId || null,
        timeframe: selectedTimeframe,
        startDate: params.startDate,
        endDate: params.endDate,
        initialCapital: Number(params.initialCapital),
        commissionRate: Number(params.commissionRate),
        slippage: Number(params.slippage),
        allowLong: params.allowLong,
        allowShort: params.allowShort,
        overlaySettlementPolicy: normalizeOverlaySettlementPolicy(params.overlaySettlementPolicy),
        strategyCategory: params.strategyCategory === 'core_position_overlay' ? 'core_position_overlay' : 'directional_holding',
        holdingPolicy: params.strategyCategory === 'core_position_overlay' ? 'intraday' : 'overnight',
        fillModel: normalizeFillModel(params.fillModel),
        executionAlgorithm: params.executionAlgorithm === 'twap' ? 'twap' : 'immediate',
        executionParameters: params.executionAlgorithm === 'twap'
          ? {
              durationSeconds: Number(params.twapDurationSeconds),
              intervalSeconds: Number(params.twapIntervalSeconds),
              minChildQuantity: Number(params.twapMinChildQuantity),
              completionPolicy: 'cancel_remaining',
            }
          : {},
        baseQuantity: params.strategyCategory === 'core_position_overlay' || params.initialPositionEnabled ? Number(params.baseQuantity) : 0,
        baseCostPrice: params.strategyCategory === 'core_position_overlay' || params.initialPositionEnabled ? Number(params.baseCostPrice) : 0,
        initialPositionEnabled: params.strategyCategory === 'core_position_overlay' || params.initialPositionEnabled,
        t0Enabled: params.strategyCategory === 'core_position_overlay',
        isDefault: false,
        strategyParams: Object.fromEntries(Object.entries(params).filter(([key]) => !EXECUTION_PARAM_KEYS.has(key))),
        runDraft: false,
      };
      const data = await runBacktest(payload);
      if (!('summary' in data)) {
        setHistoryPage(1);
        await refreshBacktestRuns(1);
        setPendingRun({ runId: data.runId, displayNo: data.displayNo });
        setActivePanel('overview');
        setStatus(`回测 ${formatRunReference(data.displayNo)} 已进入队列，完成后将自动展示概览和信号明细`);
        return;
      }
      setResult(data);
      setHoldingStartDate('');
      setHoldingEndDate('');
      setHoldingStatusFilter('all');
      setHistoryPage(1);
      void refreshBacktestRuns(1);
      setStatus(`回测完成 ${formatRunReference(data.displayNo)}，成交 ${getFillCount(data.summary) ?? 0} 笔`);
    } catch (e) {
      if (e instanceof BacktestRequestError) {
        setBacktestErrorDetail(e.detail);
      } else {
        setError(e instanceof Error ? e.message : '回测失败');
      }
    } finally {
      setRunning(false);
    }
  };

  const handleResetParams = () => {
    const account = accounts.find((item) => item.id === selectedAccountId);
    if (account) {
      applyAccount(account);
      setStatus(`已恢复账号 ${account.name} 的参数`);
    } else {
      setParams(DEFAULT_PARAMS);
      setStatus('已恢复默认参数');
    }
    setError('');
  };

  const buildDataCoverage = (
    label: string,
    statusData: DataStatus | null,
    loading: boolean,
    statusError: string,
  ) => {
    if (loading) return { tone: 'info', text: `正在检查${label} ${selectedTimeframe} K 线覆盖...` };
    if (statusError) return { tone: 'warn', text: `${label}数据状态读取失败：${statusError}` };
    if (!statusData || !statusData.exists) {
      return { tone: 'warn', text: `${label} ${selectedTimeframe} 尚未导入 QMT 历史数据` };
    }
    if (!isValidDate(params.startDate) || !isValidDate(params.endDate)) {
      return { tone: 'warn', text: '日期格式正确后再检查数据覆盖' };
    }
    const firstDate = statusData.firstDate || '-';
    const lastDate = statusData.lastDate || '-';
    const gaps = statusData.gapDates.filter((item) => (
      item.date >= params.startDate && item.date <= params.endDate
    ));
    if (!statusData.firstDate || !statusData.lastDate) {
      return { tone: 'warn', text: `${label} ${selectedTimeframe} K 线缺少可识别日期` };
    }
    if (params.startDate < statusData.firstDate || params.endDate > statusData.lastDate) {
      return {
        tone: 'warn',
        text: `${label} ${selectedTimeframe} 覆盖 ${firstDate}-${lastDate}，未完全覆盖当前区间`,
      };
    }
    if (gaps.length > 0) {
      return { tone: 'warn', text: `${label} ${selectedTimeframe} 覆盖区间，但存在 ${gaps.length} 个缺口日期` };
    }
    return { tone: 'ok', text: `${label} ${selectedTimeframe} 已覆盖 ${params.startDate}-${params.endDate}，共 ${statusData.barCount} 行` };
  };

  const formatMoney = (value: number | null | undefined) => (
    typeof value === 'number' && Number.isFinite(value)
      ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
      : '--'
  );

  const formatPct = (value: number | null | undefined) => (
    typeof value === 'number' && Number.isFinite(value)
      ? `${(value * 100).toFixed(2)}%`
      : '--'
  );
  const formatNumber = (value: number | null | undefined) => (
    typeof value === 'number' && Number.isFinite(value)
      ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
      : '--'
  );
  const formatOptionalPct = (value: unknown) => (
    typeof value === 'number' ? formatPct(value) : '--'
  );
  const formatOptionalNumber = (value: unknown) => (
    typeof value === 'number' ? formatNumber(value) : '--'
  );
  const isCorePositionOverlay = params.strategyCategory === 'core_position_overlay';
  const isL2Quote = params.fillModel === 'l2_quote';
  const preflightTimeframes = preflight?.timeframes || [];
  const selectedPreflight = preflightTimeframes.find((item) => item.timeframe === selectedTimeframe);
  const timeframeOptions = preflightTimeframes.length > 0
    ? preflightTimeframes
    : (isL2Quote
      ? L2_TIMEFRAMES.map((timeframe) => ({ timeframe, available: false, reason: '正在检查数据可用性' }))
      : backtestTimeframes.map((item) => ({
        ...item,
        reason: item.available ? '数据完整' : 'Bar 数据不完整',
      })));
  const dataCoverage = selectedPreflight
    ? {
      tone: selectedPreflight.available ? 'ok' : 'warning',
      text: selectedPreflight.reason,
    }
    : { tone: 'warning', text: '正在检查当前日期范围的数据可用性。' };
  const benchmarkCoverage = isL2Quote
    ? { tone: 'info', text: 'L2 模式不使用 QMT 基准覆盖；结果会保存 L2 数据来源版本。' }
    : buildDataCoverage(
      '基准标的',
      benchmarkDataStatus,
      loadingBenchmarkDataStatus,
      benchmarkDataStatusError,
    );
  const runPreflightReady = selectedPreflight?.available === true && (isL2Quote || benchmarkCoverage.tone === 'ok');
  const dataCoverageColor = dataCoverage.tone === 'ok' ? '#0f766e' : dataCoverage.tone === 'info' ? '#1d4ed8' : '#b45309';
  const resultHistoryRun = result?.runId ? backtestRuns.find((item) => item.id === result.runId) || null : null;
  const resultStrategyCategory = result?.strategyCategory || result?.experimentManifest?.strategyCategory || resultHistoryRun?.strategyCategory || params.strategyCategory;
  const isOverlayResult = resultStrategyCategory === 'core_position_overlay';
  const resultParams = resultHistoryRun ? asHistoryRecord(resultHistoryRun.paramsJson) : {};
  const resultInitialCash = Number(resultParams.initialCapital ?? params.initialCapital);
  const resultBaseQuantity = Number(resultParams.baseQuantity ?? params.baseQuantity);
  const resultBaseCostPrice = Number(resultParams.baseCostPrice ?? params.baseCostPrice);
  const resultInitialPositionEnabled = isOverlayResult
    || (resultHistoryRun ? resultParams.initialPositionEnabled === true : params.initialPositionEnabled);
  const initialTotalEquity = Number.isFinite(resultInitialCash)
    ? resultInitialCash + (resultInitialPositionEnabled && Number.isFinite(resultBaseQuantity) && Number.isFinite(resultBaseCostPrice)
      ? resultBaseQuantity * resultBaseCostPrice
      : 0)
    : null;
  const holdingDefaultStartDate = normalizeApiDate(resultHistoryRun?.startDate || params.startDate);
  const holdingDefaultEndDate = normalizeApiDate(resultHistoryRun?.endDate || params.endDate);
  const effectiveHoldingStartDate = holdingStartDate || holdingDefaultStartDate;
  const effectiveHoldingEndDate = holdingEndDate || holdingDefaultEndDate;
  const normalizedEquity = result ? result.equity.map((point) => ({ ...point, date: normalizeApiDate(point.date) })) : [];
  const rangedEquity = filterEquityByRange(normalizedEquity, chartRange);
  const rangedDates = new Set(rangedEquity.map((point) => point.date));
  const rangedBenchmark = result ? filterBenchmarkByDates(result.benchmarkEquity, rangedDates) : [];
  const dailyBars = buildDailyBars(rangedEquity, rangedBenchmark);
  const dailyHoldings = (result?.dailyHoldings || []).map((holding) => ({ ...holding, date: normalizeApiDate(holding.date) }));
  const getHoldingStatus = (holding: BacktestRunResponse['dailyHoldings'][number]): HoldingStatusFilter => {
    const netQuantity = holding.longQty - holding.shortQty;
    if (netQuantity > 0) return 'long';
    if (netQuantity < 0) return 'short';
    return 'flat';
  };

  const activeRun = useMemo(() => {
    const pending = pendingRun ? backtestRuns.find((run) => run.id === pendingRun.runId) : null;
    if (pending && (pending.status === 'queued' || pending.status === 'running' || pending.status === 'cancelling')) return pending;
    return backtestRuns.find((run) => run.status === 'cancelling')
      || backtestRuns.find((run) => run.status === 'running')
      || backtestRuns.find((run) => run.status === 'queued')
      || null;
  }, [backtestRuns, pendingRun]);
  const [activeRunClock, setActiveRunClock] = useState(Date.now());

  useEffect(() => {
    if (!activeRun || activeRun.status === 'queued') return undefined;
    setActiveRunClock(Date.now());
    const timer = window.setInterval(() => setActiveRunClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [activeRun?.id, activeRun?.status]);
  const activeRunElapsed = activeRun?.startedAt
    ? formatElapsedTime(activeRun.startedAt, activeRunClock)
    : '';

  const handleCancelRun = async () => {
    if (!activeRun) return;
    setCancellingRun(true);
    setError('');
    try {
      const response = await cancelBacktestRun(activeRun.id);
      const run = await fetchBacktestRun(activeRun.id);
      setBacktestRuns((prev) => prev.map((item) => (item.id === run.id ? run : item)));
      setStatus(
        response.status === 'cancelling'
          ? `回测 ${formatRunReference(activeRun.displayNo)} 正在取消`
          : `回测 ${formatRunReference(activeRun.displayNo)} ${response.status === 'cancelled' ? '已取消' : '已结束'}`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : '取消回测失败');
      await refreshBacktestRuns().catch(() => undefined);
    } finally {
      setCancellingRun(false);
    }
  };
  const filteredDailyHoldings = dailyHoldings.filter((holding) => {
    if (effectiveHoldingStartDate && holding.date < effectiveHoldingStartDate) return false;
    if (effectiveHoldingEndDate && holding.date > effectiveHoldingEndDate) return false;
    return holdingStatusFilter === 'all' || getHoldingStatus(holding) === holdingStatusFilter;
  });
  const latestHolding = filteredDailyHoldings[filteredDailyHoldings.length - 1];
  const longHoldingDays = filteredDailyHoldings.filter((holding) => getHoldingStatus(holding) === 'long').length;
  const shortHoldingDays = filteredDailyHoldings.filter((holding) => getHoldingStatus(holding) === 'short').length;
  const holdingPageCount = Math.max(1, Math.ceil(filteredDailyHoldings.length / holdingPageSize));
  const safeHoldingPage = Math.min(holdingPage, holdingPageCount);
  const holdingPageStart = filteredDailyHoldings.length > 0 ? (safeHoldingPage - 1) * holdingPageSize + 1 : 0;
  const holdingPageEnd = Math.min(safeHoldingPage * holdingPageSize, filteredDailyHoldings.length);
  const pagedDailyHoldings = filteredDailyHoldings.slice(
    holdingPageStart > 0 ? holdingPageStart - 1 : 0,
    holdingPageEnd,
  );
  const allSignals = result
    ? [...result.trades, ...(result.skippedSignals || [])].sort((left, right) => (
      (left.signalTime || left.time).localeCompare(right.signalTime || right.time)
    ))
    : [];
  const tradeByFillId = new Map<string, BacktestTrade>();
  for (const trade of result?.trades || []) {
    if (trade.fillId) tradeByFillId.set(trade.fillId, trade);
  }
  const filledSignalCount = allSignals.filter((trade) => trade.status !== 'skipped').length;
  const skippedSignalCount = allSignals.length - filledSignalCount;
  const filteredTrades = allSignals.filter((trade) => {
    if (tradeSideFilter !== 'all' && trade.side !== tradeSideFilter) return false;
    if (tradeStatusFilter === 'filled' && trade.status === 'skipped') return false;
    if (tradeStatusFilter === 'skipped' && trade.status !== 'skipped') return false;
    const closedLoopPnl = getClosedLoopTradePnl(trade, isOverlayResult);
    if (tradePnlFilter === 'win' && (closedLoopPnl === null || closedLoopPnl <= 0)) return false;
    if (tradePnlFilter === 'loss' && (closedLoopPnl === null || closedLoopPnl >= 0)) return false;
    return true;
  });
  const tradePageCount = Math.max(1, Math.ceil(filteredTrades.length / tradePageSize));
  const safeTradePage = Math.min(tradePage, tradePageCount);
  const tradePageStart = filteredTrades.length > 0 ? (safeTradePage - 1) * tradePageSize + 1 : 0;
  const tradePageEnd = Math.min(safeTradePage * tradePageSize, filteredTrades.length);
  const pagedTrades = filteredTrades.slice(tradePageStart > 0 ? tradePageStart - 1 : 0, tradePageEnd);
  const tradeColumnDefs: Array<{
    key: TradeColumnKey;
    label: string;
    width: string;
    align?: 'right';
    render: (trade: BacktestRunResponse['trades'][number]) => ReactNode;
  }> = [
    {
      key: 'time',
      label: '信号 K 线时间',
      width: 'minmax(140px, 1.3fr)',
      render: (trade) => formatSignalTime(trade.signalTime || trade.time),
    },
    {
      key: 'executionTime',
      label: '成交时间',
      width: 'minmax(140px, 1.3fr)',
      render: (trade) => trade.status === 'skipped'
        ? '--'
        : formatSignalTime(trade.executionTime || trade.time),
    },
    {
      key: 'status',
      label: '执行状态',
      width: 'minmax(82px, 0.8fr)',
      render: (trade) => {
        const skipped = trade.status === 'skipped';
        return (
          <span className={`trade-status-chip ${skipped ? 'is-skipped' : 'is-filled'}`} style={{ color: skipped ? '#b45309' : '#0f766e', fontWeight: 700 }}>
            {skipped ? '已跳过' : '已成交'}
          </span>
        );
      },
    },
    {
      key: 'direction',
      label: '方向',
      width: 'minmax(100px, 1fr)',
      render: (trade) => (
        <span className={`trade-side-label ${trade.side === 'BUY' ? 'is-buy' : 'is-sell'}`} style={{ color: trade.side === 'BUY' ? '#dc2626' : '#16a34a', fontWeight: 600 }}>
          {formatTradeDirection(trade.side)}
        </span>
      ),
    },
    {
      key: 'action',
      label: '开平动作',
      width: 'minmax(104px, 1fr)',
      render: (trade) => TRADE_ACTION_LABELS[trade.action || trade.direction] || trade.action || trade.direction,
    },
    {
      key: 'source',
      label: '来源',
      width: 'minmax(82px, 0.8fr)',
      render: (trade) => {
        const systemSource = getTradeSource(trade.direction);
        const isSystem = systemSource === '系统结算';
        const moduleName = trade.strategyModule === 'A'
          ? 'A 趋势'
          : trade.strategyModule === 'B'
            ? 'B 均值'
            : '策略信号';
        const researchContext = trade.researchSignalId
          ? ` · ${trade.researchSignalAction || '研究信号'}`
          : '';
        const source = isSystem ? systemSource : `${moduleName}${researchContext}`;
        return (
          <span title={trade.researchSignalId ? `研究信号 ${trade.researchSignalId}` : undefined} style={{
            display: 'inline-flex', alignItems: 'center', height: 22, padding: '0 7px',
            borderRadius: 11, fontSize: 11, fontWeight: 700,
            color: isSystem ? '#9a3412' : '#0369a1',
            background: isSystem ? '#ffedd5' : '#e0f2fe',
          }}>
            {source}
          </span>
        );
      },
    },
    {
      key: 'note',
      label: '说明',
      width: 'minmax(160px, 1.5fr)',
      render: (trade) => formatTradeNote(trade.direction, trade.note, trade.fundingLimited),
    },
    {
      key: 'price',
      label: '成交价/数量',
      width: 'minmax(132px, 1.35fr)',
      align: 'right',
      render: (trade) => (
        <span>
          <span className="trade-price-value">{`${trade.price.toFixed(2)} ×${trade.quantity}`}</span>
          <span className="trade-price-source">
            {trade.priceSource || '成交模型价格'}
          </span>
        </span>
      ),
    },
    {
      key: 'pnl',
      label: '收益',
      width: 'minmax(90px, 0.9fr)',
      align: 'right',
      render: (trade) => {
        const closedLoopPnl = getClosedLoopTradePnl(trade, isOverlayResult);
        if (closedLoopPnl === null) return <span title="本次成交尚未形成闭环">--</span>;
        return (
          <span className={`trade-pnl-value ${closedLoopPnl > 0 ? 'is-profit' : closedLoopPnl < 0 ? 'is-loss' : ''}`} style={{ color: getPnlColor(closedLoopPnl), fontWeight: 600 }}>
            {closedLoopPnl.toFixed(2)}
          </span>
        );
      },
    },
  ];
  const visibleTradeColumns = tradeColumnDefs.filter((column) => tradeColumns[column.key]);
  const tradeGridColumns = visibleTradeColumns.map((column) => column.width).join(' ') || '1fr';
  const overlaySummary = useMemo(() => {
    if (!result || !isOverlayResult) return result?.summary || null;
    if (typeof result.summary.t0ExcessPnl === 'number') return result.summary;
    const lastHolding = dailyHoldings[dailyHoldings.length - 1];
    if (resultBaseQuantity <= 0 || resultBaseCostPrice <= 0 || !lastHolding) return result.summary;
    const initialEquity = resultInitialCash + resultBaseQuantity * resultBaseCostPrice;
    const finalEquity = Number(lastHolding.totalEquity);
    const finalClose = Number(lastHolding.close);
    if (!Number.isFinite(initialEquity) || !Number.isFinite(finalEquity) || !Number.isFinite(finalClose)) return result.summary;
    const baseFinalEquity = resultInitialCash + resultBaseQuantity * finalClose;
    const baseHoldingPnl = baseFinalEquity - initialEquity;
    const t0ExcessPnl = finalEquity - baseFinalEquity;
    return {
      ...result.summary,
      initialEquity,
      totalPnl: finalEquity - initialEquity,
      totalReturn: initialEquity > 0 ? (finalEquity / initialEquity) - 1 : 0,
      baseHoldingPnl,
      baseHoldingReturn: initialEquity > 0 ? baseHoldingPnl / initialEquity : null,
      t0ExcessPnl,
      t0ExcessReturn: t0ExcessPnl / (resultBaseQuantity * resultBaseCostPrice),
    };
  }, [dailyHoldings, isOverlayResult, result, resultBaseCostPrice, resultBaseQuantity, resultInitialCash]);
  const coreMetrics = isOverlayResult ? [
    ['账户总收益', overlaySummary ? formatMoney(overlaySummary.accountPnl ?? overlaySummary.totalPnl) : '--'],
    ['底仓基准收益', overlaySummary ? formatMoney(overlaySummary.baseBenchmarkPnl ?? overlaySummary.baseHoldingPnl) : '--'],
    ['增强收益', overlaySummary ? formatMoney(overlaySummary.overlayPnl ?? overlaySummary.t0ExcessPnl) : '--'],
    ['已实现增强收益', overlaySummary ? formatMoney(overlaySummary.realizedOverlayPnl) : '--'],
    ['未实现增强收益', overlaySummary ? formatMoney(overlaySummary.unrealizedOverlayPnl) : '--'],
    ['成交 / 完整周期胜率', result ? `${formatNumber(getFillCount(result.summary))} / ${formatPct(result.summary.cycleWinRate)}` : '--'],
  ] : [
    ['总收益', result ? formatPct(result.summary.totalReturn) : '--'],
    ['年化收益', result ? formatPct(result.summary.annualReturn) : '--'],
    ['超额收益', result ? formatPct(result.excessReturn) : '--'],
    ['最大回撤', result ? formatPct(result.summary.maxDrawdownRate) : '--'],
    ['Sharpe', result ? formatNumber(result.summary.annualSharpe) : '--'],
    ['完整交易 / 胜率', result ? `${formatNumber(result.summary.completedTradeCount ?? getFillCount(result.summary))} / ${formatPct(result.summary.winRate)}` : '--'],
  ];
  const metricGroups = [
    {
      title: '收益',
      rows: isOverlayResult ? [
        ['账户总收益', overlaySummary ? formatMoney(overlaySummary.accountPnl ?? overlaySummary.totalPnl) : '--'],
        ['最终权益', overlaySummary ? formatMoney(overlaySummary.finalEquity) : '--'],
        ['总收益率', overlaySummary ? formatPct(overlaySummary.totalReturn) : '--'],
        ['底仓基准收益', overlaySummary ? formatMoney(overlaySummary.baseBenchmarkPnl ?? overlaySummary.baseHoldingPnl) : '--'],
        ['增强收益', overlaySummary ? formatMoney(overlaySummary.overlayPnl ?? overlaySummary.t0ExcessPnl) : '--'],
        ['已实现增强收益', overlaySummary ? formatMoney(overlaySummary.realizedOverlayPnl) : '--'],
        ['未实现增强收益', overlaySummary ? formatMoney(overlaySummary.unrealizedOverlayPnl) : '--'],
        ['年化收益', result ? formatPct(result.summary.annualReturn) : '--'],
      ] : [
        ['总收益', result ? formatMoney(result.summary.totalPnl) : '--'],
        ['最终权益', result ? formatMoney(result.summary.finalEquity) : '--'],
        ['总收益率', result ? formatPct(result.summary.totalReturn) : '--'],
        ['基准收益率', result ? formatPct(result.benchmarkReturn) : '--'],
        ['超额收益率', result ? formatPct(result.excessReturn) : '--'],
        ['年化收益', result ? formatPct(result.summary.annualReturn) : '--'],
      ],
    },
    {
      title: '风险',
      rows: [
        ['最大回撤', result ? formatMoney(result.summary.maxDrawdown) : '--'],
        ['最大回撤率', result ? formatPct(result.summary.maxDrawdownRate) : '--'],
        ['年化波动率', result ? formatPct(result.summary.annualVolatility) : '--'],
        ['Sharpe', result ? formatNumber(result.summary.annualSharpe) : '--'],
        ['Sortino', result ? formatNumber(result.summary.sortino) : '--'],
      ],
    },
    {
      title: '交易质量',
      rows: [
        [isOverlayResult ? '完整周期胜率' : '完整交易胜率', result ? formatPct(isOverlayResult ? result.summary.cycleWinRate : result.summary.winRate) : '--'],
        ['日胜率', result ? formatPct(result.summary.dailyWinRate) : '--'],
        ['盈亏比', result ? formatNumber(result.summary.profitLossRatio) : '--'],
        ['成交笔数', result ? formatNumber(getFillCount(result.summary)) : '--'],
        [isOverlayResult ? '完整周期数' : '完整交易数', result ? String(isOverlayResult ? result.summary.completedCycleCount ?? 0 : result.summary.completedTradeCount ?? 0) : '--'],
        [isOverlayResult ? '盈利周期' : '盈利交易', result ? String(isOverlayResult ? result.summary.winningCycles ?? 0 : result.summary.winningTrades) : '--'],
        [isOverlayResult ? '亏损周期' : '亏损交易', result ? String(isOverlayResult ? result.summary.losingCycles ?? 0 : result.summary.losingTrades) : '--'],
        [isOverlayResult ? '未完成周期' : '未完成交易', result ? String(isOverlayResult ? result.summary.openCycleCount ?? 0 : result.summary.openTradeCount ?? 0) : '--'],
        ['平均盈利', result ? formatMoney(result.summary.avgWin) : '--'],
        ['平均亏损', result ? formatMoney(result.summary.avgLoss) : '--'],
      ],
    },
  ];

  const resultTabs = [
    { id: 'code' as WorkbenchPanel, label: '策略代码', icon: Code2 },
    { id: 'overview' as WorkbenchPanel, label: '概览', icon: FileSliders },
    { id: 'trades' as WorkbenchPanel, label: '信号明细', icon: History },
    { id: 'positions' as WorkbenchPanel, label: '持仓收益', icon: Database },
    { id: 'history' as WorkbenchPanel, label: '历史回测', icon: History },
  ];
  const activeConfig = strategyConfigs.find((item) => item.id === selectedConfigId);
  const configNameById = new Map(strategyConfigs.map((item) => [item.id, item.name]));
  const selectedHistoryRun = backtestRuns.find((item) => item.id === selectedHistoryRunId)
    || historyRunDetails[selectedHistoryRunId]
    || null;
  const compareHistoryRun = backtestRuns.find((item) => item.id === compareHistoryRunId)
    || historyRunDetails[compareHistoryRunId]
    || null;
  const compareHistoryOptions = compareHistoryRun
    && !historyCompareOptions.some((item) => item.id === compareHistoryRun.id)
    ? [compareHistoryRun, ...historyCompareOptions]
    : historyCompareOptions;
  const selectedHistoryConfigName = selectedHistoryRun?.configId
    ? configNameById.get(selectedHistoryRun.configId) || '已保存方案'
    : '未绑定方案';
  const selectedHistoryCategory = selectedHistoryRun?.strategyCategory
    || (selectedHistoryRun ? getHistoryRunParam(selectedHistoryRun, 'strategyCategory') : null);
  const selectedHistoryIsOverlay = selectedHistoryCategory === 'core_position_overlay';
  const selectedHistoryMetrics: Array<[string, string]> = selectedHistoryRun ? [
    ['总收益率', formatOptionalPct(selectedHistoryRun.summaryJson.totalReturn)],
    ['最大回撤', formatOptionalPct(selectedHistoryRun.summaryJson.maxDrawdownRate)],
    ['夏普比率', formatOptionalNumber(selectedHistoryRun.summaryJson.annualSharpe)],
    ...(selectedHistoryIsOverlay
      ? [
        ['增强收益', formatOptionalNumber(selectedHistoryRun.summaryJson.overlayPnl ?? selectedHistoryRun.summaryJson.t0ExcessPnl)],
        ['完整周期胜率', formatOptionalPct(selectedHistoryRun.summaryJson.cycleWinRate)],
      ] as Array<[string, string]>
      : [
        ['交易胜率', formatOptionalPct(selectedHistoryRun.summaryJson.winRate)],
        ['完整交易数', formatOptionalNumber(selectedHistoryRun.summaryJson.completedTradeCount)],
      ] as Array<[string, string]>),
  ] : [];
  const historyComparisonScope = selectedHistoryRun && compareHistoryRun
    ? [
      selectedHistoryRun.symbol === compareHistoryRun.symbol ? '同标的' : '标的不同',
      selectedHistoryRun.timeframe === compareHistoryRun.timeframe ? '同周期' : '周期不同',
      selectedHistoryRun.startDate === compareHistoryRun.startDate && selectedHistoryRun.endDate === compareHistoryRun.endDate
        ? '同区间'
        : '区间不同',
    ]
    : [];
  const resultManifest = result?.experimentManifest;
  const resultCostModel = resultManifest?.costModel;
  const resultContext = {
    runReference: result?.runId ? formatRunReference(result.displayNo) : '当前设置',
    configName: resultHistoryRun?.configId
      ? configNameById.get(resultHistoryRun.configId) || '已保存策略方案'
      : activeConfig?.name || configName || '未选择策略方案',
    symbol: resultHistoryRun?.symbol || selectedSymbol,
    benchmarkSymbol: resultHistoryRun?.benchmarkSymbol || result?.benchmarkSymbol || selectedBenchmarkSymbol,
    timeframe: resultHistoryRun?.timeframe || resultManifest?.barRange?.timeframe || selectedTimeframe,
    startDate: resultHistoryRun?.startDate || params.startDate,
    endDate: resultHistoryRun?.endDate || params.endDate,
    commissionRate: resultCostModel?.commissionRate ?? Number(params.commissionRate),
    slippage: resultCostModel?.slippage ?? Number(params.slippage),
    overlaySettlementPolicy: resultManifest?.overlaySettlementPolicy || String(params.overlaySettlementPolicy),
    holdingPolicy: resultManifest?.holdingPolicy || String(params.holdingPolicy),
    strategyCategory: result?.strategyCategory || resultManifest?.strategyCategory || resultHistoryRun?.strategyCategory || params.strategyCategory,
  };
  const hasWorkbenchSidebar = !['history', 'trades', 'positions', 'code'].includes(activePanel);
  const historyParamDiffs = compareHistoryRuns(selectedHistoryRun, compareHistoryRun);
  const filteredHistoryRuns = backtestRuns;
  const deletableFilteredHistoryRuns = filteredHistoryRuns.filter(
    (run) => run.status !== 'queued' && run.status !== 'running' && run.status !== 'cancelling',
  );
  const selectedDeletableHistoryRunIds = deletableFilteredHistoryRuns
    .filter((run) => selectedHistoryRunIds.has(run.id))
    .map((run) => run.id);
  const allDeletableHistoryRunsSelected = deletableFilteredHistoryRuns.length > 0
    && selectedDeletableHistoryRunIds.length === deletableFilteredHistoryRuns.length;
  const historyPageCount = Math.max(1, Math.ceil(historyTotal / historyPageSize));
  const safeHistoryPage = Math.min(historyPage, historyPageCount);
  const historyPageStart = (safeHistoryPage - 1) * historyPageSize;
  const pagedHistoryRuns = filteredHistoryRuns;
  const historyRangeStart = historyTotal > 0 ? historyPageStart + 1 : 0;
  const historyRangeEnd = Math.min(historyPageStart + filteredHistoryRuns.length, historyTotal);
  const historyGridColumns = '32px 36px minmax(64px, 0.5fr) minmax(150px, 1.25fr) minmax(150px, 1.25fr) minmax(120px, 0.95fr) minmax(76px, 0.6fr) minmax(76px, 0.6fr) minmax(70px, 0.5fr) minmax(130px, 1fr) 88px';
  const activePanelLabel = resultTabs.find((item) => item.id === activePanel)?.label || '概览';
  const runDisabled = running || activeRun != null || !hasSelectedConfig || versionDirty;
  const statusMessage = error || status || (versionDirty
    ? '策略内容已修改，请先保存策略后运行回测。'
    : activeConfig ? `当前策略：${activeConfig.name}` : '请先新建并保存策略');
  const statusColor = error ? '#b91c1c' : status ? '#0f766e' : '#94a3b8';
  const statusBackground = error ? '#fef2f2' : status ? '#ecfdf5' : '#f8fafc';
  const handleExportTrades = () => {
    downloadCsv(
      `trades_${selectedSymbol}_${params.startDate}_${params.endDate}.csv`,
      filteredTrades.map((trade) => ({
        signalTime: formatSignalTime(trade.signalTime || trade.time),
        executionTime: formatSignalTime(trade.executionTime || trade.time),
        status: trade.status === 'skipped' ? '已跳过' : '已成交',
        direction: trade.direction,
        side: trade.side,
        action: trade.action || trade.direction,
        price: trade.price,
        priceSource: trade.priceSource || '成交模型价格',
        quantity: trade.quantity,
        pnl: getClosedLoopTradePnl(trade, isOverlayResult) ?? '',
        researchSignalId: trade.researchSignalId || '',
        researchSignalAction: trade.researchSignalAction || '',
        researchSignalEffectiveAt: trade.researchSignalEffectiveAt || '',
        note: trade.note,
      }))
    );
  };

  const handleViewTradeChart = (trade: BacktestRunResponse['trades'][number]) => {
    if (!result?.runId || trade.status === 'skipped') return;
    const executionTime = (trade.executionTime || trade.time).replace(/\D/g, '');
    if (executionTime.length < 8) {
      setError('该成交记录缺少可定位的实际成交时间');
      return;
    }
    onViewTradeChart({
      runId: result.runId,
      symbol: resultContext.symbol,
      timeframe: resultContext.timeframe || selectedTimeframe,
      date: normalizeApiDate(executionTime.slice(0, 8)),
    });
  };

  const handleApplyBacktestRun = (run: BacktestRunHistory) => {
    const saved = run.paramsJson;
    const textValue = (key: string, fallback: string) => (
      saved[key] == null ? fallback : String(saved[key])
    );
    const boolValue = (key: string, fallback: boolean) => (
      typeof saved[key] === 'boolean' ? Boolean(saved[key]) : fallback
    );
    const savedStrategyParams = (
      saved.strategyParams != null && typeof saved.strategyParams === 'object'
    )
      ? saved.strategyParams as Partial<BacktestParams>
      : {};
    setSelectedSymbol(run.symbol);
    setSelectedTimeframe(run.timeframe || '5m');
    setSelectedBenchmarkSymbol(run.benchmarkSymbol || run.symbol);
    setSelectedStrategy(run.strategy);
    setSelectedConfigId(run.configId || '');
    setSelectedScriptId(run.scriptId || '');
    setSelectedAccountId(run.accountId || '');
    const restoredStrategyCategory = saved.strategyCategory === 'core_position_overlay' || saved.strategyCategory === 'directional_holding'
      ? saved.strategyCategory
      : run.strategyCategory === 'core_position_overlay' || run.strategyCategory === 'directional_holding'
        ? run.strategyCategory
        : Number(saved.baseQuantity || 0) > 0 || saved.t0Enabled === true
          ? 'core_position_overlay'
          : 'directional_holding';
    const restoredInitialPositionEnabled = restoredStrategyCategory === 'core_position_overlay'
      || saved.initialPositionEnabled === true;
    setParams({
      ...DEFAULT_PARAMS,
      ...savedStrategyParams,
      startDate: normalizeApiDate(run.startDate),
      endDate: normalizeApiDate(run.endDate),
      initialCapital: textValue('initialCapital', DEFAULT_PARAMS.initialCapital),
      commissionRate: textValue('commissionRate', DEFAULT_PARAMS.commissionRate),
      slippage: textValue('slippage', DEFAULT_PARAMS.slippage),
      allowLong: boolValue('allowLong', DEFAULT_PARAMS.allowLong),
      allowShort: boolValue('allowShort', DEFAULT_PARAMS.allowShort),
      overlaySettlementPolicy: normalizeOverlaySettlementPolicy(saved.overlaySettlementPolicy ?? savedStrategyParams.overlaySettlementPolicy),
      strategyCategory: restoredStrategyCategory,
      holdingPolicy: restoredStrategyCategory === 'core_position_overlay' ? 'intraday' : 'overnight',
      fillModel: normalizeFillModel(savedStrategyParams.fillModel || run.fillModel),
      executionAlgorithm: run.paramsJson.executionAlgorithm === 'twap' ? 'twap' : 'immediate',
      twapDurationSeconds: String(asHistoryRecord(run.paramsJson.executionParameters).durationSeconds ?? DEFAULT_PARAMS.twapDurationSeconds),
      twapIntervalSeconds: String(asHistoryRecord(run.paramsJson.executionParameters).intervalSeconds ?? DEFAULT_PARAMS.twapIntervalSeconds),
      twapMinChildQuantity: String(asHistoryRecord(run.paramsJson.executionParameters).minChildQuantity ?? DEFAULT_PARAMS.twapMinChildQuantity),
      baseQuantity: restoredInitialPositionEnabled ? textValue('baseQuantity', DEFAULT_PARAMS.baseQuantity) : DEFAULT_PARAMS.baseQuantity,
      baseCostPrice: restoredInitialPositionEnabled ? textValue('baseCostPrice', DEFAULT_PARAMS.baseCostPrice) : DEFAULT_PARAMS.baseCostPrice,
      initialPositionEnabled: restoredInitialPositionEnabled,
      t0Enabled: restoredStrategyCategory === 'core_position_overlay',
    });
    setStatus(`已恢复历史回测 ${formatRunReference(run.displayNo)} 的参数`);
  };

  const handleViewHistoryResult = (run: BacktestRunHistory) => {
    if (!run.resultJson) {
      setError('该历史记录未保存完整结果，请重新运行后查看图表和信号明细。');
      return;
    }
    setResult({
      ...run.resultJson,
      runId: run.id,
      displayNo: run.displayNo,
      configId: run.configId,
      configVersionId: run.configVersionId,
      scriptId: run.scriptId,
    });
    setHoldingStartDate('');
    setHoldingEndDate('');
    setHoldingStatusFilter('all');
    setActivePanel('overview');
    setStatus(`正在查看历史回测 ${formatRunReference(run.displayNo)} 的结果`);
  };

  const handleRerunHistory = async (run: BacktestRunHistory) => {
    const saved = run.paramsJson;
    const account = accounts.find((item) => item.id === run.accountId);
    setError('');
    setStatus('');
    setBacktestErrorDetail(null);
    setRunning(true);
    const rerunStrategyCategory = saved.strategyCategory === 'core_position_overlay' || saved.strategyCategory === 'directional_holding'
      ? saved.strategyCategory
      : run.strategyCategory === 'core_position_overlay' || run.strategyCategory === 'directional_holding'
        ? run.strategyCategory
        : Number(saved.baseQuantity ?? 0) > 0 || saved.t0Enabled === true
          ? 'core_position_overlay'
          : 'directional_holding';
    const rerunInitialPositionEnabled = rerunStrategyCategory === 'core_position_overlay'
      || saved.initialPositionEnabled === true;
    try {
      const data = await runBacktest({
        configId: run.configId,
        configVersionId: run.configVersionId,
        scriptId: run.scriptId,
        accountId: run.accountId,
        account: account?.name || 'default',
        mode: 'backtest',
        symbol: run.symbol,
        benchmarkSymbol: run.benchmarkSymbol,
        strategy: run.strategy,
        signalDatasetId: typeof saved.signalDatasetId === 'string' ? saved.signalDatasetId : null,
        timeframe: run.timeframe || '5m',
        startDate: normalizeApiDate(run.startDate),
        endDate: normalizeApiDate(run.endDate),
        initialCapital: Number(saved.initialCapital ?? 1_000_000),
        commissionRate: Number(saved.commissionRate ?? 0),
        slippage: Number(saved.slippage ?? 0),
        allowLong: saved.allowLong !== false,
        allowShort: saved.allowShort !== false,
        overlaySettlementPolicy: normalizeOverlaySettlementPolicy(saved.overlaySettlementPolicy),
        strategyCategory: rerunStrategyCategory,
        holdingPolicy: rerunStrategyCategory === 'core_position_overlay' ? 'intraday' : 'overnight',
        fillModel: normalizeFillModel(run.fillModel),
        executionAlgorithm: run.paramsJson.executionAlgorithm === 'twap' ? 'twap' : 'immediate',
        executionParameters: asHistoryRecord(run.paramsJson.executionParameters) as Record<string, number | string>,
        baseQuantity: Number(rerunInitialPositionEnabled ? saved.baseQuantity ?? 0 : 0),
        baseCostPrice: Number(rerunInitialPositionEnabled ? saved.baseCostPrice ?? 0 : 0),
        initialPositionEnabled: rerunInitialPositionEnabled,
        t0Enabled: rerunStrategyCategory === 'core_position_overlay',
        isDefault: false,
        strategyParams: (saved.strategyParams || {}) as Record<string, string | boolean>,
      });
      if (!('summary' in data)) {
        setHistoryPage(1);
        await refreshBacktestRuns(1);
        setPendingRun({ runId: data.runId, displayNo: data.displayNo });
        setActivePanel('overview');
        setStatus(`回测 ${formatRunReference(data.displayNo)} 已重新进入队列，完成后将自动展示结果`);
        return;
      }
      setResult(data);
      setHoldingStartDate('');
      setHoldingEndDate('');
      setHoldingStatusFilter('all');
      setHistoryPage(1);
      await refreshBacktestRuns(1);
      setActivePanel('overview');
      setStatus(`已重新运行回测 ${formatRunReference(run.displayNo)}，新记录为 ${formatRunReference(data.displayNo)}`);
    } catch (e) {
      if (e instanceof BacktestRequestError) {
        setBacktestErrorDetail(e.detail);
      } else {
        setError(e instanceof Error ? e.message : '重新运行回测失败');
      }
    } finally {
      setRunning(false);
    }
  };

  const handleSaveHistoryMeta = async () => {
    if (typeof selectedHistoryRunId !== 'string') return;
    setError('');
    setStatus('');
    try {
      const tags = historyTagsDraft
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean);
      const run = await updateBacktestRunMeta(selectedHistoryRunId, {
        note: historyNoteDraft.trim(),
        tags,
      });
      setBacktestRuns((prev) => prev.map((item) => (item.id === run.id ? run : item)));
      setStatus(`已保存历史记录 ${formatRunReference(run.displayNo)} 的备注和标签`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存历史备注失败');
    }
  };

  const openPublicationDialog = (run: BacktestRunHistory) => {
    if (run.publicationId) {
      onViewSharedBacktest?.(run.publicationId);
      return;
    }
    const eligibilityError = getPublicationEligibilityError(run);
    if (eligibilityError) {
      setError(eligibilityError);
      return;
    }
    setPublicationRun(run);
    setPublicationTitle(`${run.symbol} ${formatStrategyName(run.strategy)}回测`);
    setPublicationDescription('');
    setPublicationTags('');
    setPublicationError('');
  };

  const handlePublishBacktest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!publicationRun) return;
    setPublishingRun(true);
    setPublicationError('');
    try {
      const response = await publishBacktestRun(publicationRun.id, {
        title: publicationTitle.trim(),
        description: publicationDescription.trim(),
        tags: publicationTags.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
      });
      const publicationId = response.publication.id;
      setBacktestRuns((runs) => runs.map((run) => (
        run.id === publicationRun.id ? { ...run, publicationId } : run
      )));
      setHistoryRunDetails((runs) => ({
        ...runs,
        [publicationRun.id]: { ...(runs[publicationRun.id] || publicationRun), publicationId },
      }));
      setPublicationRun(null);
      setStatus(response.created ? '已共享到策略广场' : '该回测已经共享，已打开现有条目');
      onViewSharedBacktest?.(publicationId);
    } catch (reason) {
      setPublicationError(reason instanceof Error ? reason.message : '共享回测失败');
    } finally {
      setPublishingRun(false);
    }
  };

  const handleDeleteBacktestRun = async (run: BacktestRunHistory) => {
    if (run.status === 'queued' || run.status === 'running' || run.status === 'cancelling') {
      setError('排队或运行中的回测不能删除，请先取消任务。');
      return;
    }
    const confirmed = window.confirm(
      `确认永久删除历史回测 ${formatRunReference(run.displayNo)} 吗？\n该操作会删除数据库中的回测记录和关联任务数据，无法恢复。`,
    );
    if (!confirmed) return;
    setError('');
    setStatus('');
    try {
      await deleteBacktestRun(run.id);
      setSelectedHistoryRunIds((previous) => {
        const next = new Set(previous);
        next.delete(run.id);
        return next;
      });
      if (selectedHistoryRunId === run.id) setSelectedHistoryRunId('');
      if (compareHistoryRunId === run.id) setCompareHistoryRunId('');
      await refreshBacktestRuns();
      setStatus(`已永久删除历史回测 ${formatRunReference(run.displayNo)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除历史回测失败');
    }
  };

  const handleSetHistoryFavorite = async (run: BacktestRunHistory) => {
    const favorited = !run.isFavorited;
    setError('');
    setStatus('');
    setFavoritingHistoryRunIds((current) => new Set(current).add(run.id));
    try {
      const favoriteState = await setBacktestRunFavorite(run.id, favorited);
      const updateFavorite = (item: BacktestRunHistory): BacktestRunHistory => (
        item.id === run.id
          ? {
            ...item,
            isFavorited: favoriteState.isFavorited,
            favoritedAt: favoriteState.favoritedAt,
          }
          : item
      );
      setBacktestRuns((current) => (
        historyFavoritesOnly && !favoriteState.isFavorited
          ? current.filter((item) => item.id !== run.id)
          : current.map(updateFavorite)
      ));
      setHistoryRunDetails((current) => {
        const detail = current[run.id];
        return detail ? { ...current, [run.id]: updateFavorite(detail) } : current;
      });
      if (historyFavoritesOnly && !favoriteState.isFavorited) {
        await refreshBacktestRuns();
      }
      setStatus(
        `${favoriteState.isFavorited ? '已收藏' : '已取消收藏'}历史回测 ${formatRunReference(run.displayNo)}`,
      );
    } catch (favoriteError) {
      setError(favoriteError instanceof Error ? favoriteError.message : '更新收藏状态失败');
    } finally {
      setFavoritingHistoryRunIds((current) => {
        const next = new Set(current);
        next.delete(run.id);
        return next;
      });
    }
  };

  const toggleHistoryRunSelection = (runId: string, checked: boolean) => {
    setSelectedHistoryRunIds((previous) => {
      const next = new Set(previous);
      if (checked) next.add(runId);
      else next.delete(runId);
      return next;
    });
  };

  const toggleAllHistoryRunSelections = (checked: boolean) => {
    setSelectedHistoryRunIds(checked ? new Set(deletableFilteredHistoryRuns.map((run) => run.id)) : new Set());
  };

  const handleDeleteSelectedHistoryRuns = async () => {
    const runIds = selectedDeletableHistoryRunIds;
    if (runIds.length === 0) return;
    const confirmed = window.confirm(
      `确认永久删除选中的 ${runIds.length} 条历史回测吗？\n该操作会删除数据库中的回测记录和关联任务数据，无法恢复。`,
    );
    if (!confirmed) return;
    setError('');
    setStatus('');
    try {
      const deleted = await deleteBacktestRuns(runIds);
      const deletedIds = new Set(runIds);
      if (selectedHistoryRunId && deletedIds.has(selectedHistoryRunId)) setSelectedHistoryRunId('');
      if (compareHistoryRunId && deletedIds.has(compareHistoryRunId)) setCompareHistoryRunId('');
      setSelectedHistoryRunIds(new Set());
      await refreshBacktestRuns();
      setStatus(`已永久删除 ${deleted} 条历史回测`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '批量删除历史回测失败');
    }
  };

  return (
    <div className="backtest-workbench" style={{
      flex: 1,
      minHeight: 0,
      background: '#eef3f8',
      color: '#0f172a',
      display: 'grid',
      gridTemplateRows: activePanel === 'overview' ? '52px minmax(0, 1fr)' : 'minmax(0, 1fr)',
      overflow: 'hidden',
    }}>
      {preflightNotice && (
        <div role="status" style={{
          position: 'fixed', right: 24, bottom: 24, zIndex: 40, maxWidth: 360,
          padding: '10px 12px', borderRadius: 6, border: '1px solid #f59e0b', background: '#fffbeb',
          color: '#92400e', boxShadow: '0 8px 24px rgba(15, 23, 42, 0.16)', fontSize: 13, lineHeight: 1.5,
        }}>
          {preflightNotice}
        </div>
      )}

      {publicationRun && (
        <div
          role="presentation"
          style={{
            position: 'fixed', inset: 0, zIndex: 80, display: 'grid', placeItems: 'center',
            padding: 20, background: 'rgba(15, 23, 42, 0.58)',
          }}
        >
          <form
            role="dialog"
            aria-modal="true"
            aria-label="共享到策略广场"
            onSubmit={handlePublishBacktest}
            style={{
              width: 'min(620px, 100%)', display: 'grid', gap: 14, padding: 22,
              borderRadius: 8, background: '#ffffff', boxShadow: '0 20px 48px rgba(15, 23, 42, 0.3)',
            }}
          >
            <div>
              <h2 style={{ margin: 0 }}>共享到策略广场</h2>
              <p style={{ marginBottom: 0, color: '#64748b' }}>
                回测 {formatRunReference(publicationRun.displayNo)} 的代码、业务参数、结果与依赖摘要将对全体内部登录用户可见。
              </p>
            </div>
            <label>
              <div style={labelStyle}>共享标题</div>
              <input required maxLength={128} value={publicationTitle} onChange={(event) => setPublicationTitle(event.target.value)} style={fieldStyle} />
            </label>
            <label>
              <div style={labelStyle}>策略说明</div>
              <textarea
                required
                maxLength={4000}
                rows={6}
                value={publicationDescription}
                onChange={(event) => setPublicationDescription(event.target.value)}
                placeholder="说明策略思路、适用范围和已知限制"
                style={{ ...fieldStyle, height: 'auto', padding: 10, resize: 'vertical' }}
              />
            </label>
            <label>
              <div style={labelStyle}>标签（可选，逗号分隔）</div>
              <input value={publicationTags} onChange={(event) => setPublicationTags(event.target.value)} style={fieldStyle} />
            </label>
            <div style={{ padding: 10, borderRadius: 6, background: '#fffbeb', color: '#92400e', fontSize: 12 }}>
              请先检查策略代码中是否包含凭据或其他敏感文字。账户关联、内部路径和系统诊断不会被共享。
            </div>
            {publicationError && <div role="alert" style={{ color: '#b91c1c' }}>{publicationError}</div>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button type="button" disabled={publishingRun} onClick={() => setPublicationRun(null)}>取消</button>
              <button type="submit" disabled={publishingRun || !publicationTitle.trim() || !publicationDescription.trim()}>
                {publishingRun ? '正在共享...' : '确认共享'}
              </button>
            </div>
          </form>
        </div>
      )}

      {activePanel === 'overview' && <header className="workbench-header" style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(260px, 1fr) auto auto',
        alignItems: 'center',
        gap: 10,
        padding: '8px 14px',
        borderBottom: '1px solid #e2e8f0',
        background: '#ffffff',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
          <label style={{
            display: 'grid',
            gridTemplateColumns: 'auto minmax(180px, 1fr)',
            alignItems: 'center',
            gap: 6,
            flex: '0 0 310px',
            minWidth: 0,
          }}>
            <span style={{ color: '#64748b', fontSize: 12, whiteSpace: 'nowrap' }}>
              策略方案
            </span>
            <select
              value={selectedConfigId}
              onChange={(e) => handleConfigSelect(e.target.value)}
              style={fieldStyle}
            >
              <option value="">未选择策略方案</option>
              {strategyConfigs.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <div style={{
            minWidth: 0,
            color: statusColor,
            background: statusBackground,
            border: `1px solid ${error ? '#fecaca' : status ? '#99f6e4' : '#d7dde8'}`,
            borderRadius: 4,
            padding: '7px 10px',
            fontSize: 13,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {statusMessage}
          </div>
        </div>

        {activeRun ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: '#a16207', fontSize: 12 }}>
              {activeRun.fillModel === 'l2_quote' ? 'L2 盘口回放' : '标准 Bar 回放'} {formatRunReference(activeRun.displayNo)} {activeRun.status === 'queued' ? '排队中' : activeRun.status === 'cancelling' ? '取消中' : '运行中'}
              {activeRun.status !== 'queued' && activeRunElapsed ? ` · ${activeRunElapsed}` : ''}
            </span>
            {activeRun.status !== 'cancelling' && (
              <button
                onClick={handleCancelRun}
                disabled={cancellingRun}
                style={{ height: 36, borderRadius: 4, border: '1px solid #dc2626', background: '#fef2f2', color: '#b91c1c', padding: '0 14px', cursor: cancellingRun ? 'wait' : 'pointer', fontWeight: 700, fontSize: 14 }}
              >
                {cancellingRun ? '正在取消...' : '取消回测'}
              </button>
            )}
          </div>
        ) : (
          <button
            className="workbench-primary-action"
            onClick={handleRunBacktest}
            disabled={runDisabled}
            title={!hasSelectedConfig
              ? '请先选择策略方案'
              : versionDirty
                ? '请先保存策略'
                : !runPreflightReady
                  ? '当前日期范围数据不完整'
                  : undefined}
            style={{
              height: 36,
              borderRadius: 4,
              border: `1px solid ${runDisabled ? '#cbd5e1' : !runPreflightReady ? '#f59e0b' : '#0f766e'}`,
              background: runDisabled ? '#f1f5f9' : !runPreflightReady ? '#fffbeb' : '#ccfbf1',
              color: runDisabled ? '#94a3b8' : !runPreflightReady ? '#92400e' : '#0f766e',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 7,
              padding: '0 14px',
              cursor: running ? 'wait' : runDisabled ? 'not-allowed' : 'pointer',
              opacity: runDisabled ? 0.7 : 1,
              fontWeight: 700,
              fontSize: 14,
            }}
          >
            <Play size={15} />
            {running ? '处理中...' : !hasSelectedConfig ? '请选择策略方案' : '运行回测'}
          </button>
        )}
      </header>}

      <div className="workbench-grid" style={{
        minHeight: 0,
        display: 'grid',
        gridTemplateColumns: hasWorkbenchSidebar
          ? '56px 340px minmax(0, 1fr)'
          : '56px minmax(0, 1fr)',
        gap: 12,
        padding: 12,
        background: '#eef3f8',
        overflow: 'hidden',
        position: 'relative',
      }}>
        {backtestErrorDetail && (
          <div style={{ position: 'absolute', top: 12, right: 12, width: 'min(560px, calc(100% - 24px))', zIndex: 5 }}>
            <BacktestErrorCard detail={backtestErrorDetail} />
          </div>
        )}
        <aside className="workbench-nav" style={{
          minHeight: 0,
          overflow: 'visible',
          border: '1px solid #dbe1ea',
          borderRadius: 8,
          background: '#f8fafc',
        }}>
          <BacktestTabs
            activeTab={activePanel}
            tabs={resultTabs}
            onChange={(tabId) => setActivePanel(tabId as WorkbenchPanel)}
          />
        </aside>

        {hasWorkbenchSidebar && (
        <aside className="workbench-rail" style={{
          minHeight: 0,
          overflow: 'auto',
          border: '1px solid #dbe1ea',
          borderRadius: 8,
          background: '#f8fafc',
          padding: 14,
        }}>
          <div className="workbench-rail-title" style={{
            color: '#0f172a',
            fontSize: 15,
            fontWeight: 700,
            marginBottom: 12,
          }}>
            实验设置
          </div>

          {activePanel === 'overview' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div className="workbench-overview-card" style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#64748b', fontSize: 12, marginBottom: 8 }}>当前策略方案</div>
                <div style={{ color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                  <div>名称：{activeConfig?.name || configName || '未选择策略方案'}</div>
                  <div>模式：回测</div>
                  <div>脚本版本：{activeConfig ? `v${activeConfig.scriptVersionNo}` : '未绑定'}</div>
                  {activeConfig?.sharedSource && (
                    <div>
                      来源：{activeConfig.sharedSource.title} · 原作者 {activeConfig.sharedSource.authorName}
                    </div>
                  )}
                  <div>标的：{selectedSymbol}</div>
                  <div>基准：{selectedBenchmarkSymbol}</div>
                </div>
              </div>
            </div>
          )}

          {activePanel !== 'code' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700 }}>市场</div>
                <InstrumentSearchField label="运行标的" value={selectedSymbol} onChange={setSelectedSymbol} />
                <InstrumentSearchField label="基准标的" value={selectedBenchmarkSymbol} onChange={setSelectedBenchmarkSymbol} />
              </section>

              <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700 }}>回测区间</div>
                <BacktestDateRangePicker
                  startDate={params.startDate} endDate={params.endDate} days={calendarPreflight?.calendarDays || []}
                  onChange={(key, value) => setParam(key, value)} onMonthChange={setCalendarMonth}
                />
              </section>

              <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700 }}>资金与交易规则</div>
                <label>
                  <div style={labelStyle}>策略类别</div>
                  <select value={params.strategyCategory} onChange={(e) => setStrategyCategory(e.target.value === 'core_position_overlay' ? 'core_position_overlay' : 'directional_holding')} style={fieldStyle}>
                    <option value="directional_holding">方向性持仓策略</option>
                    <option value="core_position_overlay">底仓增强策略</option>
                  </select>
                  <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.55, marginTop: 6 }}>
                    {isCorePositionOverlay ? '围绕固定目标底仓交易；可选择日内恢复或跨日延续增强仓。' : '在信号触发时开仓、平仓并跨日持有；核心评价持仓与方向收益。'}
                  </div>
                </label>
                {!isCorePositionOverlay && (
                  <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, color: '#334155', fontSize: 13, cursor: 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={params.initialPositionEnabled}
                      onChange={(e) => setInitialPositionEnabled(e.target.checked)}
                      style={{ marginTop: 2 }}
                    />
                    <span>
                      <span style={{ fontWeight: 700 }}>启用初始底仓</span>
                      <span style={{ display: 'block', color: '#64748b', fontSize: 12, lineHeight: 1.55, marginTop: 3 }}>关闭时以现金开始；启用后初始持仓参与收益，并按 A 股 T+1 规则处理。</span>
                    </span>
                  </label>
                )}
                {(isCorePositionOverlay || params.initialPositionEnabled) && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, borderTop: '1px solid #dbe3ef', paddingTop: 10 }}>
                    <div style={{ color: '#0f172a', fontSize: 12, fontWeight: 700 }}>{isCorePositionOverlay ? '底仓增强' : '初始底仓'}</div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                      <label>
                        <div style={labelStyle}>底仓数量</div>
                        <input value={params.baseQuantity} onChange={(e) => setParam('baseQuantity', e.target.value)} style={fieldStyle} />
                      </label>
                      <label>
                        <div style={labelStyle}>底仓成本价</div>
                        <input value={params.baseCostPrice} onChange={(e) => setParam('baseCostPrice', e.target.value)} style={fieldStyle} />
                      </label>
                    </div>
                    {isCorePositionOverlay && (
                      <label>
                        <div style={labelStyle}>增强仓结算规则</div>
                        <select value={params.overlaySettlementPolicy} onChange={(e) => setParam('overlaySettlementPolicy', normalizeOverlaySettlementPolicy(e.target.value))} style={fieldStyle}>
                          <option value="intraday_restore">日内恢复目标底仓</option>
                          <option value="cross_day_carry">允许跨日延续</option>
                        </select>
                      </label>
                    )}
                    <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.55 }}>
                      {isCorePositionOverlay
                        ? params.overlaySettlementPolicy === 'cross_day_carry'
                          ? '日终不自动补回；未完成周期跨日延续并按期末行情估值。允许跨日不会放宽 T+1。'
                          : '日终恢复目标底仓；所有成交仍受真实持仓、资金和 T+1 约束。'
                        : '首个交易日的初始持仓可卖；当日新买入股票仍不可卖出。'}
                    </div>
                  </div>
                )}
                <label>
                  <div style={labelStyle}>初始资金</div>
                  <input value={params.initialCapital} onChange={(e) => setParam('initialCapital', e.target.value)} style={fieldStyle} />
                </label>
                <div>
                  <div style={labelStyle}>默认 A 股费用</div>
                  <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.55 }}>佣金万分之 0.8（单笔最低 5 元）· 过户费万分之 0.1 · 卖出印花税万分之 5</div>
                </div>
                <label>
                  <div style={labelStyle}>滑点</div>
                  <input value={params.slippage} onChange={(e) => setParam('slippage', e.target.value)} style={fieldStyle} />
                </label>
                <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 10, color: '#64748b', fontSize: 12, lineHeight: 1.55 }}>
                  策略方向：{params.allowLong ? '允许做多' : '不做多'} · {params.allowShort ? '允许做空' : '不做空'}；请在“策略管理”中修改并保存策略。
                </div>
              </section>


              <section style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700, marginBottom: 8 }}>
                  运行前检查
                </div>
                <div style={{ color: dataCoverageColor, fontSize: 12, lineHeight: 1.65 }}>
                  {dataCoverage.text}
                </div>

                <div style={{ color: benchmarkCoverage.tone === 'ok' ? '#0f766e' : '#b45309', fontSize: 12, lineHeight: 1.65, marginTop: 6 }}>
                  {benchmarkCoverage.text}
                </div>
                <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.65, marginTop: 8 }}>
                  {activeConfig
                    ? versionDirty
                      ? '策略内容已修改；请先保存策略后运行。'
                      : `将使用已保存策略「${activeConfig.name}」。`
                    : '请先在策略管理中创建并保存策略。'}
                </div>
              </section>
            </div>
          )}

          {activePanel === 'code' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                <div style={{ color: '#0f172a', fontWeight: 700, marginBottom: 6 }}>
                  {isCustomScriptStrategy(selectedStrategy) ? '当前自定义脚本' : '实际运行代码'}
                </div>
                {isCustomScriptStrategy(selectedStrategy) ? (
                  <>
                    <div>{scriptForm.name || '尚未命名的策略脚本'}</div>
                    <div style={{ marginTop: 8 }}>策略方案版本会保存这份代码快照与参数；请在“策略管理”中编辑代码并保存生成版本。</div>
                    <textarea
                      value={scriptForm.code}
                      readOnly
                      spellCheck={false}
                      style={{
                        width: '100%', minHeight: 420, resize: 'vertical', marginTop: 10, background: '#f8fafc',
                        color: '#334155', border: '1px solid #d7dde8', borderRadius: 6, padding: 14,
                        fontSize: 13, lineHeight: 1.55, fontFamily: 'Consolas, monospace', outline: 'none',
                      }}
                    />
                  </>
                ) : (
                  <>
                    <div>{strategySource?.name || '当前策略未提供代码快照'}</div>
                    {strategySource && <div style={{ marginTop: 5 }}>版本指纹：{strategySource.codeHash.slice(0, 12)}</div>}
                    <div style={{ marginTop: 8 }}>策略方案会保存这份代码的快照与参数；代码修改需在实际策略文件中完成。</div>
                    {strategySourceError && <div style={{ color: '#b91c1c', marginTop: 8 }}>{strategySourceError}</div>}
                  </>
                )}
              </div>
            </div>
          )}
          {false && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>历史回测：{historyTotal}</div>
                <div>当前方案：{activeConfig?.name || '未选择'}</div>
                <div>方案版本：{configVersions.length}</div>
                <div>当前脚本版本：{activeConfig?.scriptVersionNo ?? '未选择'}</div>
              </div>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700, marginBottom: 10 }}>
                  方案版本
                </div>
                {configVersions.length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {configVersions.slice(0, 8).map((version) => (
                      <div key={version.id} style={{ borderBottom: '1px solid #e2e8f0', paddingBottom: 8 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, color: '#334155', fontSize: 12, fontWeight: 700 }}>
                          <span>v{version.versionNo}</span>
                          <span>{version.createdAt}</span>
                        </div>
                        <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                          {version.symbol} · {version.mode === 'live' ? '运行' : '回测'}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                    保存策略后会自动生成版本。
                  </div>
                )}
              </div>
              <button
                onClick={() => void refreshBacktestRuns()}
                disabled={historyLoading}
                style={{
                  height: 36,
                  borderRadius: 4,
                  border: '1px solid #cbd5e1',
                  background: '#f8fafc',
                  color: '#334155',
                  cursor: historyLoading ? 'wait' : 'pointer',
                }}
              >
                {historyLoading ? '刷新中...' : '刷新历史'}
              </button>
            </div>
          )}

          {activePanel === 'logs' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>已保存策略：{strategyConfigs.length}</div>
                <div>独立脚本：{strategyConfigs.length}</div>
                <div>当前回测：{result ? formatRunReference(result.displayNo) : '暂无'}</div>
              </div>
              <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                日志输出后续会接入回测和运行过程日志；当前先显示策略方案和脚本统计。
              </div>
            </div>
          )}

        </aside>
        )}

        <main style={{
          minWidth: 0,
          minHeight: 0,
          overflow: 'auto',
          padding: activePanel === 'overview' ? 4 : '16px 20px 24px',
          background: 'transparent',
        }}>
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
            minHeight: '100%',
            width: activePanel === 'overview' ? '100%' : 'min(1440px, 100%)',
            margin: activePanel === 'overview' ? 0 : '0 auto',
          }}>
            {activePanel === 'code' ? (
            <section className="strategy-studio" style={{
              ...panelStyle,
              width: 'min(1440px, 100%)',
              margin: '0 auto',
              minHeight: 'calc(100vh - 164px)',
              padding: 16,
              background: '#ffffff',
              display: 'grid',
              gridTemplateRows: 'auto minmax(0, 1fr)',
              gap: 12,
            }}>
              <div className="strategy-studio-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 12 }}>
                <div>
                  <div className="strategy-studio-title" style={{ color: '#0f172a', fontSize: 16, fontWeight: 700 }}>
                  {scriptForm.name || '未命名策略'}
                  </div>
                  <div className="strategy-studio-subtitle" style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                    策略资产、代码快照与回测配置在同一工作台中维护
                  </div>
                </div>
                <div className={`strategy-studio-state ${error ? 'is-error' : versionDirty ? 'is-dirty' : hasSelectedConfig ? 'is-ready' : ''}`} aria-live="polite">
                  {error ? '需处理异常' : versionDirty ? '未保存修改' : hasSelectedConfig ? '已保存版本' : '新建中'}
                </div>
              </div>
              <div className="strategy-studio-grid" style={{ display: 'grid', gridTemplateColumns: '280px minmax(0, 1fr) 340px', gap: 12, minHeight: 0 }}>
                <section className="strategy-asset-panel" style={{
                  ...panelStyle,
                  padding: 12,
                  background: '#f8fafc',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 12,
                  minHeight: 0,
                  overflow: 'auto',
                }}>
                  <div className="strategy-panel-heading">
                    <span>策略资产</span>
                    <span>{strategyConfigs.length} 个方案</span>
                  </div>
                  <label>
                    <div style={labelStyle}>策略方案</div>
                    <select
                      value={selectedConfigId}
                      onChange={(e) => handleConfigSelect(e.target.value)}
                      style={fieldStyle}
                    >
                      <option value="">未选择策略方案</option>
                      {strategyConfigs.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <input
                    value={configName}
                    onChange={(e) => setConfigName(e.target.value)}
                    placeholder="策略名称"
                    style={fieldStyle}
                  />
                  {renderStrategyDirectionControls()}
                  <div className={`strategy-save-notice ${versionDirty ? 'is-dirty' : hasSelectedConfig ? 'is-ready' : ''}`} style={{
                    borderRadius: 4,
                    padding: '8px 10px',
                    background: versionDirty ? '#fffbeb' : '#ecfdf5',
                    color: versionDirty ? '#b45309' : '#0f766e',
                    fontSize: 12,
                    lineHeight: 1.55,
                  }}>
                    {versionDirty
                      ? '策略内容已修改，请先保存后再运行回测。'
                      : hasSelectedConfig
                        ? '当前策略内容已保存，可直接运行回测。'
                        : '新建策略保存后即可运行回测。'}
                  </div>
                  <div className="strategy-asset-actions" style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 8 }}>
                    <button
                      className="strategy-save-action"
                      onClick={handleNewStrategy}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #cbd5e1',
                        background: '#ffffff',
                        color: '#334155',
                        cursor: 'pointer',
                      }}
                    >
                      新建策略
                    </button>
                    <button
                      onClick={handleCopyStrategy}
                      disabled={!hasSelectedConfig}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #cbd5e1',
                        background: '#ffffff',
                        color: '#334155',
                        cursor: hasSelectedConfig ? 'pointer' : 'not-allowed',
                        opacity: hasSelectedConfig ? 1 : 0.55,
                      }}
                    >
                      复制策略
                    </button>
                    <button
                      onClick={handleSaveConfig}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #0f766e',
                        background: '#ccfbf1',
                        color: '#0f766e',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 6,
                        cursor: 'pointer',
                      }}
                    >
                      <Save size={14} />
                      保存策略
                    </button>
                    <button
                      onClick={handleDeleteConfig}
                      disabled={!hasSelectedConfig}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #cbd5e1',
                        background: '#ffffff',
                        color: '#64748b',
                        cursor: hasSelectedConfig ? 'pointer' : 'not-allowed',
                        opacity: hasSelectedConfig ? 1 : 0.55,
                      }}
                    >
                      删除策略
                    </button>
                  </div>
                  <div className="strategy-asset-note" style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                    保存会同时更新代码和参数，并生成不可变版本。每次回测都绑定当时的精确版本。
                  </div>
                </section>

                <section className="strategy-editor-panel" style={{
                  ...panelStyle,
                  padding: 12,
                  background: '#ffffff',
                  display: 'grid',
                  gridTemplateRows: 'auto minmax(0, 1fr)',
                  gap: 12,
                  minHeight: 0,
                }}>
                  <div className="strategy-editor-top">
                    <div className="strategy-editor-heading" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                      <div>
                        <div style={{ color: '#0f172a', fontSize: 14, fontWeight: 700 }}>
                          {selectedStrategy === 'user_script' ? '可编辑策略代码' : '实际运行代码'}
                        </div>
                        <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                          {selectedStrategy === 'user_script'
                            ? '编辑完成后点击左侧“保存策略”，保存后即可回测'
                            : (strategySource ? `代码指纹：${strategySource.codeHash.slice(0, 12)}` : '正在读取代码快照')}
                        </div>
                      </div>
                      {selectedStrategy === 'user_script' && (
                        <button
                          className="strategy-guide-trigger"
                          onClick={() => setStrategyGuideOpen(true)}
                          type="button"
                        >
                          <BookOpen size={15} />
                          策略编写指南
                        </button>
                      )}
                    </div>
                    <StrategyStudioErrorNotice message={error} />
                  </div>
                  {selectedStrategy === 'user_script' ? (
                    <div className="strategy-editor-body" style={{ display: 'grid', gridTemplateRows: 'auto auto auto minmax(0, 1fr) auto', gap: 10, minHeight: 0 }}>
                      <input
                        value={scriptForm.name}
                        onChange={(e) => updateScriptForm({ name: e.target.value })}
                        placeholder="策略名称，例如：双均线趋势"
                        style={fieldStyle}
                      />
                      <input
                        value={scriptForm.description}
                        onChange={(e) => updateScriptForm({ description: e.target.value })}
                        placeholder="策略说明（选填）"
                        style={fieldStyle}
                      />
                      <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, color: '#334155', fontSize: 12 }}>
                        <input
                          type="checkbox"
                          checked={scriptForm.signalRequired}
                          onChange={(event) => {
                            const enabled = event.target.checked;
                            updateScriptForm({ signalRequired: enabled });
                            if (!enabled) setSelectedSignalDatasetId('');
                          }}
                        />
                        <span>
                          <strong>研究信号驱动策略</strong>
                          <span style={{ display: 'block', color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                            启用后必须选择研究信号，代码也必须显式读取 bar['researchSignal']。
                          </span>
                        </span>
                      </label>
                      <textarea
                        className="strategy-code-editor"
                        value={scriptForm.code}
                        onChange={(e) => updateScriptForm({ code: e.target.value })}
                        spellCheck={false}
                        style={{
                          width: '100%', minHeight: 420, resize: 'vertical', background: '#f8fafc',
                          color: '#334155', border: '1px solid #cbd5e1', borderRadius: 6, padding: 14,
                          fontSize: 13, lineHeight: 1.55, fontFamily: 'Consolas, monospace', outline: 'none',
                        }}
                      />
                      <div className="strategy-editor-footer" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                        <span style={{ color: '#64748b', fontSize: 12 }}>
                          接口：on_bar(bar, state)；可选 on_quote(quote, state) 与 on_order_event(event, state)。time 为 YYYYMMDDHHMMSS，tradeDate 为 YYYY-MM-DD，hhmm 例如 931；{params.fillModel === 'l2_quote' ? '定义 on_quote 时连续回放 3 秒快照，新订单仅可用后续快照撮合。' : '标准 Bar 执行 on_bar 与成交回报，不执行 on_quote；信号按下一根可交易 Bar 开盘价加滑点成交。'}支持导入后端已安装模块。
                        </span>
                        <span style={{ color: '#64748b', fontSize: 12 }}>代码将随“保存策略”一并保存。</span>
                      </div>
                    </div>
                  ) : (
                    <textarea
                      className="strategy-code-editor is-readonly"
                      value={strategySourceCode || strategySourceError}
                      readOnly
                      spellCheck={false}
                      style={{
                        width: '100%', minHeight: 520, resize: 'none', background: '#f8fafc',
                        color: '#334155', border: '1px solid #d7dde8', borderRadius: 6, padding: 14,
                        fontSize: 13, lineHeight: 1.55, fontFamily: 'Consolas, monospace', outline: 'none',
                      }}
                    />
                  )}
                </section>
                <StrategyGuideDrawer
                  open={strategyGuideOpen}
                  onClose={() => setStrategyGuideOpen(false)}
                  onUseExample={handleUseStrategyGuideExample}
                />

                <section className="strategy-config-panel" style={{
                  ...panelStyle,
                  padding: 0,
                  background: '#f8fafc',
                  display: 'flex',
                  flexDirection: 'column',
                  minHeight: 0,
                  overflow: 'hidden',
                }}>
                  <div className="strategy-config-header">
                    <div className="strategy-panel-heading">
                      <span>回测配置</span>
                      <span>按组展开</span>
                    </div>
                    <div className="strategy-config-context">
                      {selectedSymbol} · {selectedTimeframe} · {isCorePositionOverlay ? '底仓增强' : '方向持仓'}
                    </div>
                  </div>
                  <div className="strategy-config-scroll">
                  <ConfigSectionCard
                    title="回测范围"
                    summary={`${selectedSymbol} · ${selectedTimeframe} · ${dataCoverage.tone === 'ok' ? '数据就绪' : '需检查'}`}
                    defaultOpen
                    sectionRef={(element) => { configSectionRefs.current.scope = element; }}
                  >
                    <InstrumentSearchField label="运行标的" value={selectedSymbol} onChange={setSelectedSymbol} />
                    <label>
                      <div style={labelStyle}>运行周期</div>
                      <select
                        value={selectedTimeframe}
                        onChange={(e) => setVersionTimeframe(e.target.value)}
                        style={fieldStyle}
                      >
                        {timeframeOptions.map((item) => (
                          <option key={item.timeframe} value={item.timeframe}>
                            {item.timeframe}（{item.available ? '数据完整' : item.reason || '数据不完整'}）
                          </option>
                        ))}
                      </select>
                      {!isL2Quote && backtestTimeframes.length === 0 && (
                        <div className="strategy-config-warning">正在读取回测引擎支持的周期。</div>
                      )}
                      {!isL2Quote && selectedTimeframe === '1d' && selectedPreflight?.available && (
                        <div className="strategy-config-help">
                          {selectedPreflight.derivation ? '日线由完整 1m 数据派生' : '使用 QMT 原始日线'}
                        </div>
                      )}
                    </label>
                    <InstrumentSearchField label="基准标的" value={selectedBenchmarkSymbol} onChange={setSelectedBenchmarkSymbol} />
                    <div>
                      <div style={labelStyle}>回测区间</div>
                      <BacktestDateRangePicker
                        startDate={params.startDate} endDate={params.endDate} days={calendarPreflight?.calendarDays || []}
                        onChange={(key, value) => setParam(key, value)} onMonthChange={setCalendarMonth}
                      />
                    </div>
                    <div className={`strategy-config-check ${dataCoverage.tone === 'ok' ? 'is-ready' : 'is-warning'}`}>
                      {dataCoverage.text}
                    </div>
                    <div className={`strategy-config-check ${benchmarkCoverage.tone === 'ok' ? 'is-ready' : 'is-warning'}`}>
                      {benchmarkCoverage.text}
                    </div>
                  </ConfigSectionCard>
                  <ConfigSectionCard
                    title="策略与仓位"
                    summary={isCorePositionOverlay ? '底仓增强策略' : '方向性持仓策略'}
                    defaultOpen
                    sectionRef={(element) => { configSectionRefs.current.strategy = element; }}
                  >
                  <label>
                    <div style={labelStyle}>策略类别</div>
                    <select value={params.strategyCategory} onChange={(e) => setStrategyCategory(e.target.value === 'core_position_overlay' ? 'core_position_overlay' : 'directional_holding')} style={fieldStyle}>
                      <option value="directional_holding">方向性持仓策略</option>
                      <option value="core_position_overlay">底仓增强策略</option>
                    </select>
                    <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5, marginTop: 5 }}>
                      {isCorePositionOverlay ? '底仓增强围绕固定目标底仓，可按配置日内恢复或跨日延续。' : '方向性持仓允许跨日保留实际仓位。'}
                    </div>
                  </label>
                  {!isCorePositionOverlay && (
                    <label className="strategy-position-toggle">
                      <input
                        type="checkbox"
                        checked={params.initialPositionEnabled}
                        onChange={(e) => setInitialPositionEnabled(e.target.checked)}
                      />
                      <span>
                        <strong>启用初始底仓</strong>
                        <small>关闭时以现金开始；启用后初始持仓参与收益。</small>
                      </span>
                    </label>
                  )}
                  {(isCorePositionOverlay || params.initialPositionEnabled) && (
                    <div className="strategy-position-settings">
                      <div className="strategy-position-grid">
                        <label>
                          <div style={labelStyle}>底仓数量</div>
                          <input value={params.baseQuantity} onChange={(e) => setParam('baseQuantity', e.target.value)} style={fieldStyle} />
                        </label>
                        <label>
                          <div style={labelStyle}>底仓成本价</div>
                          <input value={params.baseCostPrice} onChange={(e) => setParam('baseCostPrice', e.target.value)} style={fieldStyle} />
                        </label>
                      </div>
                      {isCorePositionOverlay && (
                        <label>
                          <div style={labelStyle}>增强仓结算规则</div>
                          <select value={params.overlaySettlementPolicy} onChange={(e) => setParam('overlaySettlementPolicy', normalizeOverlaySettlementPolicy(e.target.value))} style={fieldStyle}>
                            <option value="intraday_restore">日内恢复目标底仓</option>
                            <option value="cross_day_carry">允许跨日延续</option>
                          </select>
                        </label>
                      )}
                      {isCorePositionOverlay ? (
                        <>
                          <div className="strategy-position-limit">已启用底仓做 T 额度限制</div>
                          <div className="strategy-config-help">{params.overlaySettlementPolicy === 'cross_day_carry'
                            ? '日终撤销未成交委托但不补回；周期跨日延续。任务结束未恢复部分计入未实现增强收益，不计入周期胜率。'
                            : '日终恢复目标底仓；当日买入股票不可卖出，必要的系统结算会保留可审计告警。'}
                          </div>
                        </>
                      ) : (
                        <div className="strategy-config-help">初始持仓在首个交易日可卖；当日新买入股票仍按 A 股 T+1 限制，策略不会在日终自动恢复底仓。</div>
                      )}
                    </div>
                  )}
                  <div>
                    <div style={labelStyle}>策略</div>
                    <div className="strategy-readonly-field" style={fieldStyle}>
                      {formatStrategyName(selectedStrategy)}
                    </div>
                  </div>
                  </ConfigSectionCard>
                  <ConfigSectionCard
                    title="成交与执行"
                    summary={`${isL2Quote ? 'L2 盘口' : '标准 Bar'} · ${params.executionAlgorithm === 'twap' ? 'TWAP' : '立即成交'}`}
                    sectionRef={(element) => { configSectionRefs.current.execution = element; }}
                  >
                  <label>
                    <div style={labelStyle}>成交模式</div>
                    <select
                      value={params.fillModel}
                      onChange={(e) => setFillModel(e.target.value === 'l2_quote' ? 'l2_quote' : 'bar_next_open')}
                      style={fieldStyle}
                    >
                      <option value="bar_next_open">标准 Bar 回放（快速）</option>
                      <option value="l2_quote">L2 盘口回放（真实）</option>
                    </select>
                    {isL2Quote && (
                      <div style={{ color: '#1d4ed8', fontSize: 11, lineHeight: 1.5, marginTop: 5 }}>
                        on_bar 在 Bar 结束时触发；定义 on_quote 的脚本会连续接收 3 秒盘口，新委托只使用严格晚于当前事件的盘口成交。纯 on_bar 脚本仍按订单驱动读取盘口。
                      </div>
                    )}
                    {params.fillModel === 'bar_next_open' && (
                      <div style={{ color: '#1d4ed8', fontSize: 11, lineHeight: 1.5, marginTop: 5 }}>
                        on_bar 在已完成 Bar 的收盘时触发；信号在下一根可交易 Bar 的开盘价加固定滑点成交，虚拟成交后会调用 on_order_event。
                      </div>
                    )}
                    {params.fillModel === 'bar_next_open' && hasQuoteCallback && (
                      <div style={{ color: '#b45309', fontSize: 11, lineHeight: 1.5, marginTop: 5 }}>
                        当前策略定义了 on_quote：标准 Bar 回放仍会执行 on_bar，但不会执行盘口止损、止盈等实时快照逻辑。
                      </div>
                    )}
                    <div style={{ display: 'block', marginTop: 10 }}>
                      <div style={labelStyle}>执行方式</div>
                      <select
                        value={params.executionAlgorithm}
                        onChange={(e) => setExecutionAlgorithm(e.target.value === 'twap' ? 'twap' : 'immediate')}
                        style={fieldStyle}
                      >
                        <option value="immediate">立即成交</option>
                        <option value="twap" disabled={!isL2Quote}>TWAP（L2）</option>
                      </select>
                    </div>
                    {params.executionAlgorithm === 'twap' && (
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
                        <div>
                          <div style={labelStyle}>执行时长（秒）</div>
                          <input value={params.twapDurationSeconds} onChange={(e) => setParam('twapDurationSeconds', e.target.value)} style={fieldStyle} />
                        </div>
                        <div>
                          <div style={labelStyle}>子单间隔（秒）</div>
                          <input value={params.twapIntervalSeconds} onChange={(e) => setParam('twapIntervalSeconds', e.target.value)} style={fieldStyle} />
                        </div>
                        <div>
                          <div style={labelStyle}>最小子单数量</div>
                          <input value={params.twapMinChildQuantity} onChange={(e) => setParam('twapMinChildQuantity', e.target.value)} style={fieldStyle} />
                        </div>
                        <div style={{ gridColumn: '1 / -1', color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                          仅使用信号之后的 3 秒快照；执行窗口结束仍未成交的数量将自动撤销。
                        </div>
                      </div>
                    )}
                  </label>
                  </ConfigSectionCard>
                  <ConfigSectionCard
                    title="策略参数与信号"
                    summary={scriptForm.signalRequired ? '需要研究信号' : `${strategyParameterFields.length} 个自定义参数`}
                    defaultOpen={scriptForm.signalRequired}
                    sectionRef={(element) => { configSectionRefs.current.inputs = element; }}
                  >
                  <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700 }}>研究信号</div>
                    <select
                      value={selectedSignalDatasetId}
                      onChange={(event) => {
                        setSelectedSignalDatasetId(event.target.value);
                        setResearchSignalLoadError('');
                      }}
                      disabled={isL2Quote || !scriptForm.signalRequired || loadingResearchSignals}
                      style={fieldStyle}
                    >
                      <option value="">{loadingResearchSignals ? '正在加载研究信号…' : '不使用研究信号'}</option>
                      {researchSignalDatasets.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name} · v{item.version ?? '—'} · {item.timeframe ?? '—'} · {item.recordCount} 条
                        </option>
                      ))}
                    </select>
                    <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                      {scriptForm.signalRequired
                        ? "当前有效信号会持续注入 bar['researchSignal']；策略负责仓位、底仓与风控。"
                        : '当前策略未声明为研究信号驱动策略。'}
                    </div>
                    {researchSignalLoadError && (
                      <div style={{ color: '#b91c1c', fontSize: 11, lineHeight: 1.5 }}>
                        {researchSignalLoadError}
                      </div>
                    )}
                    {selectedResearchSignal && (
                      <div style={{
                        display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8,
                        padding: 10, border: '1px solid #cbd5e1', borderRadius: 4, background: '#f8fafc',
                      }}>
                        <div><div style={labelStyle}>数据集</div><strong>{selectedResearchSignal.name}</strong></div>
                        <div><div style={labelStyle}>不可变版本</div><strong>v{selectedResearchSignal.version ?? '—'}</strong></div>
                        <div><div style={labelStyle}>信号周期</div><strong>{selectedResearchSignal.timeframe ?? '—'}</strong></div>
                        <div><div style={labelStyle}>信号记录</div><strong>{selectedResearchSignal.recordCount.toLocaleString()} 条</strong></div>
                        <div style={{ gridColumn: '1 / -1', color: researchSignalTimeframeError ? '#b91c1c' : '#047857', fontSize: 11 }}>
                          {researchSignalTimeframeError || `可对齐到当前 ${selectedTimeframe} 回测；运行时将冻结版本和生效时点。`}
                        </div>
                      </div>
                    )}
                    {isL2Quote && (
                      <div style={{ color: '#b45309', fontSize: 11, lineHeight: 1.5 }}>
                        研究信号联动首期仅支持标准 Bar 回放。
                      </div>
                    )}
                  </section>
                  {(loadingStrategySchema || strategySchemaError || strategyParameterFields.length > 0) && (
                    <div className="strategy-parameter-box">
                      {renderStrategyParameterForm()}
                    </div>
                  )}
                  </ConfigSectionCard>
                  <ConfigSectionCard
                    title="资金与成本"
                    summary={`初始资金 ${params.initialCapital}`}
                    sectionRef={(element) => { configSectionRefs.current.capital = element; }}
                  >
                  <label>
                    <div style={labelStyle}>初始资金</div>
                    <input value={params.initialCapital} onChange={(e) => setParam('initialCapital', e.target.value)} style={fieldStyle} />
                  </label>
                  <div>
                    <div style={labelStyle}>默认 A 股费用</div>
                    <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.45 }}>佣金万分之 0.8（单笔最低 5 元）· 过户费万分之 0.1 · 卖出印花税万分之 5</div>
                  </div>
                  <label>
                    <div style={labelStyle}>滑点</div>
                    <input value={params.slippage} onChange={(e) => setParam('slippage', e.target.value)} style={fieldStyle} />
                    <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.45, marginTop: 5 }}>
                      固定比例；0.001 表示 0.1%，买入上调、卖出下调。
                    </div>
                  </label>
                  </ConfigSectionCard>
                  </div>
                  <div className="strategy-config-footer">
                  {status && !error && (
                    <div className="strategy-runtime-message" style={{
                      color: '#0f766e',
                      background: '#ecfdf5',
                      border: '1px solid #99f6e4',
                      borderRadius: 4,
                      padding: '8px 10px',
                      fontSize: 12,
                    }}>
                      {status}
                    </div>
                  )}
                  <div className="strategy-run-actions" style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                    <button className="strategy-run-action" style={{
                      flex: 1,
                      height: 34,
                      borderRadius: 4,
                      border: '1px solid #0f766e',
                      background: '#ccfbf1',
                      color: '#0f766e',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: 6,
                      cursor: runDisabled ? 'not-allowed' : 'pointer',
                      opacity: runDisabled ? 0.7 : 1,
                    }} onClick={handleRunBacktest} disabled={runDisabled} title={versionDirty ? '请先保存策略' : !hasSelectedConfig ? '请先保存策略' : !runPreflightReady ? '当前日期范围数据不完整' : undefined}>
                      <Play size={14} />
                      {running ? '处理中...' : !hasSelectedConfig ? '请选择策略方案' : '运行回测'}
                    </button>
                    <button style={{
                      width: 38,
                      height: 34,
                      borderRadius: 4,
                      border: '1px solid #cbd5e1',
                      background: '#f8fafc',
                      color: '#64748b',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      cursor: 'pointer',
                    }} onClick={handleResetParams} title="恢复当前账号参数">
                      <RotateCcw size={14} />
                    </button>
                  </div>
                  </div>
                </section>
              </div>
              <div className={`strategy-studio-statusbar ${error ? 'is-error' : versionDirty ? 'is-dirty' : hasSelectedConfig ? 'is-ready' : ''}`} aria-live="polite">
                <span>{error ? '操作未完成，请查看代码区上方的错误详情' : statusMessage}</span>
                <span>
                  {runDisabled
                    ? versionDirty
                      ? '保存后可运行回测'
                      : '完成策略配置后可运行回测'
                    : `${selectedSymbol} · ${selectedTimeframe} · 已通过运行前检查`}
                </span>
              </div>
            </section>
            ) : (
            <div className="analysis-main" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div className="analysis-heading" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                <div>
                  <div style={{ color: '#0f172a', fontSize: typography.xl, fontWeight: 700 }}>
                    {activePanelLabel}
                  </div>
                  <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                    {activePanel === 'overview' && '收益概览、基准对比和交易摘要'}
                    {activePanel === 'params' && '当前参数位于左侧，可随策略一起保存'}
                    {activePanel === 'trades' && '单笔信号记录与后续字段筛选'}
                    {activePanel === 'positions' && (isOverlayResult ? '日终底仓恢复、账户权益与做T增厚核对' : '每日持仓、资产构成与收益快照')}
                    {activePanel === 'history' && '回测记录与结果快照追溯'}
                    {activePanel === 'logs' && '回测与运行日志待接入'}
                  </div>
                </div>
              </div>

              {(activePanel === 'trades' || activePanel === 'positions') && (
                <ResultContextBar {...resultContext} />
              )}

              {activePanel === 'trades' && (
                <>
                {result && (result.executionPlans || []).length > 0 && (
                  <details style={{ ...panelStyle, padding: '10px 12px', background: '#ffffff', color: '#475569', fontSize: 12 }}>
                    <summary style={{ color: '#0f766e', cursor: 'pointer', width: 'fit-content' }}>
                      执行追溯（{result.executionPlans?.length} 个母单）
                    </summary>
                    <div style={{ display: 'grid', gap: 6, marginTop: 10 }}>
                      {result.executionPlans?.map((plan) => (
                        <div key={plan.parentOrderId} style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 1.4fr) repeat(4, minmax(80px, 1fr))', gap: 10, alignItems: 'center', borderTop: '1px solid #e2e8f0', paddingTop: 6 }}>
                          <span title={plan.parentOrderId}>{plan.algorithm === 'twap' ? 'TWAP' : '立即成交'} · {formatSignalTime(plan.signalTime)}</span>
                          <span>申请 {plan.requestedQuantity}</span>
                          <span>成交 {plan.filledQuantity}</span>
                          <span>撤销 {plan.cancelledQuantity}</span>
                          <span>{plan.status === 'filled' ? '已完成' : plan.reason || plan.status}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
                <section className="trade-filter-panel" style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                  <div className="trade-filter-layout" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'end', gap: 10 }}>
                    <div className="trade-filter-summary" aria-label="信号执行摘要">
                      <div><span>信号</span><strong>{allSignals.length}</strong></div>
                      <div><span>有效委托</span><strong>{result ? formatNumber(getOrderCount(result.summary)) : '--'}</strong></div>
                      <div><span>已成交</span><strong className="is-filled">{filledSignalCount}</strong></div>
                      <div><span>已跳过</span><strong className="is-skipped">{skippedSignalCount}</strong></div>
                      <div><span>{isOverlayResult ? '周期胜率' : '交易胜率'}</span><strong>{result ? formatPct(isOverlayResult ? result.summary.cycleWinRate : result.summary.winRate) : '--'}</strong></div>
                    </div>
                    <div className="trade-filter-controls">
                      <label style={{ minWidth: 110 }}>
                        <div style={labelStyle}>买卖方向</div>
                        <select value={tradeSideFilter} onChange={(e) => setTradeSideFilter(e.target.value as TradeSideFilter)} style={fieldStyle}>
                          <option value="all">全部</option>
                          <option value="BUY">买入</option>
                          <option value="SELL">卖出</option>
                        </select>
                      </label>
                      <label style={{ minWidth: 110 }}>
                        <div style={labelStyle}>执行状态</div>
                        <select value={tradeStatusFilter} onChange={(e) => setTradeStatusFilter(e.target.value as TradeStatusFilter)} style={fieldStyle}>
                          <option value="all">全部</option>
                          <option value="filled">已成交</option>
                          <option value="skipped">已跳过</option>
                        </select>
                      </label>
                      <label style={{ minWidth: 110 }}>
                        <div style={labelStyle}>盈亏状态</div>
                        <select value={tradePnlFilter} onChange={(e) => setTradePnlFilter(e.target.value as TradePnlFilter)} style={fieldStyle}>
                          <option value="all">全部</option>
                          <option value="win">盈利</option>
                          <option value="loss">亏损</option>
                        </select>
                      </label>
                      <label className="trade-group-toggle" style={{ display: 'flex', alignItems: 'center', gap: 6, height: 34, color: '#475569', fontSize: 12 }}>
                        <input type="checkbox" checked={groupTradesByDay} onChange={(e) => setGroupTradesByDay(e.target.checked)} />
                        按日期分组
                      </label>
                      <button
                        className="trade-export-action"
                        onClick={handleExportTrades}
                        disabled={filteredTrades.length === 0}
                        style={{
                          height: 34,
                          borderRadius: 4,
                          border: '1px solid #0f766e',
                          background: filteredTrades.length > 0 ? '#ccfbf1' : '#f1f5f9',
                          color: filteredTrades.length > 0 ? '#0f766e' : '#94a3b8',
                          cursor: filteredTrades.length > 0 ? 'pointer' : 'not-allowed',
                          padding: '0 10px',
                        }}
                      >
                        导出 {filteredTrades.length} 条 CSV
                      </button>
                    </div>
                  </div>
                  <details className="trade-column-controls" style={{ marginTop: 10, color: '#475569', fontSize: 12 }}>
                    <summary style={{ color: '#0f766e', cursor: 'pointer', width: 'fit-content' }}>选择显示字段（当前 {visibleTradeColumns.length} 项）</summary>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 14px', marginTop: 9 }}>
                      {tradeColumnDefs.map((column) => (
                        <label key={column.key} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                          <input
                            type="checkbox"
                            checked={tradeColumns[column.key]}
                            onChange={(e) => setTradeColumns((prev) => ({ ...prev, [column.key]: e.target.checked }))}
                          />
                          {column.label}
                        </label>
                      ))}
                    </div>
                  </details>
                </section>
                </>
              )}

              {activePanel === 'overview' && (
              <section style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div className="result-metrics-strip" style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(128px, 1fr))', gap: 8, overflowX: 'auto', paddingBottom: 2 }}>
                  {coreMetrics.map(([name, value]) => (
                    <div className="result-metric-card" key={name} style={{ ...panelStyle, minWidth: 128, padding: '12px 14px', background: 'var(--surface)' }}>
                      <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{name}</div>
                      <div style={{ color: 'var(--text)', fontSize: 21, fontWeight: 700, marginTop: 8, fontVariantNumeric: 'tabular-nums' }}>
                        {value}
                      </div>
                    </div>
                  ))}
                </div>

              </section>
              )}

              {activePanel === 'overview' && (
              <section className="analytics-card" style={{ ...panelStyle, padding: 16, background: '#ffffff' }}>
                <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 16 }}>
                  <div>
                    <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>完整指标</div>
                    <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>按收益、风险和交易质量查看回测结果的构成。</div>
                  </div>
                  <div style={{ color: '#0f766e', fontSize: 12, fontWeight: 600 }}>分析视图</div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(220px, 1fr))', gap: 12 }}>
                  {metricGroups.map((group) => {
                    const descriptions: Record<string, string> = {
                      '收益': '策略回报，以及相对买入持有基准的表现。',
                      '风险': '净值下行幅度和风险调整后的收益能力。',
                      '交易质量': '信号胜负、盈亏结构与交易频率。',
                    };
                    return (
                      <div className="metric-group-card" key={group.title} style={{ border: '1px solid #d7dde8', borderRadius: 6, overflow: 'hidden' }}>
                        <div style={{ padding: '12px 14px', background: '#f8fafc', borderBottom: '1px solid #d7dde8' }}>
                          <div style={{ color: '#0f172a', fontSize: 14, fontWeight: 700 }}>{group.title}</div>
                          <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.5, marginTop: 4 }}>{descriptions[group.title]}</div>
                        </div>
                        <div style={{ padding: '4px 14px' }}>
                          {group.rows.map(([name, value]) => (
                            <div key={name} style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, padding: '10px 0', borderBottom: '1px solid #eef2f7' }}>
                              <span style={{ color: '#64748b', fontSize: 12 }}>{name}</span>
                              <span style={{ color: '#0f172a', fontSize: 14, fontWeight: 700, textAlign: 'right' }}>{value}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div style={{ marginTop: 12, padding: '10px 12px', borderRadius: 5, background: '#f8fafc', color: '#64748b', fontSize: 12, lineHeight: 1.65 }}>
                  指标基于本次回测的每日净值与信号记录计算；手续费、滑点和持仓规则已纳入收益结果。
                </div>
              </section>
              )}

              {activePanel === 'overview' && (
              <section className="equity-chart-card" style={{ ...panelStyle, minHeight: 360, padding: 16, background: '#ffffff' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                  <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>净值曲线</div>
                  {result && (
                    <div style={{ color: '#64748b', fontSize: 12 }}>
                      策略 vs 买入持有 {result.benchmarkSymbol || selectedBenchmarkSymbol}
                    </div>
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginTop: 12, flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {[
                      ['1m', '1个月'],
                      ['1y', '1年'],
                      ['all', '全部'],
                    ].map(([value, label]) => (
                      <button
                        key={value}
                        onClick={() => setChartRange(value as ChartRange)}
                        style={{
                          height: 28,
                          borderRadius: 4,
                          border: `1px solid ${chartRange === value ? '#0f766e' : '#cbd5e1'}`,
                          background: chartRange === value ? '#ccfbf1' : '#f8fafc',
                          color: chartRange === value ? '#0f766e' : '#64748b',
                          cursor: 'pointer',
                        }}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div style={{ display: 'flex', gap: 12, color: '#64748b', fontSize: 12, flexWrap: 'wrap' }}>
                    <label><input type="checkbox" checked={showStrategyCurve} onChange={(e) => setShowStrategyCurve(e.target.checked)} /> 策略净值</label>
                    <label><input type="checkbox" checked={showBenchmarkCurve} onChange={(e) => setShowBenchmarkCurve(e.target.checked)} /> 买入持有基准</label>
                    <label><input type="checkbox" checked={showExcessCurve} onChange={(e) => setShowExcessCurve(e.target.checked)} /> 超额收益</label>
                    <label><input type="checkbox" checked={useLogAxis} onChange={(e) => setUseLogAxis(e.target.checked)} /> 对数轴</label>
                  </div>
                </div>
                <div style={{ height: 300, marginTop: 14, border: '1px solid #d7dde8', borderRadius: 6, display: 'flex', alignItems: 'stretch', color: '#64748b', fontSize: 13, overflow: 'hidden', background: '#f8fafc' }}>
                  {rangedEquity.length > 0 ? (
                    <div style={{ flex: 1, padding: '12px 14px 10px', minWidth: 0 }}>
                      {(() => {
                        const chart = buildNetValueChart(rangedEquity, rangedBenchmark, {
                          showStrategy: showStrategyCurve,
                          showBenchmark: showBenchmarkCurve,
                          showExcess: showExcessCurve,
                          useLogAxis,
                        });
                        const renderedSeries = chart.chartSeries;
                        return (
                          <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
                            <svg viewBox={`0 0 ${chart.width} ${chart.height}`} preserveAspectRatio="none" style={{ width: '100%', height: 238, display: 'block' }} role="img" aria-label="净值曲线">
                              {[0, 0.5, 1].map((ratio) => {
                                const y = chart.padding.top + chart.chartHeight * ratio;
                                return (
                                  <line key={ratio} x1={chart.padding.left} y1={y} x2={chart.width - chart.padding.right} y2={y} stroke="#d7dde8" strokeWidth="1" strokeDasharray="5 7" opacity={0.9} />
                                );
                              })}
                              {renderedSeries.map((series) => (
                                <polyline
                                  key={series.name}
                                  points={toPolyline(series.points)}
                                  fill="none"
                                  stroke={series.color}
                                  strokeWidth={series.name === '策略' ? '3' : '2.5'}
                                  strokeLinejoin="round"
                                  strokeLinecap="round"
                                  strokeDasharray={series.dashed ? '6 5' : undefined}
                                  vectorEffect="non-scaling-stroke"
                                  opacity={series.points.length > 0 ? 1 : 0}
                                />
                              ))}
                              {renderedSeries.map((series) => {
                                const point = getLastPoint(series.points);
                                if (!point) return null;
                                return (
                                  <circle key={`${series.name}-last`} cx={point.x} cy={point.y} r="4.5" fill={series.color} stroke="#f8fafc" strokeWidth="2" vectorEffect="non-scaling-stroke">
                                    <title>{`${series.name} ${point.date === '__START__' ? '起点' : point.date} 净值 ${point.netValue.toFixed(4)}`}</title>
                                  </circle>
                                );
                              })}
                            </svg>
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, color: '#64748b', fontSize: 12, lineHeight: 1.5 }}>
                              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                                {renderedSeries.map((series) => {
                                  const point = getLastPoint(series.points.filter((item) => item.date !== '__START__'));
                                  return (
                                    <span key={series.name} style={{ color: '#334155' }}>
                                      <span style={{ color: series.color, marginRight: 6 }}>●</span>
                                      {series.name} {point ? point.netValue.toFixed(4) : '--'}
                                    </span>
                                  );
                                })}
                                <span>超额 {result ? formatPct(result.excessReturn) : '--'}</span>
                              </div>
                              <span>{chart.actualDates.length > 0 ? `${chart.actualDates[0]} - ${chart.actualDates[chart.actualDates.length - 1]}` : '暂无日期'}</span>
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                  ) : (
                    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                      等待运行回测
                    </div>
                  )}
                </div>
                {dailyBars.length > 0 && (
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                    {([
                      ['每日收益', 'pnl' as const, '#dc2626'],
                      ['超额收益', 'excess' as const, '#dc2626'],
                    ] satisfies Array<[string, 'pnl' | 'excess', string]>).map(([title, key, color]) => {
                      const values = dailyBars.map((bar) => Math.abs(Number(bar[key])));
                      const max = Math.max(...values, 1);
                      return (
                        <div key={title} style={{ border: '1px solid #d7dde8', borderRadius: 6, background: '#f8fafc', padding: 10 }}>
                          <div style={{ color: '#64748b', fontSize: 12, marginBottom: 8 }}>{title}</div>
                          <div style={{ height: 88, display: 'flex', alignItems: 'center', gap: 3 }}>
                            {dailyBars.map((bar) => {
                              const value = Number(bar[key]);
                              const height = Math.max(Math.abs(value) / max * 42, 2);
                              return (
                                <div key={`${title}-${bar.date}`} title={`${bar.date} ${value.toFixed(2)}`} style={{ flex: 1, minWidth: 3, height: 84, display: 'flex', alignItems: value > 0 ? 'flex-end' : value < 0 ? 'flex-start' : 'center' }}>
                                  <div style={{ width: '100%', height, background: value > 0 ? color : value < 0 ? '#16a34a' : '#94a3b8', borderRadius: 2 }} />
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
                {resultManifest && (
                  <div style={{ marginTop: 10, padding: '9px 10px', borderRadius: 5, background: '#f1f5f9', color: '#475569', fontSize: 12, lineHeight: 1.65 }}>
                    <div>
                      回测口径：Bar 收盘信号虚拟成交 · {resultManifest.holdingPolicy === 'overnight' ? '允许跨日' : resultManifest.holdingPolicy === 'intraday' ? '日内平仓' : '期末规则未记录'} · {resultCostModel ? `手续费 ${(resultCostModel.commissionRate * 10_000).toFixed(2)} bp · 滑点 ${(resultCostModel.slippage * 10_000).toFixed(2)} bp` : '成本模型未记录'}
                    </div>
                    <div>
                      实验指纹：{resultManifest.experimentHash?.slice(0, 12) || '未记录'} · {resultManifest.barCount} 根 {resultManifest.barRange?.timeframe || selectedTimeframe} K 线
                    </div>
                  </div>
                )}
                {result && result.warnings.length > 0 && (
                  <div style={{ color: '#b45309', fontSize: 12, marginTop: 10 }}>
                    {result.warnings.join('；')}
                  </div>
                )}
              </section>
              )}

              {(activePanel === 'overview' || activePanel === 'trades') && (
              <section className="trade-ledger-card" style={{ ...panelStyle, padding: 16, background: '#ffffff' }}>
                <div className="trade-ledger-heading">
                  <div>
                    <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>信号执行账本</div>
                    <div>按信号时间追溯策略动作、成交结果与单笔收益。</div>
                  </div>
                  {activePanel === 'trades' && <span>当前筛选 {filteredTrades.length} 条</span>}
                </div>
                <div className="trade-ledger-toolbar" style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  marginBottom: 10,
                  flexWrap: 'wrap',
                }}>
                  <div style={{ color: '#64748b', fontSize: 12 }}>
                    展示 {tradePageStart}-{tradePageEnd} / {filteredTrades.length} 条筛选信号
                    {result ? `，其中已成交 ${result.trades.length} 笔` : ''}
                  </div>
                  <div className="trade-pagination" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ color: '#64748b', fontSize: 12 }}>每页</span>
                    <select
                      value={tradePageSize}
                      onChange={(e) => setTradePageSize(Number(e.target.value))}
                      style={{ ...fieldStyle, width: 82, height: 30, padding: '4px 8px' }}
                    >
                      <option value={50}>50</option>
                      <option value={100}>100</option>
                      <option value={200}>200</option>
                    </select>
                    <button
                      onClick={() => setTradePage((page) => Math.max(1, page - 1))}
                      disabled={safeTradePage <= 1}
                      style={{
                        height: 30,
                        padding: '0 10px',
                        border: '1px solid #cbd5e1',
                        borderRadius: 4,
                        background: safeTradePage <= 1 ? '#f1f5f9' : '#ffffff',
                        color: safeTradePage <= 1 ? '#94a3b8' : '#334155',
                        cursor: safeTradePage <= 1 ? 'not-allowed' : 'pointer',
                      }}
                    >
                      上一页
                    </button>
                    <span style={{ color: '#64748b', fontSize: 12, minWidth: 54, textAlign: 'center' }}>
                      {safeTradePage} / {tradePageCount}
                    </span>
                    <button
                      onClick={() => setTradePage((page) => Math.min(tradePageCount, page + 1))}
                      disabled={safeTradePage >= tradePageCount}
                      style={{
                        height: 30,
                        padding: '0 10px',
                        border: '1px solid #cbd5e1',
                        borderRadius: 4,
                        background: safeTradePage >= tradePageCount ? '#f1f5f9' : '#ffffff',
                        color: safeTradePage >= tradePageCount ? '#94a3b8' : '#334155',
                        cursor: safeTradePage >= tradePageCount ? 'not-allowed' : 'pointer',
                      }}
                    >
                      下一页
                    </button>
                  </div>
                </div>
                <div className="trade-ledger-table-head" style={{ display: 'grid', gridTemplateColumns: `${tradeGridColumns} 68px`, gap: 8, color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8', paddingBottom: 8 }}>
                  {visibleTradeColumns.map((column) => (
                    <span key={column.key} style={{ textAlign: column.align || 'left' }}>
                      {column.label}
                    </span>
                  ))}
                  <span>复盘</span>
                </div>
                {result && filteredTrades.length > 0 && visibleTradeColumns.length > 0 ? (
                  pagedTrades.map((trade, index) => {
                    const currentDate = (trade.time || '').slice(0, 8) || '未知日期';
                    const previousTrade = pagedTrades[index - 1];
                    const previousDate = previousTrade ? ((previousTrade.time || '').slice(0, 8) || '未知日期') : '';
                    const showGroupTitle = groupTradesByDay && currentDate !== previousDate;
                    const dayTrades = showGroupTitle
                      ? pagedTrades.filter((item) => ((item.time || '').slice(0, 8) || '未知日期') === currentDate)
                      : [];
                    const dayFilledCount = dayTrades.filter((item) => item.status !== 'skipped').length;
                    const dayPnl = dayTrades.reduce((total, item) => total + item.pnl, 0);
                    return (
                      <div key={`${trade.time}-${trade.direction}-${index}`}>
                        {showGroupTitle && (
                          <div className="trade-day-group" style={{ color: '#0f766e', fontSize: 12, fontWeight: 700, padding: '10px 0 4px', borderBottom: '1px solid #e2e8f0' }}>
                            <span>{currentDate}</span>
                            <span>{dayTrades.length} 条信号 · 成交 {dayFilledCount} 笔 · 当日盈亏 {dayPnl.toFixed(2)}</span>
                          </div>
                        )}
                        <div className={`trade-ledger-row ${trade.status === 'skipped' ? 'is-skipped' : 'is-filled'}`} style={{ display: 'grid', gridTemplateColumns: `${tradeGridColumns} 68px`, gap: 8, color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0', padding: '8px 0', alignItems: 'center' }}>
                          {visibleTradeColumns.map((column) => (
                            <span key={column.key} className={column.key === 'price' ? 'trade-price-cell' : undefined} style={{
                              textAlign: column.align || 'left',
                              overflow: column.key === 'price' ? 'visible' : 'hidden',
                              textOverflow: column.key === 'price' ? 'clip' : 'ellipsis',
                              whiteSpace: column.key === 'note' || column.key === 'price' ? 'normal' : 'nowrap',
                              lineHeight: column.key === 'note' || column.key === 'price' ? 1.5 : undefined,
                            }}>
                              {column.render(trade)}
                            </span>
                          ))}
                          <button
                            type="button"
                            disabled={!result?.runId || trade.status === 'skipped'}
                            onClick={() => handleViewTradeChart(trade)}
                            style={{
                              border: 'none',
                              background: 'transparent',
                              color: result?.runId && trade.status !== 'skipped' ? '#2962ff' : '#94a3b8',
                              cursor: result?.runId && trade.status !== 'skipped' ? 'pointer' : 'not-allowed',
                              padding: 0,
                              textAlign: 'left',
                              fontSize: 12,
                            }}
                          >
                            查看 K 线
                          </button>
                        </div>
                      </div>
                    );
                  })
                ) : result && visibleTradeColumns.length === 0 ? (
                  <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
                    至少选择一个显示字段
                  </div>
                ) : result ? (
                  <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
                    当前筛选条件下没有交易记录
                  </div>
                ) : (
                  <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
                    暂无回测结果
                  </div>
                )}
                <div style={{ color: '#64748b', fontSize: 12, paddingTop: 10 }}>
                  导出 CSV 会包含当前筛选后的全部交易。
                </div>
              </section>
              )}

              {activePanel === 'overview' && isOverlayResult && result && result.resultSchemaVersion !== 3 && (
                <div style={{ padding: '10px 12px', border: '1px solid #f59e0b', borderRadius: 6, background: '#fffbeb', color: '#92400e', fontSize: 12 }}>
                  该任务使用历史收益口径，未重算增强周期；主动重新运行后才使用结果口径 v3。
                </div>
              )}

              {activePanel === 'overview' && isOverlayResult && result?.resultSchemaVersion === 3 && (
                <section style={{ ...panelStyle, padding: 16, background: '#ffffff' }}>
                  <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>增强周期</div>
                  <div style={{ color: '#64748b', fontSize: 12, marginTop: 4, marginBottom: 12 }}>
                    增强仓从 0 离开并再次回到 0 才算一个完整周期；未完成周期不进入胜率。
                  </div>
                  {(result.cycles || []).length > 0 ? (
                    <div style={{ display: 'grid', gap: 8 }}>
                      {(result.cycles || []).map((cycle, cycleIndex) => (
                        <details key={cycle.cycleId} style={{ border: '1px solid #d7dde8', borderRadius: 6, padding: '9px 12px', background: cycle.status === 'open' ? '#fffbeb' : '#f8fafc' }}>
                          <summary style={{ cursor: 'pointer', color: '#334155', fontSize: 12, fontWeight: 700 }}>
                            增强周期 {cycleIndex + 1} · {cycle.direction === 'long' ? '正增强' : '负增强'} · {cycle.status === 'completed' ? '已完成' : '未完成'} · 当前收益 {formatMoney(cycle.netPnl)}
                          </summary>
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(120px, 1fr))', gap: 8, marginTop: 10, color: '#475569', fontSize: 12 }}>
                            <span>开始：{formatSignalTime(cycle.openedAt)}</span>
                            <span>完成：{cycle.closedAt ? formatSignalTime(cycle.closedAt) : '--'}</span>
                            <span>已恢复：{cycle.closedQuantity}</span>
                            <span>剩余：{cycle.remainingQuantity}</span>
                            <span>已实现：{formatMoney(cycle.realizedPnl)}</span>
                            <span>未实现：{formatMoney(cycle.unrealizedPnl)}</span>
                          </div>
                          <div style={{ overflow: 'auto', marginTop: 10, border: '1px solid #e2e8f0', borderRadius: 4, background: '#ffffff' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(150px, 1.25fr) 70px 90px 100px minmax(110px, 1fr) 120px', minWidth: 720, padding: '7px 10px', color: '#64748b', fontSize: 11, fontWeight: 700, background: '#f8fafc' }}>
                              <span>成交时间</span>
                              <span>方向</span>
                              <span>数量</span>
                              <span>成交价</span>
                              <span>周期作用</span>
                              <span>实现增强收益</span>
                            </div>
                            {cycle.fillIds.map((fillId) => {
                              const trade = tradeByFillId.get(fillId);
                              if (!trade) {
                                return (
                                  <div key={fillId} style={{ padding: '8px 10px', borderTop: '1px solid #e2e8f0', color: '#b45309', fontSize: 11 }}>
                                    成交记录关联失败：{fillId}
                                  </div>
                                );
                              }
                              const display = getOverlayCycleTradeDisplay(trade, cycle.cycleId);
                              return (
                                <div key={fillId} style={{ display: 'grid', gridTemplateColumns: 'minmax(150px, 1.25fr) 70px 90px 100px minmax(110px, 1fr) 120px', minWidth: 720, padding: '8px 10px', borderTop: '1px solid #e2e8f0', color: '#334155', fontSize: 11 }}>
                                  <span>{formatSignalTime(trade.executionTime || trade.time)}</span>
                                  <span>{trade.side === 'BUY' ? '买入' : trade.side === 'SELL' ? '卖出' : trade.side}</span>
                                  <span>{formatNumber(display.quantity)}</span>
                                  <span>{formatNumber(trade.price)}</span>
                                  <span>{display.effectLabel}</span>
                                  <span style={{ color: display.realizedPnl === null ? '#94a3b8' : getPnlColor(display.realizedPnl) }}>
                                    {display.realizedPnl === null ? '--' : formatMoney(display.realizedPnl)}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                        </details>
                      ))}
                    </div>
                  ) : (
                    <div style={{ color: '#64748b', fontSize: 12 }}>本次回测没有增强周期。</div>
                  )}
                </section>
              )}

              {activePanel === 'history' && (
                <section className="history-ledger-card" style={{ ...panelStyle, padding: 16, background: '#ffffff', minHeight: 320 }}>
                  <div className="history-ledger-heading" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                    <div>
                      <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>
                        历史回测
                      </div>
                      <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                        每次回测会保存代码、参数与行情数据快照。
                      </div>
                    </div>
                    <button
                      onClick={() => void refreshBacktestRuns()}
                      disabled={historyLoading}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #cbd5e1',
                        background: '#f8fafc',
                        color: '#334155',
                        cursor: historyLoading ? 'wait' : 'pointer',
                        padding: '0 12px',
                      }}
                    >
                      {historyLoading ? '刷新中...' : '刷新'}
                    </button>
                  </div>
                  <div className="history-index-controls" style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
                    <input
                      value={historyQuery}
                      onChange={(e) => {
                        setHistoryQuery(e.target.value);
                        setHistoryPage(1);
                      }}
                      placeholder="搜索实验、策略、标签或方案"
                      style={{ ...fieldStyle, flex: 1 }}
                    />
                    <select
                      value={historySymbolFilter}
                      onChange={(e) => {
                        setHistorySymbolFilter(e.target.value);
                        setHistoryPage(1);
                      }}
                      style={{ ...fieldStyle, width: 160 }}
                    >
                      <option value="">全部标的</option>
                      {historySymbols.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}
                    </select>
                    <button
                      type="button"
                      className={`history-favorites-filter${historyFavoritesOnly ? ' is-active' : ''}`}
                      aria-pressed={historyFavoritesOnly}
                      onClick={() => {
                        setHistoryFavoritesOnly((current) => !current);
                        setHistoryPage(1);
                      }}
                    >
                      <Star size={15} fill={historyFavoritesOnly ? 'currentColor' : 'none'} />
                      只看收藏
                    </button>
                  </div>
                  {historyLoadError && (
                    <div style={{ color: '#b91c1c', fontSize: 12, marginBottom: 12 }}>
                      {historyLoadError}
                    </div>
                  )}
                  {selectedDeletableHistoryRunIds.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 14, padding: '8px 10px', border: '1px solid #fecaca', borderRadius: 4, background: '#fff1f2', color: '#9f1239', fontSize: 12 }}>
                      <span>已选 {selectedDeletableHistoryRunIds.length} 条</span>
                      <span style={{ display: 'flex', gap: 8 }}>
                        <button onClick={() => setSelectedHistoryRunIds(new Set())} style={{ height: 28, borderRadius: 4, border: '1px solid #cbd5e1', background: '#ffffff', color: '#334155', cursor: 'pointer' }}>取消选择</button>
                        <button onClick={() => void handleDeleteSelectedHistoryRuns()} style={{ height: 28, borderRadius: 4, border: '1px solid #fecaca', background: '#be123c', color: '#ffffff', cursor: 'pointer' }}>删除所选</button>
                      </span>
                    </div>
                  )}
                  <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 16 }}>
                    <div className="history-experiment-table" style={{ minWidth: 0 }}>
                      <div className="history-experiment-head" style={{ display: 'grid', gridTemplateColumns: historyGridColumns, gap: 8, color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8', padding: '0 10px 8px' }}>
                        <input type="checkbox" checked={allDeletableHistoryRunsSelected} disabled={deletableFilteredHistoryRuns.length === 0} onChange={(event) => toggleAllHistoryRunSelections(event.target.checked)} aria-label="选择当前页中的全部可删除回测" />
                        <span>收藏</span>
                        <span>编号</span>
                        <span>策略</span>
                        <span>标的/基准</span>
                        <span>区间</span>
                        <span>收益</span>
                        <span>回撤</span>
                        <span>夏普比率</span>
                        <span>完成时间</span>
                        <span>操作</span>
                      </div>
                      {filteredHistoryRuns.length > 0 ? (
                        pagedHistoryRuns.map((run) => {
                          const isSelected = run.id === selectedHistoryRunId;
                          const deletable = run.status !== 'queued' && run.status !== 'running' && run.status !== 'cancelling';
                          return (
                            <div className={`history-run-row${isSelected ? ' is-selected' : ''}`} key={run.id} onClick={() => setSelectedHistoryRunId(run.id)} onDoubleClick={() => handleViewHistoryResult(run)} style={{ display: 'grid', gridTemplateColumns: historyGridColumns, gap: 8, alignItems: 'center', color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0', padding: '9px 10px', background: isSelected ? '#ecfdf5' : 'transparent', cursor: 'pointer' }}>
                              <input type="checkbox" checked={selectedHistoryRunIds.has(run.id)} disabled={!deletable} onClick={(event) => event.stopPropagation()} onChange={(event) => toggleHistoryRunSelection(run.id, event.target.checked)} aria-label={`选择历史回测 ${formatRunReference(run.displayNo)}`} />
                              <button
                                type="button"
                                className={`history-favorite-button${run.isFavorited ? ' is-active' : ''}`}
                                aria-label={`${run.isFavorited ? '取消收藏' : '收藏'}历史回测 ${formatRunReference(run.displayNo)}`}
                                disabled={favoritingHistoryRunIds.has(run.id)}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  void handleSetHistoryFavorite(run);
                                }}
                              >
                                <Star size={16} fill={run.isFavorited ? 'currentColor' : 'none'} />
                              </button>
                              <button onClick={() => setSelectedHistoryRunId(run.id)} style={{ textAlign: 'left', border: 'none', background: 'transparent', padding: 0, color: '#0f172a', cursor: 'pointer', fontWeight: isSelected ? 700 : 500 }}>
                                {formatRunReference(run.displayNo)}
                              </button>
                              <span style={{ minWidth: 0 }}>
                                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {run.configId ? (configNameById.get(run.configId) || '已保存方案') : '未绑定方案'}
                                </div>
                                <div style={{ color: '#64748b', marginTop: 2 }}>
                                  {getStrategyCategoryLabel(run.strategyCategory)} · {run.configId ? '已保存策略快照' : '未关联策略'}
                                </div>
                              </span>
                              <span>
                                <div>{run.symbol} · {run.timeframe || '5m'} K 线</div>
                                <div style={{ color: '#64748b', marginTop: 2 }}>{run.benchmarkSymbol || '-'}</div>
                              </span>
                              <span>{run.startDate}-{run.endDate}</span>
                              <span style={{ color: getPnlColor(run.summaryJson.totalReturn ?? 0), fontWeight: 700 }}>
                                {formatOptionalPct(run.summaryJson.totalReturn)}
                              </span>
                              <span>{formatOptionalPct(run.summaryJson.maxDrawdownRate)}</span>
                              <span>{formatOptionalNumber(run.summaryJson.annualSharpe)}</span>
                              <span>
                                <div>{formatHistoryParamValue(run.status)}</div>
                                <div style={{ color: '#64748b', marginTop: 2 }}>{run.finishedAt || run.createdAt}</div>
                                {run.tags.length > 0 && (
                                  <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 4 }}>
                                    {run.tags.slice(0, 3).map((tag) => (
                                      <span
                                        key={tag}
                                        style={{
                                          border: '1px solid #cbd5e1',
                                          borderRadius: 999,
                                          padding: '0 6px',
                                          fontSize: 10,
                                          color: '#334155',
                                          background: '#f8fafc',
                                        }}
                                      >
                                        {tag}
                                      </span>
                                    ))}
                                  </div>
                                )}
                              </span>
                              <span style={{ display: 'flex', gap: 4 }}>
                                <button
                                  onClick={() => handleApplyBacktestRun(run)}
                                  style={{
                                    height: 28,
                                    borderRadius: 4,
                                    border: '1px solid #cbd5e1',
                                    background: '#f8fafc',
                                    color: '#334155',
                                    cursor: 'pointer',
                                  }}
                                >
                                  复制
                                </button>
                                <button
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void handleDeleteBacktestRun(run);
                                  }}
                                  disabled={run.status === 'queued' || run.status === 'running' || run.status === 'cancelling'}
                                  style={{
                                    height: 28,
                                    borderRadius: 4,
                                    border: '1px solid #fecaca',
                                    background: '#fff1f2',
                                    color: '#be123c',
                                    cursor: run.status === 'queued' || run.status === 'running' || run.status === 'cancelling' ? 'not-allowed' : 'pointer',
                                    opacity: run.status === 'queued' || run.status === 'running' || run.status === 'cancelling' ? 0.55 : 1,
                                  }}
                                >
                                  删除
                                </button>
                              </span>
                            </div>
                          );
                        })
                      ) : (
                        <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
                          {historyLoading ? '正在加载历史实验...' : '没有符合当前筛选条件的历史实验。'}
                        </div>
                      )}
                      {filteredHistoryRuns.length > 0 && (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '12px 10px 0', color: '#64748b', fontSize: 12 }}>
                          <span>显示 {historyRangeStart}-{historyRangeEnd} / {historyTotal} 条实验</span>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <select value={historyPageSize} onChange={(e) => { setHistoryPageSize(Number(e.target.value)); setHistoryPage(1); }} style={{ ...fieldStyle, width: 82, height: 30 }}>
                              <option value={10}>10 条/页</option>
                              <option value={20}>20 条/页</option>
                              <option value={50}>50 条/页</option>
                            </select>
                            <button onClick={() => setHistoryPage((page) => Math.max(1, page - 1))} disabled={safeHistoryPage <= 1} style={{ height: 30, borderRadius: 4, border: '1px solid #cbd5e1', background: '#ffffff', color: '#334155', cursor: safeHistoryPage <= 1 ? 'not-allowed' : 'pointer', opacity: safeHistoryPage <= 1 ? 0.45 : 1 }}>上一页</button>
                            <span>{safeHistoryPage} / {historyPageCount}</span>
                            <button onClick={() => setHistoryPage((page) => Math.min(historyPageCount, page + 1))} disabled={safeHistoryPage >= historyPageCount} style={{ height: 30, borderRadius: 4, border: '1px solid #cbd5e1', background: '#ffffff', color: '#334155', cursor: safeHistoryPage >= historyPageCount ? 'not-allowed' : 'pointer', opacity: safeHistoryPage >= historyPageCount ? 0.45 : 1 }}>下一页</button>
                          </div>
                        </div>
                      )}
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
                      <div className="history-summary-card history-detail-card" style={{ ...panelStyle, padding: 14, background: '#ffffff' }}>
                      <div className="history-summary-hero">
                        <div>
                          <div className="history-summary-title">
                            {selectedHistoryRun ? `回测 ${formatRunReference(selectedHistoryRun.displayNo)}` : '实验摘要'}
                            {selectedHistoryRun && <span className={`history-status-badge is-${selectedHistoryRun.status}`}>{formatHistoryParamValue(selectedHistoryRun.status)}</span>}
                            {selectedHistoryRun && <span className="history-category-badge">{getStrategyCategoryLabel(selectedHistoryCategory)}</span>}
                          </div>
                          <div className="history-summary-subtitle">
                            {selectedHistoryRun
                              ? `${selectedHistoryConfigName} · ${selectedHistoryRun.symbol} · ${selectedHistoryRun.timeframe || '5m'} K线 · ${selectedHistoryRun.startDate} 至 ${selectedHistoryRun.endDate}`
                              : '选择一条历史记录，查看实验结论和关键设置。'}
                          </div>
                        </div>
                      </div>
                        {selectedHistoryRun ? (
                          <div className="history-summary-content">
                            {getRunErrorDetail(selectedHistoryRun) && <BacktestErrorCard detail={getRunErrorDetail(selectedHistoryRun)!} />}
                            <div className="history-result-metrics">
                              {selectedHistoryMetrics.map(([label, value]) => (
                                <div key={label}>
                                  <span>{label}</span>
                                  <strong>{value}</strong>
                                </div>
                              ))}
                            </div>
                            <HistoryKeySettings run={selectedHistoryRun} configName={selectedHistoryConfigName} />
                            {!selectedHistoryRun.resultJson?.experimentManifest?.experimentHash && (
                              <div className="history-repro-warning">该记录的实验清单不完整，无法完整复现。</div>
                            )}
                            <section className="history-research-note">
                              <div className="history-section-title">研究备注与标签</div>
                              <div className="history-note-grid">
                                <label>
                                  <span>备注</span>
                                  <textarea
                                    value={historyNoteDraft}
                                    onChange={(e) => setHistoryNoteDraft(e.target.value)}
                                    rows={3}
                                    placeholder="记录本次实验的结论、异常与后续方向"
                                  />
                                </label>
                                <label>
                                  <span>标签</span>
                                  <input
                                    value={historyTagsDraft}
                                    onChange={(e) => setHistoryTagsDraft(e.target.value)}
                                    placeholder="候选, 调参, 保留"
                                  />
                                  <small>使用逗号分隔多个标签</small>
                                </label>
                              </div>
                            </section>
                            <div className="history-action-bar">
                              <button className="is-primary" onClick={() => handleViewHistoryResult(selectedHistoryRun)}>查看完整结果</button>
                              <button onClick={() => {
                                handleApplyBacktestRun(selectedHistoryRun);
                                setActivePanel('overview');
                              }}>复制参数新建</button>
                              <button onClick={() => handleRerunHistory(selectedHistoryRun)} disabled={running}>
                                {running ? '运行中...' : '重新运行'}
                              </button>
                              <button
                                type="button"
                                onClick={() => openPublicationDialog(selectedHistoryRun)}
                                disabled={!selectedHistoryRun.publicationId && Boolean(getPublicationEligibilityError(selectedHistoryRun))}
                                title={selectedHistoryRun.publicationId
                                  ? '查看已经上架的共享回测'
                                  : getPublicationEligibilityError(selectedHistoryRun) || '将这次成功回测共享给内部用户'}
                              >
                                <Share2 size={14} />
                                {selectedHistoryRun.publicationId ? '查看共享' : '共享到策略广场'}
                              </button>
                              <button onClick={handleSaveHistoryMeta}>保存备注</button>
                            </div>
                            <HistoryFullParameters run={selectedHistoryRun} />
                            <HistoryTechnicalDetails run={selectedHistoryRun} />
                          </div>
                        ) : (
                          <div style={{ color: '#64748b', fontSize: 12 }}>选择一条历史记录查看详情。</div>
                        )}
                      </div>

                      <details className="history-compare-card history-disclosure">
                        <summary>与另一条回测对比</summary>
                        <div className="history-compare-content">
                        {historyTotal > 1 ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                            <label>
                              <div style={labelStyle}>对比对象</div>
                              <input
                                value={historyCompareQuery}
                                onChange={(event) => setHistoryCompareQuery(event.target.value)}
                                placeholder="搜索对比回测"
                                style={{ ...fieldStyle, marginBottom: 8 }}
                              />
                              <select
                                value={compareHistoryRunId}
                                onChange={(e) => setCompareHistoryRunId(e.target.value)}
                                style={fieldStyle}
                              >
                                {compareHistoryOptions
                                  .filter((item) => item.id !== selectedHistoryRunId)
                                  .map((item) => (
                                    <option key={item.id} value={item.id}>
                                      回测 {formatRunReference(item.displayNo)} · {item.symbol} · {item.startDate}-{item.endDate}
                                    </option>
                                  ))}
                              </select>
                            </label>
                            {selectedHistoryRun && compareHistoryRun && selectedHistoryRun.strategyCategory === compareHistoryRun.strategyCategory && (
                              <div className={`history-comparison-scope ${historyComparisonScope.every((item) => item.startsWith('同')) ? 'is-comparable' : 'is-mixed'}`}>
                                {historyComparisonScope.join(' · ')}
                              </div>
                            )}
                            {selectedHistoryRun && compareHistoryRun && selectedHistoryRun.strategyCategory !== compareHistoryRun.strategyCategory && (
                              <div style={{ color: '#b45309', fontSize: 12, lineHeight: 1.5 }}>
                                两条回测属于不同策略类别，收益口径不可直接比较；仍可查看参数差异。
                              </div>
                            )}
                            {selectedHistoryRun && compareHistoryRun && (
                              <div style={{ overflow: 'auto', border: '1px solid #e2e8f0', borderRadius: 6 }}>
                                <div style={{ display: 'grid', gridTemplateColumns: '130px 1fr 1fr 1fr', gap: 8, padding: '8px 10px', background: '#f8fafc', color: '#64748b', fontSize: 11, fontWeight: 700 }}>
                                  <span>结果指标</span>
                                  <span title={selectedHistoryRun.id}>回测 {formatRunReference(selectedHistoryRun.displayNo)}</span>
                                  <span title={compareHistoryRun.id}>回测 {formatRunReference(compareHistoryRun.displayNo)}</span>
                                  <span>差异</span>
                                </div>
                                {[
                                  ['总收益率', selectedHistoryRun.summaryJson.totalReturn, compareHistoryRun.summaryJson.totalReturn, formatOptionalPct],
                                  ['最大回撤', selectedHistoryRun.summaryJson.maxDrawdownRate, compareHistoryRun.summaryJson.maxDrawdownRate, formatOptionalPct],
                                  ['夏普比率', selectedHistoryRun.summaryJson.annualSharpe, compareHistoryRun.summaryJson.annualSharpe, formatOptionalNumber],
                                  ['有效委托数', getOrderCount(selectedHistoryRun.summaryJson), getOrderCount(compareHistoryRun.summaryJson), formatOptionalNumber],
                                  ['成交笔数', getFillCount(selectedHistoryRun.summaryJson), getFillCount(compareHistoryRun.summaryJson), formatOptionalNumber],
                                ].map(([label, current, compared, formatter]) => {
                                  const difference = Number(current ?? 0) - Number(compared ?? 0);
                                  return (
                                    <div key={label as string} style={{ display: 'grid', gridTemplateColumns: '130px 1fr 1fr 1fr', gap: 8, padding: '8px 10px', borderTop: '1px solid #e2e8f0', color: '#334155', fontSize: 12 }}>
                                      <span>{label as string}</span>
                                      <span>{(formatter as (value: unknown) => string)(current)}</span>
                                      <span>{(formatter as (value: unknown) => string)(compared)}</span>
                                      <span style={{ color: getPnlColor(difference), fontWeight: 700 }}>
                                        {(formatter as (value: unknown) => string)(difference)}
                                      </span>
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                            {historyParamDiffs.length > 0 ? (
                              <div style={{ display: 'grid', gap: 8 }}>
                                {historyParamDiffs.map((item) => (
                                  <div key={item.key} style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 8 }}>
                                    <div style={{ color: '#0f172a', fontSize: 12, fontWeight: 700 }}>{item.label}</div>
                                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 4, color: '#334155', fontSize: 12 }}>
                                      <div>当前：{item.left}</div>
                                      <div>对比：{item.right}</div>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <div style={{ color: '#64748b', fontSize: 12 }}>两条记录的关键参数一致。</div>
                            )}
                            <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                              当前只展示差异字段，便于直接看出两次回测改了什么。
                            </div>
                          </div>
                        ) : (
                          <div style={{ color: '#64748b', fontSize: 12 }}>至少需要两条历史回测记录才能对比。</div>
                        )}
                        </div>
                      </details>
                    </div>
                  </div>
                </section>
              )}

              {activePanel === 'positions' && (
                <section className="holding-ledger-card" style={{ ...panelStyle, padding: 16, background: '#ffffff', minHeight: 320 }}>
                  <div className="holding-ledger-heading">
                    <div>
                      <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>持仓收益</div>
                      <div>{isOverlayResult ? '基于每日收盘快照核对账户、实际持仓与做T增厚；不作为选股能力评价。' : '基于本次回测的每日收盘快照，不代表实时账户。'}</div>
                    </div>
                    <span>{filteredDailyHoldings.length} / {dailyHoldings.length} 个交易日</span>
                  </div>
                  {dailyHoldings.length > 0 ? (
                    <>
                      <div className="holding-summary-grid">
                        {[
                          ['初始总资产', formatMoney(initialTotalEquity), ''],
                          ['期末总资产', latestHolding ? formatMoney(latestHolding.totalEquity) : '--', 'is-equity'],
                          ['期末现金', latestHolding ? formatMoney(latestHolding.cash) : '--', ''],
                          ['筛选期盈亏', latestHolding ? formatMoney(filteredDailyHoldings.reduce((total, item) => total + item.dailyPnl, 0)) : '--', latestHolding && filteredDailyHoldings.reduce((total, item) => total + item.dailyPnl, 0) < 0 ? 'is-loss' : 'is-profit'],
                          ['持仓天数', `${longHoldingDays + shortHoldingDays} 天`, ''],
                        ].map(([label, value, tone]) => (
                          <div className={`holding-summary-card ${tone}`} key={label}>
                            <span>{label}</span>
                            <strong>{value}</strong>
                          </div>
                        ))}
                      </div>
                      <div className="holding-filter-bar">
                        <label>
                          <div style={labelStyle}>起始日期</div>
                          <input type="date" value={effectiveHoldingStartDate} onChange={(e) => setHoldingStartDate(e.target.value)} style={fieldStyle} />
                        </label>
                        <label>
                          <div style={labelStyle}>结束日期</div>
                          <input type="date" value={effectiveHoldingEndDate} onChange={(e) => setHoldingEndDate(e.target.value)} style={fieldStyle} />
                        </label>
                        <label>
                          <div style={labelStyle}>仓位状态</div>
                          <select value={holdingStatusFilter} onChange={(e) => setHoldingStatusFilter(e.target.value as HoldingStatusFilter)} style={fieldStyle}>
                            <option value="all">全部状态</option>
                            <option value="long">净多仓</option>
                            <option value="short">净空仓</option>
                            <option value="flat">空仓</option>
                          </select>
                        </label>
                        <button type="button" onClick={() => { setHoldingStartDate(''); setHoldingEndDate(''); setHoldingStatusFilter('all'); }}>
                          清除筛选
                        </button>
                      </div>
                      <div className="holding-exposure-strip" aria-label="仓位构成摘要">
                        <span>净多仓 {longHoldingDays} 天</span>
                        <span>净空仓 {shortHoldingDays} 天</span>
                        <span>空仓 {filteredDailyHoldings.length - longHoldingDays - shortHoldingDays} 天</span>
                        <span>收盘快照按 {selectedTimeframe} K 线回测结果汇总</span>
                      </div>
                      {isOverlayResult && result?.resultSchemaVersion !== 3 && (
                        <div style={{ marginTop: 12, border: '1px solid #f59e0b', borderRadius: 6, padding: '12px 14px', background: '#fffbeb', color: '#92400e', fontSize: 12 }}>
                          该历史任务没有保存逐日增强归因数据，请重新运行回测后查看底仓增强收益明细。
                        </div>
                      )}
                      {(!isOverlayResult || result?.resultSchemaVersion === 3) && (
                        <div className="holding-table-wrap" style={{ overflow: 'auto', border: '1px solid #d7dde8', borderRadius: 6 }}>
                          <div className="holding-table-head" style={{ display: 'grid', gridTemplateColumns: isOverlayResult ? OVERLAY_HOLDING_GRID_COLUMNS : STANDARD_HOLDING_GRID_COLUMNS, width: '100%', minWidth: isOverlayResult ? 900 : 780, gap: 0, background: '#f8fafc', color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8' }}>
                            {['日期', '收盘价', '多头持仓', '空头持仓', '现金', '持仓净市值', '总资产', '账户日收益', ...(isOverlayResult ? ['增强日收益'] : [])].map((label) => (
                              <div key={label} style={{ padding: '10px 12px', fontWeight: 700 }}>{label}</div>
                            ))}
                          </div>
                          {pagedDailyHoldings.map((item) => (
                            <div className={`holding-table-row is-${getHoldingStatus(item)}`} key={item.date} style={{ display: 'grid', gridTemplateColumns: isOverlayResult ? OVERLAY_HOLDING_GRID_COLUMNS : STANDARD_HOLDING_GRID_COLUMNS, width: '100%', minWidth: isOverlayResult ? 900 : 780, color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0' }}>
                              <div style={{ padding: '9px 12px' }}>{normalizeApiDate(item.date)}</div>
                              <div style={{ padding: '9px 12px' }}>{formatNumber(item.close)}</div>
                              <div style={{ padding: '9px 12px' }}><span className="holding-quantity is-long">{item.longQty}</span></div>
                              <div style={{ padding: '9px 12px' }}><span className="holding-quantity is-short">{item.shortQty}</span></div>
                              <div style={{ padding: '9px 12px' }}>{formatMoney(item.cash)}</div>
                              <div style={{ padding: '9px 12px' }}>{formatMoney(item.marketValue)}</div>
                              <div style={{ padding: '9px 12px', fontWeight: 600 }}>{formatMoney(item.totalEquity)}</div>
                              <div style={{ padding: '9px 12px', color: getPnlColor(item.dailyPnl) }}>{formatMoney(item.dailyPnl)}</div>
                              {isOverlayResult && (
                                <div style={{ padding: '9px 12px', color: getPnlColor(Number(item.dailyOverlayPnl ?? 0)) }}>{formatMoney(item.dailyOverlayPnl)}</div>
                              )}
                            </div>
                          ))}
                          {filteredDailyHoldings.length === 0 && (
                            <div className="holding-empty-row">当前筛选条件下没有持仓快照。</div>
                          )}
                        </div>
                      )}
                    {(!isOverlayResult || result?.resultSchemaVersion === 3) && (
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginTop: 12 }}>
                      <div style={{ color: '#64748b', fontSize: 12 }}>
                        展示 {holdingPageStart}-{holdingPageEnd} / {filteredDailyHoldings.length} 个交易日
                      </div>
                      <div className="trade-pagination" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ color: '#64748b', fontSize: 12 }}>每页</span>
                        <select
                          value={holdingPageSize}
                          onChange={(event) => setHoldingPageSize(Number(event.target.value))}
                          style={{ ...fieldStyle, width: 82, height: 30, padding: '4px 8px' }}
                        >
                          <option value={20}>20</option>
                          <option value={50}>50</option>
                          <option value={100}>100</option>
                        </select>
                        <button
                          type="button"
                          onClick={() => setHoldingPage((page) => Math.max(1, page - 1))}
                          disabled={safeHoldingPage <= 1}
                          style={{
                            height: 30,
                            padding: '0 10px',
                            border: '1px solid #cbd5e1',
                            borderRadius: 4,
                            background: safeHoldingPage <= 1 ? '#f1f5f9' : '#ffffff',
                            color: safeHoldingPage <= 1 ? '#94a3b8' : '#334155',
                            cursor: safeHoldingPage <= 1 ? 'not-allowed' : 'pointer',
                          }}
                        >
                          上一页
                        </button>
                        <span style={{ color: '#64748b', fontSize: 12, minWidth: 54, textAlign: 'center' }}>
                          {safeHoldingPage} / {holdingPageCount}
                        </span>
                        <button
                          type="button"
                          onClick={() => setHoldingPage((page) => Math.min(holdingPageCount, page + 1))}
                          disabled={safeHoldingPage >= holdingPageCount}
                          style={{
                            height: 30,
                            padding: '0 10px',
                            border: '1px solid #cbd5e1',
                            borderRadius: 4,
                            background: safeHoldingPage >= holdingPageCount ? '#f1f5f9' : '#ffffff',
                            color: safeHoldingPage >= holdingPageCount ? '#94a3b8' : '#334155',
                            cursor: safeHoldingPage >= holdingPageCount ? 'not-allowed' : 'pointer',
                          }}
                        >
                          下一页
                        </button>
                      </div>
                      </div>
                    )}
                    </>
                  ) : (
                    <div style={{ color: '#64748b', fontSize: 13, lineHeight: 1.8 }}>
                      运行一次回测后，这里会展示每日持仓、现金、市值和总资产。
                    </div>
                  )}
                </section>
              )}

              {activePanel === 'logs' && (
                <section style={{ ...panelStyle, padding: 16, background: '#ffffff', minHeight: 320 }}>
                  <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
                    日志
                  </div>
                  <div style={{
                    background: '#f8fafc',
                    border: '1px solid #d7dde8',
                    borderRadius: 6,
                    padding: 14,
                    color: '#334155',
                    fontFamily: 'Consolas, monospace',
                    fontSize: 12,
                    lineHeight: 1.7,
                  }}>
                    <div>输出目录：{result?.outputDir || '--'}</div>
                    <div>回测记录：{result?.runId ? formatRunReference(result.displayNo) : '--'}</div>
                    <div>告警数量：{result?.warnings.length || 0}</div>
                    {result && result.warnings.length > 0 ? (
                      result.warnings.map((item) => (
                        <div key={item}>WARN {item}</div>
                      ))
                    ) : (
                      <div>INFO 当前没有引擎告警。</div>
                    )}
                  </div>
                </section>
              )}
            </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );

  /* return (
    <div style={{
      flex: 1, overflow: 'auto', background: 'var(--canvas)',
      padding: 16, display: 'grid',
      gridTemplateColumns: '320px minmax(0, 1fr)',
      gap: 16,
    }}>
      <aside style={{ ...panelStyle, padding: 14, alignSelf: 'start' }}>
        <div style={{
          color: '#0f172a', fontSize: 15, fontWeight: 700,
          marginBottom: 14,
        }}>
          回测参数
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label>
            <div style={labelStyle}>标的</div>
            <select
              value={selectedSymbol}
              onChange={(e) => setSelectedSymbol(e.target.value)}
              style={fieldStyle}
            >
              {availableInstruments.map((item) => (
                <option key={item.symbol} value={item.symbol}>
                  {item.symbol}{item.name ? ` ${item.name}` : ''}
                </option>
              ))}
            </select>
          </label>

          <label>
            <div style={labelStyle}>买入持有基准</div>
            <select
              value={selectedBenchmarkSymbol}
              onChange={(e) => setSelectedBenchmarkSymbol(e.target.value)}
              style={fieldStyle}
            >
              {availableInstruments.map((item) => (
                <option key={item.symbol} value={item.symbol}>
                  {item.symbol}{item.name ? ` ${item.name}` : ''}
                </option>
              ))}
            </select>
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 8 }}>
            <input
              value={newSymbol}
              onChange={(e) => setNewSymbol(e.target.value)}
              placeholder="新增标的"
              style={fieldStyle}
            />
            <input
              value={newSymbolName}
              onChange={(e) => setNewSymbolName(e.target.value)}
              placeholder="名称"
              style={fieldStyle}
            />
            <button
              onClick={handleCreateInstrument}
              title="添加标的"
              style={{
                width: 36, height: 34, borderRadius: 4,
                border: '1px solid #cbd5e1', background: '#f8fafc',
                color: '#0f766e', display: 'flex', alignItems: 'center',
                justifyContent: 'center', cursor: 'pointer',
              }}
            >
              <Plus size={15} />
            </button>
          </div>

          <label>
            <div style={labelStyle}>账号</div>
            <select
              value={selectedAccountId}
              onChange={(e) => handleAccountSelect(e.target.value)}
              style={fieldStyle}
            >
              {accounts.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}{item.isDefault ? ' 默认' : ''}
                </option>
              ))}
            </select>
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8 }}>
            <input
              value={newAccountName}
              onChange={(e) => setNewAccountName(e.target.value)}
              placeholder="新增账号名"
              style={fieldStyle}
            />
            <button
              onClick={handleCreateAccount}
              title="用当前参数添加账号"
              style={{
                width: 36, height: 34, borderRadius: 4,
                border: '1px solid #cbd5e1', background: '#f8fafc',
                color: '#0f766e', display: 'flex', alignItems: 'center',
                justifyContent: 'center', cursor: 'pointer',
              }}
            >
              <Plus size={15} />
            </button>
          </div>

          <div>
            <div style={labelStyle}>策略</div>
            <div style={{
              ...fieldStyle,
              display: 'flex',
              alignItems: 'center',
              background: '#f1f5f9',
              color: '#334155',
            }}>
              {formatStrategyName(selectedStrategy)}
            </div>
          </div>

            <label>
              <div style={labelStyle}>策略</div>
              <select
                value={selectedConfigId}
                onChange={(e) => handleConfigSelect(e.target.value)}
                style={fieldStyle}
              >
                <option value="">未选择策略方案</option>
                {strategyConfigs.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>

            <input
              value={configName}
              onChange={(e) => setConfigName(e.target.value)}
              placeholder="策略名称"
              style={fieldStyle}
            />
            <label>
              <div style={labelStyle}>运行策略</div>
              <select
                value={selectedStrategy}
                onChange={(e) => setSelectedStrategy(e.target.value)}
                style={fieldStyle}
              >
                {strategies.map((strategyId) => (
                  <option key={strategyId} value={strategyId}>
                    {formatStrategyName(strategyId)}
                  </option>
                ))}
              </select>
              <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.45, marginTop: 5 }}>
                决定本策略实际执行的代码；保存后可直接运行回测。
              </div>
            </label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <button
                onClick={handleSaveConfig}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #0f766e',
                  background: '#ccfbf1', color: '#0f766e', cursor: 'pointer',
                }}
              >
                保存策略
              </button>
              <button
                onClick={handleDeleteConfig}
                disabled={!hasSelectedConfig}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #cbd5e1',
                  background: '#f8fafc', color: '#64748b',
                  cursor: hasSelectedConfig ? 'pointer' : 'not-allowed',
                  opacity: hasSelectedConfig ? 1 : 0.55,
                }}
              >
                删除策略
              </button>
            </div>
          </div>

          <div style={{
            border: '1px solid #d7dde8',
            borderRadius: 6,
            padding: 10,
            background: '#ffffff',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}>
            <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
              策略代码归属于当前完整策略，不能单独切换或复用其他策略的脚本。
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
              <button
                onClick={handleNewStrategy}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #cbd5e1',
                  background: '#f8fafc', color: '#334155', cursor: 'pointer',
                }}
              >
                新建
              </button>
              <button
                onClick={handleCopyStrategy}
                disabled={!hasSelectedConfig}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #cbd5e1',
                  background: '#f8fafc', color: '#334155',
                  cursor: hasSelectedConfig ? 'pointer' : 'not-allowed',
                  opacity: hasSelectedConfig ? 1 : 0.55,
                }}
              >
                复制
              </button>
              <button
                onClick={handleSaveConfig}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #0f766e',
                  background: '#ccfbf1', color: '#0f766e', cursor: 'pointer',
                }}
              >
                保存策略
              </button>
              <button
                onClick={handleDeleteConfig}
                disabled={!hasSelectedConfig}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #cbd5e1',
                  background: '#f8fafc', color: '#64748b',
                  cursor: hasSelectedConfig ? 'pointer' : 'not-allowed',
                  opacity: hasSelectedConfig ? 1 : 0.55,
                }}
              >
                删除
              </button>
            </div>
            <label>
              <div style={labelStyle}>策略名称 <span style={{ color: '#b91c1c' }}>必填</span></div>
              <input
                value={scriptForm.name}
                onChange={(e) => setScriptForm((prev) => ({ ...prev, name: e.target.value }))}
                placeholder="例如：趋势网格 A/B"
                style={fieldStyle}
              />
            </label>
            <label>
              <div style={labelStyle}>策略说明 <span style={{ color: '#64748b' }}>选填</span></div>
              <input
                value={scriptForm.description}
                onChange={(e) => setScriptForm((prev) => ({ ...prev, description: e.target.value }))}
                placeholder="例如：5 分钟级别的趋势网格策略"
                style={fieldStyle}
              />
            </label>
            <textarea
              value={scriptForm.code}
              onChange={(e) => setScriptForm((prev) => ({ ...prev, code: e.target.value }))}
              spellCheck={false}
              style={{
                width: '100%',
                minHeight: 210,
                resize: 'vertical',
                background: '#f8fafc',
                color: '#0f172a',
                border: '1px solid #cbd5e1',
                borderRadius: 4,
                padding: 10,
                fontSize: 12,
                lineHeight: 1.45,
                fontFamily: 'Consolas, monospace',
              }}
            />
              <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                时间字段：time 为 YYYYMMDDHHMMSS，tradeDate 为 YYYY-MM-DD，hhmm 例如 931。脚本必须实现 on_bar(bar, state)；L2 可选 on_quote(quote, state) 和 on_order_event(event, state)。不得访问本地文件。
              </div>
              {hasLegacyTimeSlice(scriptForm.code) && (
                <div style={{ color: '#a16207', fontSize: 11, lineHeight: 1.5 }}>
                  检测到对 bar['time'] 的切片。旧脚本仍兼容；新策略请使用 tradeDate 和 hhmm，避免依赖字符串位置。
                </div>
              )}
          </div>

          <div>
            <div style={labelStyle}>回测区间</div>
            <BacktestDateRangePicker
              startDate={params.startDate} endDate={params.endDate} days={calendarPreflight?.calendarDays || []}
              onChange={(key, value) => setParam(key, value)} onMonthChange={setCalendarMonth}
            />
          </div>

          <label>
            <div style={labelStyle}>初始资金</div>
            <input
              value={params.initialCapital}
              onChange={(e) => setParam('initialCapital', e.target.value)}
              style={fieldStyle}
            />
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <div style={labelStyle}>默认 A 股费用</div>
              <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.45 }}>佣金万分之 0.8（单笔最低 5 元）· 过户费万分之 0.1 · 卖出印花税万分之 5</div>
            </div>
            <label>
              <div style={labelStyle}>滑点</div>
              <input
                value={params.slippage}
                onChange={(e) => setParam('slippage', e.target.value)}
                style={fieldStyle}
              />
            </label>
          </div>

          <label>
            <div style={labelStyle}>仓位模式</div>
            <select
              value={params.positionMode}
              onChange={(e) => setParam('positionMode', e.target.value)}
              style={fieldStyle}
            >
              <option value="fixed_cash">固定金额</option>
              <option value="full_available">可用资金</option>
            </select>
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label>
              <div style={labelStyle}>单笔金额</div>
              <input
                value={params.orderCash}
                onChange={(e) => setParam('orderCash', e.target.value)}
                style={fieldStyle}
              />
            </label>
            <label>
              <div style={labelStyle}>单笔比例</div>
              <input
                value={params.orderPct}
                onChange={(e) => setParam('orderPct', e.target.value)}
                style={fieldStyle}
              />
            </label>
          </div>

          <label style={{
            display: 'flex', alignItems: 'center', gap: 8,
            color: '#64748b', fontSize: 13,
          }}>
            <input
              type="checkbox"
              checked={params.allowShort}
              onChange={(e) => setParam('allowShort', e.target.checked)}
            />
            允许做空
          </label>
          {!params.allowShort && (
            <div style={{ color: '#b45309', fontSize: 11, marginTop: -8 }}>
              当前引擎暂未支持禁用做空，本次仍按策略默认双向逻辑执行
            </div>
          )}


          <div style={{
            color: dataCoverageColor,
            background: dataCoverage.tone === 'ok' ? '#ecfdf5' : '#fffbeb',
            border: `1px solid ${dataCoverage.tone === 'ok' ? '#99f6e4' : '#f59e0b'}`,
            borderRadius: 4,
            padding: '8px 10px',
            fontSize: 12,
            lineHeight: 1.5,
          }}>
            {dataCoverage.text}
          </div>

          {(status || error) && (
            <div style={{
              color: error ? '#b91c1c' : '#0f766e',
              background: error ? '#fef2f2' : '#ecfdf5',
              border: `1px solid ${error ? '#fecaca' : '#99f6e4'}`,
              borderRadius: 4, padding: '8px 10px', fontSize: 12,
            }}>
              {error || status}
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
            <button style={{
              flex: 1, height: 34, borderRadius: 4,
              border: '1px solid #0f766e', background: '#ccfbf1',
              color: '#0f766e', display: 'flex', alignItems: 'center',
              justifyContent: 'center', gap: 6, cursor: 'pointer',
              opacity: runDisabled ? 0.7 : 1,
            }} onClick={handleRunBacktest} disabled={runDisabled}>
              <Play size={14} />
              {running ? '运行中...' : '运行回测'}
            </button>
            <button style={{
              width: 38, height: 34, borderRadius: 4,
              border: '1px solid #cbd5e1', background: '#f8fafc',
              color: '#64748b', display: 'flex', alignItems: 'center',
              justifyContent: 'center', cursor: 'pointer',
            }} onClick={handleResetParams} title="恢复当前账号参数">
              <RotateCcw size={14} />
            </button>
          </div>
        </div>
      </aside>

      <main style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
        <section style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, minmax(140px, 1fr))',
            gap: 12,
          }}>
            {coreMetrics.map(([name, value]) => (
              <div key={name} style={{ ...panelStyle, padding: 14 }}>
                <div style={{ color: '#64748b', fontSize: 12 }}>{name}</div>
                <div style={{
                  color: '#0f172a',
                  fontSize: 24,
                  fontWeight: 700,
                  marginTop: 8,
                }}>
                  {value}
                </div>
              </div>
            ))}
          </div>

          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(3, minmax(180px, 1fr))',
            gap: 12,
          }}>
            {metricGroups.map((group) => (
              <div key={group.title} style={{ ...panelStyle, padding: 14 }}>
                <div style={{
                  color: '#0f172a',
                  fontSize: 14,
                  fontWeight: 700,
                  marginBottom: 10,
                }}>
                  {group.title}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                  {group.rows.map(([name, value]) => (
                    <div key={name} style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      justifyContent: 'space-between',
                      gap: 12,
                      borderBottom: '1px solid #e2e8f0',
                      paddingBottom: 6,
                    }}>
                      <span style={{ color: '#64748b', fontSize: 12 }}>{name}</span>
                      <span style={{
                        color: '#334155',
                        fontSize: 13,
                        fontWeight: 600,
                        textAlign: 'right',
                      }}>
                        {value}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section style={{ ...panelStyle, minHeight: 360, padding: 16 }}>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
          }}>
            <div style={{ color: '#0f172a', fontSize: 15, fontWeight: 700 }}>
              净值曲线
            </div>
            {result && (
              <div style={{ color: '#64748b', fontSize: 12 }}>
                策略 vs 买入持有 {result.benchmarkSymbol || selectedBenchmarkSymbol}
              </div>
            )}
          </div>
          <div style={{
            height: 300, marginTop: 14, border: '1px solid #d7dde8',
            borderRadius: 6, display: 'flex', alignItems: 'stretch',
            color: '#64748b', fontSize: 13, overflow: 'hidden',
            background: '#f8fafc',
          }}>
            {result && result.equity.length > 0 ? (
              <div style={{ flex: 1, padding: '12px 14px 10px', minWidth: 0 }}>
                {(() => {
                  const chart = buildNetValueChart(result.equity, result.benchmarkEquity);
                  return (
                    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
                      <svg
                        viewBox={`0 0 ${chart.width} ${chart.height}`}
                        preserveAspectRatio="none"
                        style={{ width: '100%', height: 238, display: 'block' }}
                        role="img"
                        aria-label="净值曲线"
                      >
                        {[0, 0.5, 1].map((ratio) => {
                          const y = chart.padding.top + chart.chartHeight * ratio;
                          return (
                            <line
                              key={ratio}
                              x1={chart.padding.left}
                              y1={y}
                              x2={chart.width - chart.padding.right}
                              y2={y}
                              stroke="#d7dde8"
                              strokeWidth="1"
                              strokeDasharray="5 7"
                              opacity={0.9}
                            />
                          );
                        })}
                        {chart.chartSeries.map((series) => (
                          <polyline
                            key={series.name}
                            points={toPolyline(series.points)}
                            fill="none"
                            stroke={series.color}
                            strokeWidth={series.name === '策略' ? '3' : '2.5'}
                            strokeLinejoin="round"
                            strokeLinecap="round"
                            vectorEffect="non-scaling-stroke"
                            opacity={series.points.length > 0 ? 1 : 0}
                          />
                        ))}
                        {chart.chartSeries.map((series) => {
                          const point = getLastPoint(series.points);
                          if (!point) return null;
                          return (
                            <circle
                              key={`${series.name}-last`}
                              cx={point.x}
                              cy={point.y}
                              r="4.5"
                              fill={series.color}
                              stroke="#f8fafc"
                              strokeWidth="2"
                              vectorEffect="non-scaling-stroke"
                            >
                              <title>{`${series.name} ${point.date === '__START__' ? '起点' : point.date} 净值 ${point.netValue.toFixed(4)}`}</title>
                            </circle>
                          );
                        })}
                      </svg>
                      <div style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        gap: 10,
                        color: '#64748b',
                        fontSize: 12,
                        lineHeight: 1.5,
                      }}>
                        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                          {chart.chartSeries.map((series) => {
                            const point = getLastPoint(series.points.filter((item) => item.date !== '__START__'));
                            return (
                              <span key={series.name} style={{ color: '#334155' }}>
                                <span style={{ color: series.color, marginRight: 6 }}>●</span>
                                {series.name} {point ? point.netValue.toFixed(4) : '--'}
                              </span>
                            );
                          })}
                          <span>超额 {result ? formatPct(result.excessReturn) : '--'}</span>
                        </div>
                        <span>
                          {chart.actualDates.length > 0
                            ? `${chart.actualDates[0]} - ${chart.actualDates[chart.actualDates.length - 1]}`
                            : '暂无日期'}
                        </span>
                      </div>
                    </div>
                  );
                })()}
              </div>
            ) : (
              <div style={{
                flex: 1, display: 'flex', alignItems: 'center',
                justifyContent: 'center',
              }}>
                等待运行回测
              </div>
            )}
          </div>
          {result && result.warnings.length > 0 && (
            <div style={{ color: '#b45309', fontSize: 12, marginTop: 10 }}>
              {result.warnings.join('；')}
            </div>
          )}
        </section>

        <section style={{ ...panelStyle, padding: 16 }}>
          <div style={{ color: '#0f172a', fontSize: 15, fontWeight: 700, marginBottom: 12 }}>
            交易明细
          </div>
          <div style={{
            display: 'grid', gridTemplateColumns: '120px 120px 1fr 120px 120px',
            color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8',
            paddingBottom: 8,
          }}>
            <span>时间</span>
            <span>方向</span>
            <span>说明</span>
            <span>价格</span>
            <span>收益</span>
          </div>
          {result && result.trades.length > 0 ? (
            result.trades.slice(0, 80).map((trade, index) => (
              <div
                key={`${trade.direction}-${index}`}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '120px 120px 1fr 120px 120px',
                  color: '#334155', fontSize: 12,
                  borderBottom: '1px solid #e2e8f0',
                  padding: '8px 0',
                }}
              >
                <span>{trade.time || '-'}</span>
                <span style={{ color: trade.side === 'BUY' ? '#dc2626' : '#16a34a' }}>
                  {formatTradeDirection(trade.side)}
                </span>
                <span>{formatTradeNote(trade.direction, trade.note)}</span>
                <span>{trade.price.toFixed(2)} ×{trade.quantity}</span>
                <span style={{ color: getPnlColor(trade.pnl) }}>
                  {trade.pnl.toFixed(2)}
                </span>
              </div>
            ))
          ) : (
            <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
              暂无回测结果
            </div>
          )}
        </section>
      </main>
    </div>
  ); */
}
