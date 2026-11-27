import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import type { DateItem } from '../types';

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
    <div className="signal-toolbar">
      <span className="signal-symbol">{symbol}</span>
      <div className="signal-nav-group">
        <button
          type="button"
          className="signal-icon-button"
          onClick={onPrevDay}
          disabled={!canPrev}
          aria-label="上一交易日"
        >
          <ChevronLeft size={15} />
        </button>
        <select
          className="signal-select"
          aria-label="交易日"
          value={date}
          onChange={(event) => onDateChange(event.target.value)}
        >
          {dates.map((item) => <option key={item.date} value={item.date}>{formatDate(item.date)}</option>)}
        </select>
        <button
          type="button"
          className="signal-icon-button"
          onClick={onNextDay}
          disabled={!canNext}
          aria-label="下一交易日"
        >
          <ChevronRight size={15} />
        </button>
      </div>
      <select
        className="signal-select signal-select-compact"
        aria-label="行情周期"
        value={timeframe}
        onChange={(event) => onTimeframeChange(event.target.value)}
      >
        {TIMEFRAMES.map((item) => <option key={item} value={item}>{item}</option>)}
      </select>
      {backtestDisplayNo != null && (
        <span className="signal-run-chip">回测 #{backtestDisplayNo} 成交点</span>
      )}
      <button
        type="button"
        className="signal-icon-button"
        onClick={onRefresh}
        disabled={loading}
        aria-label="刷新 K 线"
      >
        <RefreshCw size={15} style={loading ? { animation: 'spin 1s linear infinite' } : undefined} />
      </button>
    </div>
  );
}
