import {
  Code2,
  Database,
  FileSliders,
  History,
  Play,
  Plus,
  RotateCcw,
  Save,
  Trash2,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  createAccount,
  createStrategyConfig,
  createStrategyScript,
  deleteStrategyConfig,
  deleteStrategyScript,
  fetchAccounts,
  fetchBacktestRuns,
  fetchBacktestRun,
  fetchBacktestTimeframes,
  fetchDataStatus,
  fetchStrategyParameterSchema,
  fetchStrategySource,
  fetchStrategyConfigs,
  fetchStrategyScripts,
  runBacktest,
  runLiveStrategy,
  updateBacktestRunMeta,
  updateStrategyConfig,
  updateStrategyScript,
} from '../api/client';
import type { BacktestRunPayload, BacktestTimeframeAvailability } from '../api/client';
import type {
  BacktestAccount,
  BacktestRunHistory,
  BacktestRunResponse,
  BenchmarkPoint,
  DataStatus,
  EquityPoint,
  InstrumentItem,
  StrategyConfig,
  StrategyParameterSchema,
  StrategyConfigVersion,
  StrategyScript,
  StrategySource,
} from '../types';
import { typography } from '../styles/typography';
import BacktestTabs from './BacktestTabs';
import InstrumentSearchField from './InstrumentSearchField';

interface Props {
  symbol: string;
  strategy: string;
  strategies: string[];
  instruments: InstrumentItem[];
  isAdmin: boolean;
}

const panelStyle = {
  background: '#ffffff',
  border: '1px solid #d7dde8',
  borderRadius: 6,
} as const;

const labelStyle = {
  color: '#64748b',
  fontSize: typography.base,
  marginBottom: 6,
} as const;

const fieldStyle = {
  width: '100%',
  height: 34,
  background: '#f8fafc',
  color: '#0f172a',
  border: '1px solid #cbd5e1',
  borderRadius: 4,
  padding: '0 10px',
  fontSize: typography.base,
} as const;

const DEFAULT_PARAMS = {
  startDate: '20260601',
  endDate: '20260626',
  initialCapital: '1000000',
  commissionRate: '0.0003',
  slippage: '0',
  allowLong: true,
  allowShort: true,
  holdingPolicy: 'intraday',
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

const EXPERIMENT_PARAM_KEYS = new Set([
  'startDate',
  'endDate',
  'initialCapital',
  'commissionRate',
  'slippage',
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

type BacktestParamValue = string | boolean;
type BacktestParams = typeof DEFAULT_PARAMS & Record<string, BacktestParamValue>;
type WorkbenchPanel = 'overview' | 'params' | 'code' | 'trades' | 'positions' | 'logs' | 'history';
type ChartRange = '1m' | '1y' | 'all';
type TradeSideFilter = 'all' | 'BUY' | 'SELL';
type TradePnlFilter = 'all' | 'win' | 'loss';
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
  | 'holdingPolicy';

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
  holdingPolicy: '持仓规则',
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

function getDefaultScriptCode(timeframe: string): string {
  return `# on_bar(bar, state) 会在每根已完成的 ${timeframe} K 线调用一次。
# 当前规则：本 Bar 收盘产生信号，并按本 Bar 收盘价虚拟成交。
# bar 可读：time/open/high/low/close/volume/cash/equity/position/params。
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
  };
}

function isValidDate(value: string) {
  return /^\d{8}$/.test(value);
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

export function hasEnabledTradeDirection(allowLong: boolean, allowShort: boolean): boolean {
  return allowLong || allowShort;
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

export function formatTradeNote(direction: string, note: string): string {
  const normalizedNote = note.trim();
  if (TRADE_NOTE_LABELS[normalizedNote]) return TRADE_NOTE_LABELS[normalizedNote];
  if (!normalizedNote) return TRADE_ACTION_LABELS[direction] || '策略信号';
  if (/[开平]|止损|止盈|强平/.test(normalizedNote)) return normalizedNote;
  const actionLabel = TRADE_ACTION_LABELS[direction];
  return actionLabel ? `${actionLabel}：${normalizedNote}` : normalizedNote;
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

function getTradeSource(direction: string): '策略信号' | '系统结算' {
  return direction.startsWith('FORCE_') ? '系统结算' : '策略信号';
}

function validateParams(params: BacktestParams) {
  const errors: string[] = [];
  const initialCapital = Number(params.initialCapital);
  const commissionRate = Number(params.commissionRate);
  const slippage = Number(params.slippage);

  if (!isValidDate(params.startDate) || !isValidDate(params.endDate)) {
    errors.push('日期格式应为 YYYYMMDD');
  } else if (params.startDate > params.endDate) {
    errors.push('开始日期不能晚于结束日期');
  }
  if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
    errors.push('初始资金必须大于 0');
  }
  if (!Number.isFinite(commissionRate) || commissionRate < 0) {
    errors.push('手续费率不能小于 0');
  }
  if (!Number.isFinite(slippage) || slippage < 0) {
    errors.push('滑点不能小于 0');
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

function formatHistoryParamValue(value: unknown) {
  if (value == null || value === '') return '--';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(6);
  }
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

type HistorySnapshotEntry = {
  label: string;
  value: unknown;
};

function asHistoryRecord(value: unknown): Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function getHistoryRunParam(run: BacktestRunHistory, key: string): unknown {
  const snapshot = asHistoryRecord(run.paramsSnapshot);
  const requestStrategyParams = asHistoryRecord(run.paramsJson.strategyParams);
  return snapshot[key] ?? requestStrategyParams[key] ?? run.paramsJson[key];
}

function getHistoryDirectionLabel(run: BacktestRunHistory): string {
  const allowLong = getHistoryRunParam(run, 'allowLong') !== false;
  const allowShort = getHistoryRunParam(run, 'allowShort') !== false;
  if (allowLong && allowShort) return '允许做多、做空';
  if (allowLong) return '仅允许做多';
  if (allowShort) return '仅允许做空';
  return '未启用交易方向';
}

function shortenHistoryHash(value: unknown): string {
  const hash = typeof value === 'string' ? value : '';
  return hash ? hash.slice(0, 12) : '--';
}

function HistorySnapshotGroup({ title, entries }: { title: string; entries: HistorySnapshotEntry[] }): ReactNode {
  return (
    <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 10, minWidth: 0 }}>
      <div style={{ color: '#475569', fontWeight: 700, marginBottom: 6 }}>{title}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: '5px 10px' }}>
        {entries.map((entry) => (
          <div key={entry.label} style={{ minWidth: 0 }}>
            <span style={{ color: '#64748b' }}>{entry.label}：</span>
            <span style={{ overflowWrap: 'anywhere' }}>{formatHistoryParamValue(entry.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function HistoryRunSnapshot({ run }: { run: BacktestRunHistory }): ReactNode {
  const manifest = run.resultJson?.experimentManifest;
  const scriptSnapshot = asHistoryRecord(run.scriptSnapshot);
  const dataVersion = asHistoryRecord(run.dataVersion);
  const strategyParams = asHistoryRecord(run.paramsSnapshot);
  const snapshotEntries = Object.entries(strategyParams)
    .filter(([key]) => !EXPERIMENT_PARAM_KEYS.has(key))
    .map(([key, value]) => ({ label: key, value }));
  const dataFiles = Array.isArray(dataVersion.files) ? dataVersion.files.length : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ color: '#0f172a', fontWeight: 700 }}>本次实际参数</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 8 }}>
        <HistorySnapshotGroup
          title="方案与方向"
          entries={[
            { label: '策略', value: formatStrategyName(run.strategy) },
            { label: '方案版本', value: run.configVersionId == null ? null : `#${run.configVersionId}` },
            { label: '脚本', value: run.scriptId == null ? null : `#${run.scriptId}` },
            { label: '运行周期', value: `${run.timeframe || '5m'} K 线` },
            { label: '交易方向', value: getHistoryDirectionLabel(run) },
            { label: '持仓规则', value: getHistoryRunParam(run, 'holdingPolicy') },
            { label: '成交规则', value: run.fillModel || manifest?.fillModel },
            { label: '代码指纹', value: shortenHistoryHash(scriptSnapshot.codeHash ?? manifest?.strategyCodeHash) },
          ]}
        />
        <HistorySnapshotGroup
          title="本次实验"
          entries={[
            { label: '标的', value: run.symbol },
            { label: '基准', value: run.benchmarkSymbol },
            { label: '回测区间', value: `${run.startDate}-${run.endDate}` },
            { label: '初始资金', value: getHistoryRunParam(run, 'initialCapital') },
            { label: '手续费率', value: getHistoryRunParam(run, 'commissionRate') },
            { label: '滑点', value: getHistoryRunParam(run, 'slippage') },
          ]}
        />
        <HistorySnapshotGroup
          title="执行与数据"
          entries={[
            { label: '信号规则', value: manifest?.signalModel },
            { label: 'K 线数量', value: manifest?.barCount },
            { label: '数据范围', value: manifest ? `${manifest.barRange.start} - ${manifest.barRange.end}` : null },
            { label: '数据周期', value: manifest?.barRange.timeframe },
            { label: '数据文件', value: dataFiles == null ? null : `${dataFiles} 个` },
            { label: '数据指纹', value: shortenHistoryHash(dataVersion.dataHash ?? manifest?.dataHash) },
          ]}
        />
      </div>
      <details open style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: '8px 10px' }}>
        <summary style={{ color: '#475569', cursor: 'pointer', fontWeight: 700 }}>
          策略参数快照（{snapshotEntries.length} 项）
        </summary>
        {snapshotEntries.length > 0 ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: '6px 12px', marginTop: 8 }}>
            {snapshotEntries.map((entry) => (
              <div key={entry.label} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                <span style={{ color: '#64748b' }}>{entry.label}：</span>
                <span>{formatHistoryParamValue(entry.value)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ color: '#b45309', marginTop: 8 }}>该旧记录未保存策略参数快照。</div>
        )}
      </details>
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
    ['initialCapital', params.initialCapital],
    ['commissionRate', params.commissionRate],
    ['slippage', params.slippage],
    ['allowLong', params.allowLong],
    ['allowShort', params.allowShort],
  ];
  return pairs;
}

