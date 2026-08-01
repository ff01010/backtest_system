import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import type { DateItem } from '../types';
import { typography } from '../styles/typography';

interface Props {
  symbol: string;
  date: string;
  timeframe: string;
  dates: DateItem[];
  loading: boolean;
  backtestDisplayNo?: number;
  onDateChange: (date: string) => void;
  onTimeframeChange: (timeframe: string) => void;
  onPrevDay: () => void;
  onNextDay: () => void;
  onRefresh: () => void;
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '60m', '1d'];

function formatDate(value: string): string {
  if (value.length !== 8) return value;
  return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
}

export default function Toolbar({
  symbol,
  date,
  timeframe,
  dates,
  loading,
  backtestDisplayNo,
  onDateChange,
  onTimeframeChange,
  onPrevDay,
  onNextDay,
  onRefresh,
}: Props) {
  const dateIndex = dates.findIndex((item) => item.date === date);
  const canPrev = dateIndex < dates.length - 1;
  const canNext = dateIndex > 0;

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8, minHeight: 52, padding: '8px 16px',
      background: 'var(--surface)', borderBottom: '1px solid var(--border)', flexWrap: 'wrap',
    }}>
      <span style={{ color: 'var(--text)', fontFamily: 'Consolas, monospace', fontSize: typography.md, fontWeight: 700 }}>
        {symbol}
      </span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <button
          type="button"
          onClick={onPrevDay}
          disabled={!canPrev}
          aria-label="上一交易日"
          style={navButtonStyle(canPrev)}
        >
          <ChevronLeft size={15} />
        </button>
        <select value={date} onChange={(event) => onDateChange(event.target.value)} style={fieldStyle}>
          {dates.map((item) => <option key={item.date} value={item.date}>{formatDate(item.date)}</option>)}
        </select>
        <button
          type="button"
          onClick={onNextDay}
          disabled={!canNext}
          aria-label="下一交易日"
          style={navButtonStyle(canNext)}
        >
          <ChevronRight size={15} />
        </button>
      </div>
      <select value={timeframe} onChange={(event) => onTimeframeChange(event.target.value)} style={fieldStyle}>
        {TIMEFRAMES.map((item) => <option key={item} value={item}>{item}</option>)}
      </select>
      {backtestDisplayNo != null && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ background: 'var(--chrome)', borderRadius: 3, color: '#ffffff', fontSize: typography.sm, padding: '6px 9px' }}>
            回测 #{backtestDisplayNo} 成交点
          </span>
        </div>
      )}
      <button type="button" onClick={onRefresh} disabled={loading} aria-label="刷新 K 线" style={navButtonStyle(!loading)}>
        <RefreshCw size={15} style={loading ? { animation: 'spin 1s linear infinite' } : undefined} />
      </button>
    </div>
  );
}

function navButtonStyle(enabled: boolean): Record<string, string | number> {
  return {
    width: 32,
    height: 32,
    border: '1px solid var(--border-strong)',
    borderRadius: 3,
    background: 'var(--surface)',
    color: enabled ? 'var(--text-muted)' : 'var(--text-subtle)',
    cursor: enabled ? 'pointer' : 'not-allowed',
    opacity: enabled ? 1 : 0.45,
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
  };
}

const fieldStyle = {
  height: 32,
  minWidth: 116,
  border: '1px solid var(--border-strong)',
  borderRadius: 3,
  background: 'var(--surface-muted)',
  color: 'var(--text)',
  fontFamily: 'Consolas, monospace',
  fontSize: typography.base,
  padding: '0 8px',
} as const;
