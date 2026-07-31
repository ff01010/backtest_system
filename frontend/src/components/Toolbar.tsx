import { ChevronLeft, ChevronRight, RefreshCw, DownloadCloud } from 'lucide-react';
import type { InstrumentItem, DateItem } from '../types';
import type { SignalFileItem } from '../api/client';
import { typography } from '../styles/typography';

interface Props {
  symbol: string;
  date: string;
  strategy: string;
  strategies: string[];
  signalFile: string;
  signalFiles: SignalFileItem[];
  instruments: InstrumentItem[];
  dates: DateItem[];
  loading: boolean;
  syncing: boolean;
  onSymbolChange: (symbol: string) => void;
  onDateChange: (date: string) => void;
  onStrategyChange: (strategy: string) => void;
  onSignalFileChange: (name: string) => void;
  onPrevDay: () => void;
  onNextDay: () => void;
  onRefresh: () => void;
  onSync: () => void;
}

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

export default function Toolbar({
  symbol, date, strategy, strategies, signalFile, signalFiles,
  instruments, dates, loading, syncing,
  onSymbolChange, onDateChange, onStrategyChange, onSignalFileChange,
  onPrevDay, onNextDay, onRefresh, onSync,
}: Props) {
  const dateIndex = dates.findIndex((d) => d.date === date);
  const canPrev = dateIndex < dates.length - 1;
  const canNext = dateIndex > 0;

  const formatDate = (d: string) => {
    if (d.length !== 8) return d;
    return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
  };

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: '12px', padding: '10px 16px',
      background: 'var(--surface)', borderBottom: '1px solid var(--border)',
      flexWrap: 'wrap',
    }}>
      {/* Symbol selector */}
        <select
          value={symbol}
        onChange={(e) => onSymbolChange(e.target.value)}
          style={{
            background: 'var(--surface-muted)', color: 'var(--text)', border: '1px solid var(--border-strong)',
            borderRadius: '4px', padding: '6px 10px', fontSize: typography.base,
            fontFamily: 'Consolas, monospace', minWidth: '140px',
          }}
      >
        {instruments.filter((i) => i.hasKline).map((i) => (
          <option key={i.symbol} value={i.symbol}>
            {i.symbol}{i.hasSignal ? '' : ' (无信号)'}
          </option>
        ))}
      </select>

      {/* Date navigation */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
        <button
          onClick={onPrevDay} disabled={!canPrev}
          style={{
            background: 'var(--surface-muted)', color: canPrev ? 'var(--text-muted)' : '#b9c4ce',
            border: '1px solid var(--border-strong)', borderRadius: '4px',
            padding: '6px 10px', cursor: canPrev ? 'pointer' : 'default',
            fontSize: typography.base,
          }}
        >
          <ChevronLeft size={15} />
        </button>

        <select
          value={date}
          onChange={(e) => onDateChange(e.target.value)}
          style={{
            background: 'var(--surface-muted)', color: 'var(--text)', border: '1px solid var(--border-strong)',
            borderRadius: '4px', padding: '6px 10px', fontSize: typography.md,
            fontFamily: 'Consolas, monospace', minWidth: '120px',
          }}
        >
          {dates.map((d) => (
            <option key={d.date} value={d.date}>
              {formatDate(d.date)} {d.hasSignal ? '' : '(无信号)'}
            </option>
          ))}
        </select>

        <button
          onClick={onNextDay} disabled={!canNext}
          style={{
            background: 'var(--surface-muted)', color: canNext ? 'var(--text-muted)' : '#b9c4ce',
            border: '1px solid var(--border-strong)', borderRadius: '4px',
            padding: '6px 10px', cursor: canNext ? 'pointer' : 'default',
            fontSize: typography.base,
          }}
        >
          <ChevronRight size={15} />
        </button>
      </div>

      {/* Timeframe */}
      <span style={{
        background: 'var(--surface-muted)', color: 'var(--text-muted)', border: '1px solid var(--border-strong)',
        borderRadius: '4px', padding: '6px 10px', fontSize: typography.base,
        fontFamily: 'Consolas, monospace',
      }}>
        5m
      </span>

      {/* Check K-line coverage */}
      <button
        onClick={onSync}
        disabled={syncing}
        title="检查 QMT K 线覆盖"
        style={{
          background: 'var(--surface-muted)', color: syncing ? 'var(--warning)' : 'var(--text-muted)',
          border: '1px solid var(--border-strong)', borderRadius: '4px',
          padding: '6px 10px', cursor: syncing ? 'wait' : 'pointer',
          display: 'flex', alignItems: 'center', gap: '4px',
        }}
      >
        <DownloadCloud size={14} style={syncing ? { animation: 'pulse 0.6s ease-in-out infinite' } : undefined} />
        {syncing ? '检查中...' : '检查覆盖'}
      </button>

      {/* Refresh */}
      <button
        onClick={onRefresh}
        disabled={loading}
        style={{
          background: 'var(--surface-muted)', color: 'var(--text-muted)', border: '1px solid var(--border-strong)',
          borderRadius: '4px', padding: '6px 10px', cursor: 'pointer',
          display: 'flex', alignItems: 'center', gap: '4px',
        }}
      >
        <RefreshCw size={14} style={loading ? { animation: 'spin 1s linear infinite' } : undefined} />
      </button>

      {/* Strategy selector */}
      <select
        value={strategy}
        onChange={(e) => onStrategyChange(e.target.value)}
        style={{
          background: 'var(--surface-muted)', color: 'var(--text)', border: '1px solid var(--border-strong)',
          borderRadius: '4px', padding: '6px 10px', fontSize: typography.sm,
          fontFamily: 'Consolas, monospace',
        }}
      >
        {strategies.map((s) => (
          <option key={s} value={s}>{formatStrategyName(s)}</option>
        ))}
      </select>

      {/* Signal file selector */}
      <select
        value={signalFile}
        onChange={(e) => onSignalFileChange(e.target.value)}
        style={{
          background: 'var(--surface-muted)', color: signalFile ? 'var(--warning)' : 'var(--text-muted)',
          border: '1px solid var(--border-strong)', borderRadius: '4px',
          padding: '6px 10px', fontSize: typography.xs,
          fontFamily: 'Consolas, monospace', maxWidth: '220px',
        }}
      >
        <option value="">信号文件 (自动)</option>
        {signalFiles.map((f) => (
          <option key={f.name} value={f.name}>{f.name}</option>
        ))}
      </select>

      {/* Symbol name */}
      <span style={{ color: 'var(--text-subtle)', fontSize: typography.base, marginLeft: 'auto' }}>
        {symbol}
      </span>
    </div>
  );
}