function compareHistoryRuns(left: BacktestRunHistory | null, right: BacktestRunHistory | null) {
  if (!left || !right) return [];
  const leftPairs = new Map(getHistoryParamPairs(left));
  const rightPairs = new Map(getHistoryParamPairs(right));
  return (Object.keys(HISTORY_PARAM_LABELS) as HistoryParamKey[])
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

function buildStrategyDraftFingerprint(input: {
  strategy: string;
  mode: string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string;
  accountId: number | '';
  params: Record<string, unknown>;
  script: { code: string; entryType: string };
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

export default function BacktestPage({ symbol, strategy, strategies, instruments, isAdmin }: Props) {
  const [instrumentList, setInstrumentList] = useState<InstrumentItem[]>(instruments);
  const [accounts, setAccounts] = useState<BacktestAccount[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState(symbol);
  const [selectedTimeframe, setSelectedTimeframe] = useState('5m');
  const [backtestTimeframes, setBacktestTimeframes] = useState<BacktestTimeframeAvailability[]>([]);
  const [selectedBenchmarkSymbol, setSelectedBenchmarkSymbol] = useState(symbol);
  const [selectedStrategy, setSelectedStrategy] = useState('user_script');
  const [selectedAccountId, setSelectedAccountId] = useState<number | ''>('');
  const [newAccountName, setNewAccountName] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestRunResponse | null>(null);
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null);
  const [benchmarkDataStatus, setBenchmarkDataStatus] = useState<DataStatus | null>(null);
  const [dataStatusError, setDataStatusError] = useState('');
  const [benchmarkDataStatusError, setBenchmarkDataStatusError] = useState('');
  const [loadingDataStatus, setLoadingDataStatus] = useState(false);
  const [loadingBenchmarkDataStatus, setLoadingBenchmarkDataStatus] = useState(false);
  const [params, setParams] = useState<BacktestParams>(DEFAULT_PARAMS);
  const [mode, setMode] = useState<'backtest' | 'live'>('backtest');
  const [strategyParameterSchema, setStrategyParameterSchema] = useState<StrategyParameterSchema | null>(null);
  const [strategySchemaError, setStrategySchemaError] = useState('');
  const [loadingStrategySchema, setLoadingStrategySchema] = useState(false);
  const [strategyScripts, setStrategyScripts] = useState<StrategyScript[]>([]);
  const [strategyConfigs, setStrategyConfigs] = useState<StrategyConfig[]>([]);
  const [strategySource, setStrategySource] = useState<StrategySource | null>(null);
  const [strategySourceError, setStrategySourceError] = useState('');
  const [configVersions, setConfigVersions] = useState<StrategyConfigVersion[]>([]);
  const [backtestRuns, setBacktestRuns] = useState<BacktestRunHistory[]>([]);
  const [selectedHistoryRunId, setSelectedHistoryRunId] = useState<string | ''>('');
  const [compareHistoryRunId, setCompareHistoryRunId] = useState<string | ''>('');
  const [historyQuery, setHistoryQuery] = useState('');
  const [historySymbolFilter, setHistorySymbolFilter] = useState('');
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState(10);
  const [historyNoteDraft, setHistoryNoteDraft] = useState('');
  const [historyTagsDraft, setHistoryTagsDraft] = useState('');
  const [selectedScriptId, setSelectedScriptId] = useState<string>('');
  const [selectedConfigId, setSelectedConfigId] = useState<string>('');
  const [versionDirty, setVersionDirty] = useState(false);
  const [publishedFingerprint, setPublishedFingerprint] = useState('');
  const [configName, setConfigName] = useState('');
  const [scriptForm, setScriptForm] = useState(createEmptyScriptForm());
  const hasSelectedScript = hasSavedStrategyId(selectedScriptId);
  const hasSelectedConfig = hasSavedStrategyId(selectedConfigId);
  const [activePanel, setActivePanel] = useState<WorkbenchPanel>('overview');
  const [chartRange, setChartRange] = useState<ChartRange>('all');
  const [showStrategyCurve, setShowStrategyCurve] = useState(true);
  const [showBenchmarkCurve, setShowBenchmarkCurve] = useState(true);
  const [showExcessCurve, setShowExcessCurve] = useState(false);
  const [useLogAxis, setUseLogAxis] = useState(false);
  const [tradeSideFilter, setTradeSideFilter] = useState<TradeSideFilter>('all');
  const [tradePnlFilter, setTradePnlFilter] = useState<TradePnlFilter>('all');
  const [tradePage, setTradePage] = useState(1);
  const [tradePageSize, setTradePageSize] = useState(50);
  const [groupTradesByDay, setGroupTradesByDay] = useState(false);
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
  const strategySourceCode = useMemo(() => (
    strategySource?.sources
      .map((source) => `# ${source.path}\n\n${source.code}`)
      .join('\n\n# ----------------------------------------------------------------\n\n') || ''
  ), [strategySource]);
  const draftFingerprint = useMemo(() => buildStrategyDraftFingerprint({
    strategy: selectedStrategy,
    mode,
    symbol: selectedSymbol,
    timeframe: selectedTimeframe,
    benchmarkSymbol: selectedBenchmarkSymbol,
    accountId: selectedAccountId,
    params,
    script: {
      code: scriptForm.code,
      entryType: scriptForm.entryType,
    },
  }), [
    mode,
    params,
    scriptForm.code,
    scriptForm.entryType,
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
    refreshBacktestRuns();
  }, []);

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
    refreshStrategyParameterSchema(selectedStrategy);
  }, [selectedStrategy]);

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
  }, [tradeSideFilter, tradePnlFilter, tradePageSize, result]);

  useEffect(() => {
    setConfigVersions([]);
  }, [selectedConfigId]);

  useEffect(() => {
    if (backtestRuns.length === 0) {
      setSelectedHistoryRunId('');
      setCompareHistoryRunId('');
      return;
    }
    if (typeof selectedHistoryRunId !== 'string') {
      setSelectedHistoryRunId(backtestRuns[0].id);
    }
    if (typeof compareHistoryRunId !== 'string') {
      setCompareHistoryRunId(backtestRuns[1]?.id || backtestRuns[0].id);
    }
  }, [backtestRuns]);

  useEffect(() => {
    if (selectedHistoryRunId === '' || compareHistoryRunId === '') return;
    if (selectedHistoryRunId === compareHistoryRunId) {
      const nextCompare = backtestRuns.find((item) => item.id !== selectedHistoryRunId);
      setCompareHistoryRunId(nextCompare?.id || selectedHistoryRunId);
    }
  }, [selectedHistoryRunId, compareHistoryRunId, backtestRuns]);

  useEffect(() => {
    if (typeof selectedHistoryRunId !== 'string') {
      setHistoryNoteDraft('');
      setHistoryTagsDraft('');
      return;
    }
    let cancelled = false;
    fetchBacktestRun(selectedHistoryRunId)
      .then((run) => {
        if (cancelled) return;
        setBacktestRuns((prev) => prev.map((item) => (item.id === run.id ? run : item)));
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
    const [scripts, configs] = await Promise.all([
      fetchStrategyScripts(),
      fetchStrategyConfigs(),
    ]);
    setStrategyScripts(scripts);
    setStrategyConfigs(configs);
  };

  const refreshBacktestRuns = async () => {
    const runs = await fetchBacktestRuns(30);
    setBacktestRuns(runs);
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

  const applyStrategyConfig = (config: StrategyConfig) => {
    const savedParams = config.paramsJson.params as Partial<BacktestParams> | undefined;
    setSelectedConfigId(config.id);
    setConfigVersions([]);
    setPublishedFingerprint('');
    setConfigName(config.name);
    setMode(config.mode === 'live' ? 'live' : 'backtest');
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
          if (value !== undefined) next[key] = value;
        });
        return next;
      });
    }
    setVersionDirty(false);
    const script = strategyScripts.find((item) => item.id === config.scriptId);
    if (script) {
      setScriptForm({
        name: script.name,
        description: script.description,
        code: script.code,
        entryType: script.entryType,
      });
      setPublishedFingerprint(buildStrategyDraftFingerprint({
        strategy: config.strategy,
        mode: config.mode,
        symbol: config.symbol,
        timeframe: config.timeframe,
        benchmarkSymbol: config.benchmarkSymbol || config.symbol,
        accountId: config.accountId || '',
        params: savedParams || {},
        script: { code: script.code, entryType: script.entryType },
      }));
    }
    setStatus(`已加载策略 ${config.name}`);
    setError('');
  };

  const buildStrategyConfigPayload = (
    scriptId: string,
    name: string,
    strategyId = selectedStrategy,
  ) => ({
    name,
    scriptId,
    strategy: strategyId,
    mode,
    symbol: selectedSymbol,
    timeframe: selectedTimeframe,
    benchmarkSymbol: selectedBenchmarkSymbol || selectedSymbol,
    accountId: typeof selectedAccountId === 'number' ? selectedAccountId : null,
    paramsJson: {
      params: { ...params },
      scriptName: scriptForm.name.trim(),
    },
    enabled: true,
  });

  const setParam = (key: string, value: BacktestParamValue) => {
    setParams((prev) => ({ ...prev, [key]: value }));
  };

  const updateScriptForm = (changes: Partial<typeof scriptForm>) => {
    setScriptForm((prev) => ({ ...prev, ...changes }));
  };

  const setVersionTimeframe = (timeframe: string) => {
    setSelectedTimeframe(timeframe);
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
      return (
        <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.5 }}>
          当前策略未提供参数模板
        </div>
      );
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

  const handleScriptSelect = (value: string) => {
    const scriptId = value || '';
    setSelectedScriptId(scriptId);
    const script = strategyScripts.find((item) => item.id === scriptId);
    if (!script) return;
    setScriptForm({
      name: script.name,
      description: script.description,
      code: script.code,
      entryType: script.entryType,
    });
    if (script.entryType === 'on_bar') setSelectedStrategy('user_script');
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

  const handleNewScript = () => {
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

  const handleSaveScript = async () => {
    setError('');
    setStatus('');
    if (!scriptForm.name.trim()) {
      setError('策略脚本名称不能为空');
      return;
    }
    try {
      const payload = {
        name: scriptForm.name.trim(),
        description: scriptForm.description.trim(),
        code: scriptForm.code,
        entryType: scriptForm.entryType,
      };
      const script = hasSelectedScript
        ? await updateStrategyScript(selectedScriptId, payload)
        : await createStrategyScript(payload);
      await refreshStrategyAssets();
      setSelectedScriptId(script.id);
      setScriptForm({
        name: script.name,
        description: script.description,
        code: script.code,
        entryType: script.entryType,
      });
      if (hasSelectedConfig) {
        setStatus(`已保存策略代码 ${script.name}，请继续保存策略配置`);
      } else {
        setStatus(`已保存策略脚本 ${script.name}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存策略脚本失败');
    }
  };

  const handleDeleteScript = async () => {
    if (!hasSelectedScript) return;
    setError('');
    setStatus('');
    try {
      await deleteStrategyScript(selectedScriptId);
      await refreshStrategyAssets();
      setSelectedScriptId('');
      setScriptForm(createEmptyScriptForm(selectedTimeframe));
      setStatus('已删除策略脚本');
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除策略脚本失败');
    }
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
      };
      const savedScript = hasSelectedScript
        ? await updateStrategyScript(selectedScriptId, scriptPayload)
        : await createStrategyScript(scriptPayload);
      const strategyId = isCustomScript ? 'user_script' : selectedStrategy;
      const payload = {
        ...buildStrategyConfigPayload(
        savedScript.id,
        strategyName,
        strategyId,
        ),
        publish: false,
      };
      const existingConfig = strategyConfigs.find((item) => item.name === strategyName);
      const targetConfigId = hasSelectedConfig
        ? selectedConfigId
        : existingConfig?.id;
      const config = targetConfigId
        ? await updateStrategyConfig(targetConfigId, payload)
        : await createStrategyConfig(payload);
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
      });
      setPublishedFingerprint(draftFingerprint);
      setVersionDirty(false);
      setStatus(`已保存策略 ${config.name}`);
      return config.id;
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存策略失败');
      return null;
    }
  };

  const handleDeleteConfig = async () => {
    if (!hasSelectedConfig) return;
    setError('');
    setStatus('');
    try {
      const deletedConfig = strategyConfigs.find((item) => item.id === selectedConfigId) || null;
      await deleteStrategyConfig(selectedConfigId);
      if (deletedConfig?.scriptId != null) {
        const stillUsed = strategyConfigs.some(
          (item) => item.id !== selectedConfigId && item.scriptId === deletedConfig.scriptId
        );
        if (!stillUsed) {
          await deleteStrategyScript(deletedConfig.scriptId);
        }
      }
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
    if (!hasSelectedConfig) {
      setError('请先选择策略方案，再运行回测');
      return;
    }
    if (versionDirty) {
      setError('策略内容已修改，请先保存策略');
      return;
    }
    const validationErrors = validateParams(params);
    if (validationErrors.length > 0) {
      setError(validationErrors.join('；'));
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
        mode,
        symbol: selectedSymbol,
        benchmarkSymbol: selectedBenchmarkSymbol || selectedSymbol,
        strategy: selectedStrategy,
        timeframe: selectedTimeframe,
        startDate: params.startDate,
        endDate: params.endDate,
        initialCapital: Number(params.initialCapital),
        commissionRate: Number(params.commissionRate),
        slippage: Number(params.slippage),
        allowLong: params.allowLong,
        allowShort: params.allowShort,
        holdingPolicy: params.holdingPolicy === 'overnight' ? 'overnight' : 'intraday',
        isDefault: false,
        strategyParams: params,
        runDraft: false,
      };
      if (mode === 'live') {
        const data = await runLiveStrategy(payload);
        setResult(null);
        refreshBacktestRuns();
        setStatus(`运行模式已记录 #${data.runId ?? '-'}，${data.message}`);
        return;
      }
      const data = await runBacktest(payload);
      setResult(data);
      refreshDataStatus(selectedSymbol, selectedTimeframe);
      refreshBacktestRuns();
      setStatus(`回测完成 #${data.runId ?? '-'}，交易 ${data.summary.tradeCount} 笔`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '回测失败');
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
  const dataCoverage = buildDataCoverage('运行标的', dataStatus, loadingDataStatus, dataStatusError);
  const benchmarkCoverage = buildDataCoverage(
    '基准标的',
    benchmarkDataStatus,
    loadingBenchmarkDataStatus,
    benchmarkDataStatusError,
  );
  const runPreflightReady = dataCoverage.tone === 'ok' && benchmarkCoverage.tone === 'ok';
  const dataCoverageColor = dataCoverage.tone === 'ok' ? '#0f766e' : '#b45309';
  const rangedEquity = result ? filterEquityByRange(result.equity, chartRange) : [];
  const rangedDates = new Set(rangedEquity.map((point) => point.date));
  const rangedBenchmark = result ? filterBenchmarkByDates(result.benchmarkEquity, rangedDates) : [];
  const dailyBars = buildDailyBars(rangedEquity, rangedBenchmark);
  const dailyHoldings = result?.dailyHoldings || [];
  const allSignals = result
    ? [...result.trades, ...(result.skippedSignals || [])].sort((left, right) => (
      (left.signalTime || left.time).localeCompare(right.signalTime || right.time)
    ))
    : [];
  const filteredTrades = allSignals.filter((trade) => {
    if (tradeSideFilter !== 'all' && trade.side !== tradeSideFilter) return false;
    if (tradePnlFilter === 'win' && trade.pnl <= 0) return false;
    if (tradePnlFilter === 'loss' && trade.pnl >= 0) return false;
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
          <span style={{ color: skipped ? '#b45309' : '#0f766e', fontWeight: 700 }}>
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
        <span style={{ color: trade.side === 'BUY' ? '#dc2626' : '#16a34a', fontWeight: 600 }}>
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
        const source = isSystem ? systemSource : moduleName;
        return (
          <span style={{
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
      render: (trade) => formatTradeNote(trade.direction, trade.note),
    },
    {
      key: 'price',
      label: '成交价/数量',
      width: 'minmax(100px, 1fr)',
      align: 'right',
      render: (trade) => (
        <span>
          <span>{`${trade.price.toFixed(2)} ×${trade.quantity}`}</span>
          <span style={{ display: 'block', color: '#64748b', fontSize: 11, marginTop: 2 }}>
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
      render: (trade) => (
        <span style={{ color: getPnlColor(trade.pnl), fontWeight: 600 }}>
          {trade.pnl.toFixed(2)}
        </span>
      ),
    },
  ];
  const visibleTradeColumns = tradeColumnDefs.filter((column) => tradeColumns[column.key]);
  const tradeGridColumns = visibleTradeColumns.map((column) => column.width).join(' ') || '1fr';
  const coreMetrics = [
    ['策略收益', result ? formatPct(result.summary.totalReturn) : '--'],
    ['基准收益', result ? formatPct(result.benchmarkReturn) : '--'],
    ['超额收益', result ? formatPct(result.excessReturn) : '--'],
    ['最大回撤率', result ? formatPct(result.summary.maxDrawdownRate) : '--'],
  ];
  const metricGroups = [
    {
      title: '收益',
      rows: [
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
        ['胜率', result ? formatPct(result.summary.winRate) : '--'],
        ['日胜率', result ? formatPct(result.summary.dailyWinRate) : '--'],
        ['盈亏比', result ? formatNumber(result.summary.profitLossRatio) : '--'],
        ['交易次数', result ? String(result.summary.tradeCount) : '--'],
        ['盈利次数', result ? String(result.summary.winningTrades) : '--'],
        ['亏损次数', result ? String(result.summary.losingTrades) : '--'],
        ['平均盈利', result ? formatMoney(result.summary.avgWin) : '--'],
        ['平均亏损', result ? formatMoney(result.summary.avgLoss) : '--'],
      ],
    },
  ];

  const resultTabs = [
    { id: 'overview' as WorkbenchPanel, label: '概览', icon: FileSliders },
    { id: 'trades' as WorkbenchPanel, label: '信号明细', icon: History },
    { id: 'positions' as WorkbenchPanel, label: '持仓收益', icon: Database },
    { id: 'history' as WorkbenchPanel, label: '历史回测', icon: History },
    { id: 'code' as WorkbenchPanel, label: '策略管理', icon: Code2 },
  ];
  const activeConfig = strategyConfigs.find((item) => item.id === selectedConfigId);
  const activeScript = strategyScripts.find((item) => item.id === selectedScriptId);
  const configNameById = new Map(strategyConfigs.map((item) => [item.id, item.name]));
  const selectedHistoryRun = backtestRuns.find((item) => item.id === selectedHistoryRunId) || null;
  const compareHistoryRun = backtestRuns.find((item) => item.id === compareHistoryRunId) || null;
  const historyParamDiffs = compareHistoryRuns(selectedHistoryRun, compareHistoryRun);
  const historySymbols = useMemo(
    () => Array.from(new Set(backtestRuns.map((run) => run.symbol))).sort(),
    [backtestRuns]
  );
  const filteredHistoryRuns = useMemo(() => {
    const query = historyQuery.trim().toLowerCase();
    return backtestRuns.filter((run) => {
      const configName = run.configId ? configNameById.get(run.configId) || '' : '';
      const searchable = [run.id, run.symbol, run.strategy, configName, ...run.tags].join(' ').toLowerCase();
      return (!historySymbolFilter || run.symbol === historySymbolFilter) && (!query || searchable.includes(query));
    });
  }, [backtestRuns, configNameById, historyQuery, historySymbolFilter]);
  const historyPageCount = Math.max(1, Math.ceil(filteredHistoryRuns.length / historyPageSize));
  const safeHistoryPage = Math.min(historyPage, historyPageCount);
  const historyPageStart = (safeHistoryPage - 1) * historyPageSize;
  const pagedHistoryRuns = filteredHistoryRuns.slice(historyPageStart, historyPageStart + historyPageSize);
  const historyRangeStart = filteredHistoryRuns.length > 0 ? historyPageStart + 1 : 0;
  const historyRangeEnd = Math.min(historyPageStart + historyPageSize, filteredHistoryRuns.length);
  const historyGridColumns = 'minmax(64px, 0.5fr) minmax(150px, 1.25fr) minmax(150px, 1.25fr) minmax(120px, 0.95fr) minmax(76px, 0.6fr) minmax(76px, 0.6fr) minmax(70px, 0.5fr) minmax(130px, 1fr) 74px';
  const activePanelLabel = resultTabs.find((item) => item.id === activePanel)?.label || '概览';
  const runDisabled = running || !hasSelectedConfig || versionDirty;
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
        pnl: trade.pnl,
        note: trade.note,
      }))
    );
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
    setParams({
      ...DEFAULT_PARAMS,
      ...savedStrategyParams,
      startDate: run.startDate,
      endDate: run.endDate,
      initialCapital: textValue('initialCapital', DEFAULT_PARAMS.initialCapital),
      commissionRate: textValue('commissionRate', DEFAULT_PARAMS.commissionRate),
      slippage: textValue('slippage', DEFAULT_PARAMS.slippage),
      allowLong: boolValue('allowLong', DEFAULT_PARAMS.allowLong),
      allowShort: boolValue('allowShort', DEFAULT_PARAMS.allowShort),
      holdingPolicy: textValue('holdingPolicy', DEFAULT_PARAMS.holdingPolicy),
    });
    setStatus(`已恢复历史回测 #${run.id} 的参数`);
  };

  const handleViewHistoryResult = (run: BacktestRunHistory) => {
    if (!run.resultJson) {
      setError('该历史记录未保存完整结果，请重新运行后查看图表和信号明细。');
      return;
    }
    setResult({
      ...run.resultJson,
      runId: run.id,
      configId: run.configId,
      configVersionId: run.configVersionId,
      scriptId: run.scriptId,
    });
    setActivePanel('overview');
    setStatus(`正在查看历史回测 #${run.id} 的结果`);
  };

  const handleRerunHistory = async (run: BacktestRunHistory) => {
    const saved = run.paramsJson;
    const account = accounts.find((item) => item.id === run.accountId);
    setError('');
    setStatus('');
    setRunning(true);
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
        timeframe: run.timeframe || '5m',
        startDate: run.startDate,
        endDate: run.endDate,
        initialCapital: Number(saved.initialCapital ?? 1_000_000),
        commissionRate: Number(saved.commissionRate ?? 0),
        slippage: Number(saved.slippage ?? 0),
        allowLong: saved.allowLong !== false,
        allowShort: saved.allowShort !== false,
        holdingPolicy: saved.holdingPolicy === 'overnight' ? 'overnight' : 'intraday',
        isDefault: false,
        strategyParams: (saved.strategyParams || {}) as Record<string, string | boolean>,
      });
      setResult(data);
      await refreshBacktestRuns();
      setActivePanel('overview');
      setStatus(`已重新运行回测 #${run.id}，新记录为 #${data.runId ?? '-'}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '重新运行回测失败');
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
      setStatus(`已保存历史记录 #${run.id} 的备注和标签`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存历史备注失败');
    }
  };

  return (
    <div style={{
      flex: 1,
      minHeight: 0,
      background: '#eef3f8',
      color: '#0f172a',
      display: 'grid',
      gridTemplateRows: activePanel === 'overview' ? '52px minmax(0, 1fr)' : 'minmax(0, 1fr)',
      overflow: 'hidden',
    }}>
      {activePanel === 'overview' && <header style={{
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

        <div style={{ display: 'flex', gap: 6 }}>
          {[
            ['backtest', '回测'],
            ['live', '运行计划'],
          ].map(([value, label]) => (
            <button
              key={value}
              onClick={() => setMode(value as 'backtest' | 'live')}
              style={{
                height: 34,
                minWidth: 54,
                borderRadius: 4,
                border: `1px solid ${mode === value ? '#0f766e' : '#cbd5e1'}`,
                background: mode === value ? '#ccfbf1' : '#f8fafc',
                color: mode === value ? '#0f766e' : '#64748b',
                cursor: 'pointer',
                fontSize: 14,
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <button
          onClick={handleRunBacktest}
          disabled={runDisabled}
          title={!hasSelectedConfig
            ? '请先选择策略方案'
            : versionDirty
              ? '请先保存策略'
              : !runPreflightReady
                ? '请等待运行前检查通过'
                : undefined}
          style={{
            height: 36,
            borderRadius: 4,
            border: `1px solid ${runDisabled ? '#cbd5e1' : '#0f766e'}`,
            background: runDisabled ? '#f1f5f9' : '#ccfbf1',
            color: runDisabled ? '#94a3b8' : '#0f766e',
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
          {running ? '处理中...' : !hasSelectedConfig ? '请选择策略方案' : mode === 'live' ? '记录运行计划' : '运行回测'}
        </button>
      </header>}

      <div style={{
        minHeight: 0,
        display: 'grid',
        gridTemplateColumns: activePanel === 'history' ? 'minmax(0, 1fr)' : '340px minmax(0, 1fr)',
        overflow: 'hidden',
      }}>
        {activePanel !== 'history' && (
        <aside style={{
          minHeight: 0,
          overflow: 'auto',
          borderRight: '1px solid #e2e8f0',
          background: '#f8fafc',
          padding: 14,
        }}>
          <div style={{
            color: '#0f172a',
            fontSize: 15,
            fontWeight: 700,
            marginBottom: 12,
          }}>
            实验设置
          </div>

          {activePanel === 'overview' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#64748b', fontSize: 12, marginBottom: 8 }}>当前策略方案</div>
                <div style={{ color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                  <div>名称：{activeConfig?.name || configName || '未选择策略方案'}</div>
                  <div>模式：{mode === 'live' ? '运行' : '回测'}</div>
                  <div>脚本：{activeScript?.name || '未绑定'}</div>
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
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  <label>
                    <div style={labelStyle}>开始日期</div>
                    <input value={params.startDate} onChange={(e) => setParam('startDate', e.target.value)} style={fieldStyle} />
                  </label>
                  <label>
                    <div style={labelStyle}>结束日期</div>
                    <input value={params.endDate} onChange={(e) => setParam('endDate', e.target.value)} style={fieldStyle} />
                  </label>
                </div>
              </section>

              <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700 }}>资金与交易规则</div>
                <label>
                  <div style={labelStyle}>初始资金</div>
                  <input value={params.initialCapital} onChange={(e) => setParam('initialCapital', e.target.value)} style={fieldStyle} />
                </label>
                <label>
                  <div style={labelStyle}>手续费率</div>
                  <input value={params.commissionRate} onChange={(e) => setParam('commissionRate', e.target.value)} style={fieldStyle} />
                </label>
                <label>
                  <div style={labelStyle}>滑点</div>
                  <input value={params.slippage} onChange={(e) => setParam('slippage', e.target.value)} style={fieldStyle} />
                </label>
                <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 10, color: '#64748b', fontSize: 12, lineHeight: 1.55 }}>
                  策略方向：{params.allowLong ? '允许做多' : '不做多'} · {params.allowShort ? '允许做空' : '不做空'}；请在“策略管理”中修改并保存策略。
                </div>
                {['trend_follow', 'grid_mean_reversion', 'trend_grid', 'trend_grid_both'].includes(selectedStrategy) && (
                  <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 10 }}>
                    <div style={{ ...labelStyle, marginBottom: 7 }}>持仓规则</div>
                    <select
                      value={params.holdingPolicy}
                      onChange={(e) => setParam('holdingPolicy', e.target.value)}
                      style={fieldStyle}
                    >
                      <option value="intraday">日内平仓</option>
                      <option value="overnight">允许跨日</option>
                    </select>
                    <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.55, marginTop: 6 }}>
                      {params.holdingPolicy === 'overnight'
                        ? '保留期末仓位，按最后价格计入未实现盈亏。'
                        : '最后一个 Bar 禁止新开仓，收盘自动结算剩余仓位。'}
                    </div>
                  </div>
                )}
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
                {mode === 'live' && (
                  <div style={{ color: '#b45309', fontSize: 12, lineHeight: 1.65, marginTop: 8 }}>
                    运行计划仅登记任务，当前不会执行实盘交易。
                  </div>
                )}
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

          {activePanel === 'positions' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{
                color: dataCoverageColor,
                background: dataCoverage.tone === 'ok' ? '#ecfdf5' : '#fffbeb',
                border: `1px solid ${dataCoverage.tone === 'ok' ? '#99f6e4' : '#f59e0b'}`,
                borderRadius: 4,
                padding: '10px 12px',
                fontSize: 12,
                lineHeight: 1.6,
              }}>
                {dataCoverage.text}
              </div>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>标的：{selectedSymbol}</div>
                <div>周期：{selectedTimeframe}</div>
                <div>首日：{dataStatus?.firstDate || '-'}</div>
                <div>末日：{dataStatus?.lastDate || '-'}</div>
                <div>缺口：{dataStatus?.gapDates.length ?? 0}</div>
                <div>重复：{dataStatus?.duplicateCount ?? 0}</div>
              </div>
            </div>
          )}

          {activePanel === 'trades' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>交易次数：{result?.summary.tradeCount ?? 0}</div>
                <div>盈利次数：{result?.summary.winningTrades ?? 0}</div>
                <div>亏损次数：{result?.summary.losingTrades ?? 0}</div>
                <div>胜率：{result ? formatPct(result.summary.winRate) : '--'}</div>
                <div>当前筛选：{filteredTrades.length}</div>
              </div>

              <div style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700, marginBottom: 10 }}>筛选</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <label>
                    <div style={labelStyle}>买卖方向</div>
                    <select value={tradeSideFilter} onChange={(e) => setTradeSideFilter(e.target.value as TradeSideFilter)} style={fieldStyle}>
                      <option value="all">全部</option>
                      <option value="BUY">买入</option>
                      <option value="SELL">卖出</option>
                    </select>
                  </label>
                  <label>
                    <div style={labelStyle}>盈亏状态</div>
                    <select value={tradePnlFilter} onChange={(e) => setTradePnlFilter(e.target.value as TradePnlFilter)} style={fieldStyle}>
                      <option value="all">全部</option>
                      <option value="win">盈利</option>
                      <option value="loss">亏损</option>
                    </select>
                  </label>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, color: '#64748b', fontSize: 13 }}>
                    <input type="checkbox" checked={groupTradesByDay} onChange={(e) => setGroupTradesByDay(e.target.checked)} />
                    按日期分组
                  </label>
                </div>
              </div>

              <div style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                <div style={{ color: '#0f172a', fontSize: 13, fontWeight: 700, marginBottom: 10 }}>显示字段</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                  {tradeColumnDefs.map((column) => (
                    <label key={column.key} style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#64748b', fontSize: 12 }}>
                      <input
                        type="checkbox"
                        checked={tradeColumns[column.key]}
                        onChange={(e) => setTradeColumns((prev) => ({ ...prev, [column.key]: e.target.checked }))}
                      />
                      {column.label}
                    </label>
                  ))}
                </div>
              </div>

              <button
                onClick={handleExportTrades}
                disabled={filteredTrades.length === 0}
                style={{
                  height: 36,
                  borderRadius: 4,
                  border: '1px solid #0f766e',
                  background: filteredTrades.length > 0 ? '#ccfbf1' : '#f1f5f9',
                  color: filteredTrades.length > 0 ? '#0f766e' : '#94a3b8',
                  cursor: filteredTrades.length > 0 ? 'pointer' : 'not-allowed',
                }}
              >
                导出当前交易 CSV
              </button>
              <div style={{ color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                字段筛选只影响当前页面展示和导出，不会改动回测结果。
              </div>
            </div>
          )}

          {false && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>历史回测：{backtestRuns.length}</div>
                <div>当前方案：{activeConfig?.name || '未选择'}</div>
                <div>方案版本：{configVersions.length}</div>
                <div>当前脚本：{activeScript?.name || '未选择'}</div>
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
                onClick={refreshBacktestRuns}
                style={{
                  height: 36,
                  borderRadius: 4,
                  border: '1px solid #cbd5e1',
                  background: '#f8fafc',
                  color: '#334155',
                  cursor: 'pointer',
                }}
              >
                刷新历史
              </button>
            </div>
          )}

          {activePanel === 'logs' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#334155', fontSize: 13, lineHeight: 1.7 }}>
                <div>已保存策略：{strategyConfigs.length}</div>
                <div>关联脚本：{strategyScripts.length}</div>
                <div>当前回测：{result ? `#${result.runId ?? '-'}` : '暂无'}</div>
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
          padding: 16,
          background: '#eef3f8',
        }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, minHeight: '100%' }}>
            <BacktestTabs
              activeTab={activePanel}
              tabs={resultTabs}
              onChange={(tabId) => setActivePanel(tabId as WorkbenchPanel)}
            />
            {activePanel === 'code' ? (
            <section style={{
              ...panelStyle,
              minHeight: 'calc(100vh - 164px)',
              padding: 16,
              background: '#ffffff',
              display: 'grid',
              gridTemplateRows: 'auto minmax(0, 1fr)',
              gap: 12,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 12 }}>
                <div>
                  <div style={{ color: '#0f172a', fontSize: 16, fontWeight: 700 }}>
                  {scriptForm.name || '未命名策略'}
                  </div>
                  <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                    当前策略代码
                  </div>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '280px minmax(0, 1fr) 340px', gap: 12, minHeight: 0 }}>
                <section style={{
                  ...panelStyle,
                  padding: 12,
                  background: '#f8fafc',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 12,
                  minHeight: 0,
                  overflow: 'auto',
                }}>
                  <div style={{ color: '#0f172a', fontSize: 14, fontWeight: 700 }}>策略列表</div>
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
                  <div style={{
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
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 8 }}>
                    <button
                      onClick={handleNewScript}
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
                  <div style={{ ...panelStyle, padding: 12, background: '#ffffff', color: '#64748b', fontSize: 12, lineHeight: 1.6 }}>
                    保存会同时更新代码和参数。每次回测会保存当时快照，可在历史回测中回溯。
                  </div>
                </section>

                <section style={{
                  ...panelStyle,
                  padding: 12,
                  background: '#ffffff',
                  display: 'grid',
                  gridTemplateRows: 'auto minmax(0, 1fr)',
                  gap: 12,
                  minHeight: 0,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
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
                  </div>
                  {selectedStrategy === 'user_script' ? (
                    <div style={{ display: 'grid', gridTemplateRows: 'auto auto minmax(0, 1fr) auto', gap: 10, minHeight: 0 }}>
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
                      <textarea
                        value={scriptForm.code}
                        onChange={(e) => updateScriptForm({ code: e.target.value })}
                        spellCheck={false}
                        style={{
                          width: '100%', minHeight: 420, resize: 'vertical', background: '#f8fafc',
                          color: '#334155', border: '1px solid #cbd5e1', borderRadius: 6, padding: 14,
                          fontSize: 13, lineHeight: 1.55, fontFamily: 'Consolas, monospace', outline: 'none',
                        }}
                      />
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                        <span style={{ color: '#64748b', fontSize: 12 }}>
                          接口：on_bar(bar, state)；每根已完成的 {selectedTimeframe} K 线触发，按收盘价成交；支持导入后端已安装模块。
                        </span>
                        <span style={{ color: '#64748b', fontSize: 12 }}>代码将随“保存策略”一并保存。</span>
                      </div>
                    </div>
                  ) : (
                    <textarea
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

                <section style={{
                  ...panelStyle,
                  padding: 12,
                  background: '#f8fafc',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 12,
                  minHeight: 0,
                  overflow: 'auto',
                }}>
                  <div style={{ color: '#0f172a', fontSize: 14, fontWeight: 700 }}>参数与运行</div>
                  <InstrumentSearchField label="运行标的" value={selectedSymbol} onChange={setSelectedSymbol} />
                  <label>
                    <div style={labelStyle}>运行周期</div>
                    <select
                      value={selectedTimeframe}
                      onChange={(e) => setVersionTimeframe(e.target.value)}
                      style={fieldStyle}
                    >
                      {backtestTimeframes.map((item) => (
                        <option key={item.timeframe} value={item.timeframe}>
                          {item.timeframe}{item.available ? '（QMT 数据已导入）' : '（QMT 数据未覆盖）'}
                        </option>
                      ))}
                    </select>
                    {backtestTimeframes.length === 0 && (
                      <div style={{ color: '#b45309', fontSize: 12, marginTop: 5 }}>
                        正在读取回测引擎支持的周期。
                      </div>
                    )}
                  </label>
                  <InstrumentSearchField label="基准标的" value={selectedBenchmarkSymbol} onChange={setSelectedBenchmarkSymbol} />
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
                  <div style={{
                    ...panelStyle,
                    padding: '10px',
                    background: '#ffffff',
                  }}>
                    {renderStrategyParameterForm()}
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                    <label>
                      <div style={labelStyle}>开始日期</div>
                      <input value={params.startDate} onChange={(e) => setParam('startDate', e.target.value)} style={fieldStyle} />
                    </label>
                    <label>
                      <div style={labelStyle}>结束日期</div>
                      <input value={params.endDate} onChange={(e) => setParam('endDate', e.target.value)} style={fieldStyle} />
                    </label>
                  </div>
                  <label>
                    <div style={labelStyle}>初始资金</div>
                    <input value={params.initialCapital} onChange={(e) => setParam('initialCapital', e.target.value)} style={fieldStyle} />
                  </label>
                  <label>
                    <div style={labelStyle}>手续费率</div>
                    <input value={params.commissionRate} onChange={(e) => setParam('commissionRate', e.target.value)} style={fieldStyle} />
                  </label>
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
                  <div style={{
                    color: benchmarkCoverage.tone === 'ok' ? '#0f766e' : '#b45309',
                    background: benchmarkCoverage.tone === 'ok' ? '#ecfdf5' : '#fffbeb',
                    border: `1px solid ${benchmarkCoverage.tone === 'ok' ? '#99f6e4' : '#f59e0b'}`,
                    borderRadius: 4,
                    padding: '8px 10px',
                    fontSize: 12,
                    lineHeight: 1.5,
                  }}>
                    {benchmarkCoverage.text}
                  </div>
                  {(status || error) && (
                    <div style={{
                      color: error ? '#b91c1c' : '#0f766e',
                      background: error ? '#fef2f2' : '#ecfdf5',
                      border: `1px solid ${error ? '#fecaca' : '#99f6e4'}`,
                      borderRadius: 4,
                      padding: '8px 10px',
                      fontSize: 12,
                    }}>
                      {error || status}
                    </div>
                  )}
                  <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                    <button style={{
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
                    }} onClick={handleRunBacktest} disabled={runDisabled} title={versionDirty ? '请先保存策略' : !hasSelectedConfig ? '请先保存策略' : !runPreflightReady ? '请等待运行前检查通过' : undefined}>
                      <Play size={14} />
                      {running ? '处理中...' : !hasSelectedConfig ? '请选择策略方案' : mode === 'live' ? '记录运行计划' : '运行回测'}
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
                </section>
              </div>
            </section>
            ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                <div>
                  <div style={{ color: '#0f172a', fontSize: typography.xl, fontWeight: 700 }}>
                    {activePanelLabel}
                  </div>
                  <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                    {activePanel === 'overview' && '收益概览、基准对比和交易摘要'}
                    {activePanel === 'params' && '当前参数位于左侧，可随策略一起保存'}
                    {activePanel === 'trades' && '单笔信号记录与后续字段筛选'}
                    {activePanel === 'positions' && '每日持仓与收益快照待接入'}
                    {activePanel === 'history' && '回测记录与结果快照追溯'}
                    {activePanel === 'logs' && '回测与运行日志待接入'}
                  </div>
                </div>
              </div>

              {activePanel === 'overview' && (
              <section style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(140px, 1fr))', gap: 12 }}>
                  {coreMetrics.map(([name, value]) => (
                    <div key={name} style={{ ...panelStyle, padding: 14, background: '#ffffff' }}>
                      <div style={{ color: '#64748b', fontSize: 12 }}>{name}</div>
                      <div style={{ color: '#0f172a', fontSize: 24, fontWeight: 700, marginTop: 8 }}>
                        {value}
                      </div>
                    </div>
                  ))}
                </div>

              </section>
              )}

              {activePanel === 'overview' && (
              <section style={{ ...panelStyle, padding: 16, background: '#ffffff' }}>
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
                      <div key={group.title} style={{ border: '1px solid #d7dde8', borderRadius: 6, overflow: 'hidden' }}>
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
              <section style={{ ...panelStyle, minHeight: 360, padding: 16, background: '#ffffff' }}>
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
                {result?.experimentManifest && (
                  <div style={{ marginTop: 10, padding: '9px 10px', borderRadius: 5, background: '#f1f5f9', color: '#475569', fontSize: 12, lineHeight: 1.65 }}>
                    <div>
                      回测口径：Bar 收盘信号虚拟成交 · {result.experimentManifest.holdingPolicy === 'overnight' ? '允许跨日' : result.experimentManifest.holdingPolicy === 'intraday' ? '日内平仓' : '期末规则未记录'} · 手续费 {(result.experimentManifest.costModel.commissionRate * 10_000).toFixed(2)} bp · 滑点 {(result.experimentManifest.costModel.slippage * 10_000).toFixed(2)} bp
                    </div>
                    <div>
                      实验指纹：{result.experimentManifest.experimentHash.slice(0, 12)} · {result.experimentManifest.barCount} 根 {result.experimentManifest.barRange.timeframe} K 线
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
              <section style={{ ...panelStyle, padding: 16, background: '#ffffff' }}>
                <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
                  信号明细
                </div>
                <div style={{
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
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
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
                <div style={{ display: 'grid', gridTemplateColumns: tradeGridColumns, gap: 8, color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8', paddingBottom: 8 }}>
                  {visibleTradeColumns.map((column) => (
                    <span key={column.key} style={{ textAlign: column.align || 'left' }}>
                      {column.label}
                    </span>
                  ))}
                </div>
                {result && filteredTrades.length > 0 && visibleTradeColumns.length > 0 ? (
                  pagedTrades.map((trade, index) => {
                    const currentDate = (trade.time || '').slice(0, 8) || '未知日期';
                    const previousTrade = pagedTrades[index - 1];
                    const previousDate = previousTrade ? ((previousTrade.time || '').slice(0, 8) || '未知日期') : '';
                    const showGroupTitle = groupTradesByDay && currentDate !== previousDate;
                    return (
                      <div key={`${trade.time}-${trade.direction}-${index}`}>
                        {showGroupTitle && (
                          <div style={{ color: '#0f766e', fontSize: 12, fontWeight: 700, padding: '10px 0 4px', borderBottom: '1px solid #e2e8f0' }}>
                            {currentDate}
                          </div>
                        )}
                        <div style={{ display: 'grid', gridTemplateColumns: tradeGridColumns, gap: 8, color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0', padding: '8px 0', alignItems: 'center' }}>
                          {visibleTradeColumns.map((column) => (
                            <span key={column.key} style={{
                              textAlign: column.align || 'left',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: column.key === 'note' ? 'normal' : 'nowrap',
                              lineHeight: column.key === 'note' ? 1.5 : undefined,
                            }}>
                              {column.render(trade)}
                            </span>
                          ))}
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

              {activePanel === 'history' && (
                <section style={{ ...panelStyle, padding: 16, background: '#ffffff', minHeight: 320 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                    <div>
                      <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700 }}>
                        历史回测
                      </div>
                      <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                        每次回测会保存代码、参数与行情数据快照。
                      </div>
                    </div>
                    <button
                      onClick={refreshBacktestRuns}
                      style={{
                        height: 34,
                        borderRadius: 4,
                        border: '1px solid #cbd5e1',
                        background: '#f8fafc',
                        color: '#334155',
                        cursor: 'pointer',
                        padding: '0 12px',
                      }}
                    >
                      刷新
                    </button>
                  </div>
                  <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
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
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 16 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: 'grid', gridTemplateColumns: historyGridColumns, gap: 8, color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8', padding: '0 10px 8px' }}>
                        <span>ID</span>
                        <span>策略</span>
                        <span>标的/基准</span>
                        <span>区间</span>
                        <span>收益</span>
                        <span>回撤</span>
                        <span>Sharpe</span>
                        <span>完成时间</span>
                        <span>操作</span>
                      </div>
                      {filteredHistoryRuns.length > 0 ? (
                        pagedHistoryRuns.map((run) => {
                          const isSelected = run.id === selectedHistoryRunId;
                          return (
                            <div key={run.id} onClick={() => setSelectedHistoryRunId(run.id)} style={{ display: 'grid', gridTemplateColumns: historyGridColumns, gap: 8, alignItems: 'center', color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0', padding: '9px 10px', background: isSelected ? '#ecfdf5' : 'transparent', cursor: 'pointer' }}>
                              <button onClick={() => setSelectedHistoryRunId(run.id)} style={{ textAlign: 'left', border: 'none', background: 'transparent', padding: 0, color: '#0f172a', cursor: 'pointer', fontWeight: isSelected ? 700 : 500 }}>
                                #{run.id}
                              </button>
                              <span style={{ minWidth: 0 }}>
                                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {run.configId ? (configNameById.get(run.configId) || `方案 ${run.configId}`) : '未绑定方案'}
                                </div>
                                <div style={{ color: '#64748b', marginTop: 2 }}>
                                  {run.configId ? '已保存策略快照' : '未关联策略'}
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
                                <div>{run.status}</div>
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
                            </div>
                          );
                        })
                      ) : (
                        <div style={{ color: '#64748b', fontSize: 13, padding: '18px 0' }}>
                          没有符合当前筛选条件的历史实验。
                        </div>
                      )}
                      {filteredHistoryRuns.length > 0 && (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '12px 10px 0', color: '#64748b', fontSize: 12 }}>
                          <span>显示 {historyRangeStart}-{historyRangeEnd} / {filteredHistoryRuns.length} 条实验</span>
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
                      <div style={{ ...panelStyle, padding: 14, background: '#ffffff' }}>
                      <div style={{ color: '#0f172a', fontSize: typography.md, fontWeight: 700, marginBottom: 4 }}>
                        {selectedHistoryRun ? `回测 #${selectedHistoryRun.id} · 实验摘要` : '实验摘要'}
                      </div>
                      <div style={{ color: '#64748b', fontSize: 12, marginBottom: 12 }}>
                        只读查看本次实验的参数快照与结论；复制后可作为新的实验继续研究。
                      </div>
                        {selectedHistoryRun ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, color: '#334155', fontSize: 12, lineHeight: 1.6 }}>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                              <div>方案：{selectedHistoryRun.configId ? (configNameById.get(selectedHistoryRun.configId) || `方案 ${selectedHistoryRun.configId}`) : '未绑定'}</div>
                              <div>策略快照：{selectedHistoryRun.scriptSnapshot ? '已保存' : '--'}</div>
                              <div>脚本：{selectedHistoryRun.scriptId ?? '--'}</div>
                              <div>标的：{selectedHistoryRun.symbol}</div>
                              <div>运行周期：{selectedHistoryRun.timeframe || '5m'} K 线</div>
                              <div>基准：{selectedHistoryRun.benchmarkSymbol || '-'}</div>
                              <div>区间：{selectedHistoryRun.startDate}-{selectedHistoryRun.endDate}</div>
                              <div>状态：{selectedHistoryRun.status}</div>
                            </div>
                            {selectedHistoryRun.resultJson?.experimentManifest ? (
                              <div style={{ padding: '8px 10px', borderRadius: 4, background: '#f1f5f9', color: '#475569' }}>
                                实验指纹：{selectedHistoryRun.resultJson.experimentManifest.experimentHash.slice(0, 12)} · {selectedHistoryRun.resultJson.experimentManifest.barCount} 根 {selectedHistoryRun.resultJson.experimentManifest.barRange.timeframe} K 线
                              </div>
                            ) : (
                              <div style={{ color: '#b45309' }}>旧记录未保存实验清单，无法完整复现。</div>
                            )}
                            <HistoryRunSnapshot run={selectedHistoryRun} />
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8, marginTop: 4 }}>
                              <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 8 }}>
                                <div style={{ color: '#64748b', fontSize: 11 }}>总收益率</div>
                                <div style={{ fontWeight: 700, marginTop: 4 }}>{formatOptionalPct(selectedHistoryRun.summaryJson.totalReturn)}</div>
                              </div>
                              <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 8 }}>
                                <div style={{ color: '#64748b', fontSize: 11 }}>最大回撤</div>
                                <div style={{ fontWeight: 700, marginTop: 4 }}>{formatOptionalPct(selectedHistoryRun.summaryJson.maxDrawdownRate)}</div>
                              </div>
                              <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 8 }}>
                                <div style={{ color: '#64748b', fontSize: 11 }}>Sharpe</div>
                                <div style={{ fontWeight: 700, marginTop: 4 }}>{formatOptionalNumber(selectedHistoryRun.summaryJson.annualSharpe)}</div>
                              </div>
                              <div style={{ border: '1px solid #e2e8f0', borderRadius: 4, padding: 8 }}>
                                <div style={{ color: '#64748b', fontSize: 11 }}>交易次数</div>
                                <div style={{ fontWeight: 700, marginTop: 4 }}>{formatOptionalNumber(selectedHistoryRun.summaryJson.tradeCount)}</div>
                              </div>
                            </div>
                            <label>
                              <div style={labelStyle}>备注</div>
                              <textarea
                                value={historyNoteDraft}
                                onChange={(e) => setHistoryNoteDraft(e.target.value)}
                                rows={3}
                                style={{
                                  ...fieldStyle,
                                  height: 'auto',
                                  minHeight: 76,
                                  padding: 10,
                                  resize: 'vertical',
                                  lineHeight: 1.5,
                                }}
                              />
                            </label>
                            <label>
                              <div style={labelStyle}>标签</div>
                              <input
                                value={historyTagsDraft}
                                onChange={(e) => setHistoryTagsDraft(e.target.value)}
                                placeholder="候选, 调参, 保留"
                                style={fieldStyle}
                              />
                            </label>
                            <div style={{ color: '#64748b', fontSize: 11, lineHeight: 1.5 }}>
                              用逗号分隔多个标签，保存后会写回数据库。
                            </div>
                            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            <button
                              onClick={() => handleViewHistoryResult(selectedHistoryRun)}
                              style={{
                                height: 34,
                                padding: '0 12px',
                                borderRadius: 4,
                                border: '1px solid #0f766e',
                                background: '#ccfbf1',
                                color: '#0f766e',
                                cursor: 'pointer',
                              }}
                            >
                              查看完整结果
                            </button>
                            <button
                              onClick={() => handleRerunHistory(selectedHistoryRun)}
                              disabled={running}
                              style={{ height: 34, padding: '0 12px', borderRadius: 4, border: '1px solid #0f766e', background: '#ffffff', color: '#0f766e', cursor: running ? 'wait' : 'pointer', opacity: running ? 0.65 : 1 }}
                            >
                              {running ? '运行中...' : '重新运行此回测'}
                            </button>
                            <button
                              onClick={() => {
                                handleApplyBacktestRun(selectedHistoryRun);
                                setActivePanel('overview');
                              }}
                              style={{ height: 34, padding: '0 12px', borderRadius: 4, border: '1px solid #cbd5e1', background: '#f8fafc', color: '#334155', cursor: 'pointer' }}
                            >
                              新建回测（复制参数）
                            </button>
                            <button
                              onClick={handleSaveHistoryMeta}
                              style={{
                                height: 34,
                                width: 120,
                                borderRadius: 4,
                                border: '1px solid #cbd5e1',
                                background: '#f8fafc',
                                color: '#334155',
                                cursor: 'pointer',
                              }}
                            >
                              保存备注
                            </button>
                            </div>
                          </div>
                        ) : (
                          <div style={{ color: '#64748b', fontSize: 12 }}>选择一条历史记录查看详情。</div>
                        )}
                      </div>

                      <div style={{ ...panelStyle, padding: 12, background: '#ffffff' }}>
                        <div style={{ color: '#0f172a', fontSize: typography.md, fontWeight: 700, marginBottom: 10 }}>参数对比</div>
                        {backtestRuns.length > 1 ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                            <label>
                              <div style={labelStyle}>对比对象</div>
                              <select
                                value={compareHistoryRunId}
                                onChange={(e) => setCompareHistoryRunId(e.target.value)}
                                style={fieldStyle}
                              >
                                {backtestRuns
                                  .filter((item) => item.id !== selectedHistoryRunId)
                                  .map((item) => (
                                    <option key={item.id} value={item.id}>
                                      #{item.id} · {item.symbol} · {item.startDate}-{item.endDate}
                                    </option>
                                  ))}
                              </select>
                            </label>
                            {selectedHistoryRun && compareHistoryRun && (
                              <div style={{ overflow: 'auto', border: '1px solid #e2e8f0', borderRadius: 6 }}>
                                <div style={{ display: 'grid', gridTemplateColumns: '130px 1fr 1fr 1fr', gap: 8, padding: '8px 10px', background: '#f8fafc', color: '#64748b', fontSize: 11, fontWeight: 700 }}>
                                  <span>结果指标</span>
                                  <span>回测 #{selectedHistoryRun.id}</span>
                                  <span>回测 #{compareHistoryRun.id}</span>
                                  <span>差异</span>
                                </div>
                                {[
                                  ['总收益率', selectedHistoryRun.summaryJson.totalReturn, compareHistoryRun.summaryJson.totalReturn, formatOptionalPct],
                                  ['最大回撤', selectedHistoryRun.summaryJson.maxDrawdownRate, compareHistoryRun.summaryJson.maxDrawdownRate, formatOptionalPct],
                                  ['Sharpe', selectedHistoryRun.summaryJson.annualSharpe, compareHistoryRun.summaryJson.annualSharpe, formatOptionalNumber],
                                  ['信号次数', selectedHistoryRun.summaryJson.tradeCount, compareHistoryRun.summaryJson.tradeCount, formatOptionalNumber],
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
                    </div>
                  </div>
                </section>
              )}

              {activePanel === 'positions' && (
                <section style={{ ...panelStyle, padding: 16, background: '#ffffff', minHeight: 320 }}>
                  <div style={{ color: '#0f172a', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
                    持仓收益
                  </div>
                  {dailyHoldings.length > 0 ? (
                    <div style={{ overflow: 'auto', border: '1px solid #d7dde8', borderRadius: 6 }}>
                      <div style={{ display: 'grid', gridTemplateColumns: '110px 90px 80px 80px 110px 120px 120px 120px 120px 110px', gap: 0, minWidth: 1040, background: '#f8fafc', color: '#64748b', fontSize: 12, borderBottom: '1px solid #d7dde8' }}>
                        {['日期', '收盘', '多仓', '空仓', '现金', '多头市值', '空头市值', '净市值', '总资产', '日盈亏'].map((label) => (
                          <div key={label} style={{ padding: '10px 12px', fontWeight: 700 }}>{label}</div>
                        ))}
                      </div>
                      {dailyHoldings.map((item) => (
                        <div key={item.date} style={{ display: 'grid', gridTemplateColumns: '110px 90px 80px 80px 110px 120px 120px 120px 120px 110px', minWidth: 1040, color: '#334155', fontSize: 12, borderBottom: '1px solid #e2e8f0' }}>
                          <div style={{ padding: '9px 12px' }}>{item.date}</div>
                          <div style={{ padding: '9px 12px' }}>{formatNumber(item.close)}</div>
                          <div style={{ padding: '9px 12px' }}>{item.longQty}</div>
                          <div style={{ padding: '9px 12px' }}>{item.shortQty}</div>
                          <div style={{ padding: '9px 12px' }}>{formatMoney(item.cash)}</div>
                          <div style={{ padding: '9px 12px' }}>{formatMoney(item.longMarketValue)}</div>
                          <div style={{ padding: '9px 12px' }}>{formatMoney(item.shortMarketValue)}</div>
                          <div style={{ padding: '9px 12px' }}>{formatMoney(item.marketValue)}</div>
                          <div style={{ padding: '9px 12px', fontWeight: 600 }}>{formatMoney(item.totalEquity)}</div>
                          <div style={{ padding: '9px 12px', color: getPnlColor(item.dailyPnl) }}>{formatMoney(item.dailyPnl)}</div>
                        </div>
                      ))}
                    </div>
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
                    <div>回测记录：{result?.runId ? `#${result.runId}` : '--'}</div>
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

          <div style={{
            border: '1px solid #d7dde8',
            borderRadius: 6,
            padding: 10,
            background: '#ffffff',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}>
            <div style={labelStyle}>执行模式</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {[
                ['backtest', '回测'],
                ['live', '运行'],
              ].map(([value, label]) => (
                <button
                  key={value}
                  onClick={() => setMode(value as 'backtest' | 'live')}
                  style={{
                    height: 32,
                    borderRadius: 4,
                    border: `1px solid ${mode === value ? '#0f766e' : '#cbd5e1'}`,
                    background: mode === value ? '#ccfbf1' : '#f8fafc',
                    color: mode === value ? '#0f766e' : '#64748b',
                    cursor: 'pointer',
                  }}
                >
                  {label}
                </button>
              ))}
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
            <label>
              <div style={labelStyle}>策略代码</div>
              <select
                value={selectedScriptId}
                onChange={(e) => handleScriptSelect(e.target.value)}
                style={fieldStyle}
              >
                {selectedScriptId === '' && (
                  <option value="" disabled>新建策略（未保存）</option>
                )}
                {strategyScripts.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.status}
                  </option>
                ))}
              </select>
            </label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
              <button
                onClick={handleNewScript}
                style={{
                  height: 32, borderRadius: 4, border: '1px solid #cbd5e1',
                  background: '#f8fafc', color: '#334155', cursor: 'pointer',
                }}
              >
                新建
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
                保存后可绑定为“自定义脚本”运行策略。脚本必须实现 on_bar(bar, state)，不得 import 或访问本地文件。
              </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label>
              <div style={labelStyle}>开始日期</div>
              <input
                value={params.startDate}
                onChange={(e) => setParam('startDate', e.target.value)}
                style={fieldStyle}
              />
            </label>
            <label>
              <div style={labelStyle}>结束日期</div>
              <input
                value={params.endDate}
                onChange={(e) => setParam('endDate', e.target.value)}
                style={fieldStyle}
              />
            </label>
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
            <label>
              <div style={labelStyle}>手续费率</div>
              <input
                value={params.commissionRate}
                onChange={(e) => setParam('commissionRate', e.target.value)}
                style={fieldStyle}
              />
              <div style={{ color: '#b45309', fontSize: 11, marginTop: 5 }}>
                当前仅保存到记录，尚未影响引擎成本
              </div>
            </label>
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
              {running ? '运行中...' : mode === 'live' ? '启动运行' : '运行回测'}
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


