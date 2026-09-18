import type { BacktestTrade } from '../types';


const CLOSED_TRADE_ACTION_PATTERN = /(CLOSE|COVER|STOP|FORCE)/;
const OVERLAY_CLOSE_EFFECTS = new Set(['partial_close', 'close', 'reverse']);

export function getClosedLoopTradePnl(
  trade: BacktestTrade,
  isOverlayResult: boolean,
): number | null {
  if (trade.status && trade.status !== 'filled') return null;
  if (isOverlayResult) {
    if (!OVERLAY_CLOSE_EFFECTS.has(trade.cycleEffect || '')) return null;
    return typeof trade.realizedOverlayPnl === 'number'
      && Number.isFinite(trade.realizedOverlayPnl)
      ? trade.realizedOverlayPnl
      : null;
  }
  const action = String(trade.action || trade.direction).toUpperCase();
  if (!CLOSED_TRADE_ACTION_PATTERN.test(action) && trade.pnl === 0) return null;
  return Number.isFinite(trade.pnl) ? trade.pnl : null;
}

export function formatSignalTime(value: string): string {
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

export function signalDetailCsvText(
  trades: BacktestTrade[],
  isOverlayResult: boolean,
): string {
  if (trades.length === 0) return '';
  const rows = trades.map((trade) => ({
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
  }));
  const headers = Object.keys(rows[0]) as Array<keyof typeof rows[number]>;
  const escapeCell = (value: string | number) => `"${String(value).replace(/"/g, '""')}"`;
  return '\ufeff' + [
    headers.join(','),
    ...rows.map((row) => headers.map((header) => escapeCell(row[header])).join(',')),
  ].join('\n');
}

export function downloadSignalDetailCsv(
  filename: string,
  trades: BacktestTrade[],
  isOverlayResult: boolean,
): void {
  const csv = signalDetailCsvText(trades, isOverlayResult);
  if (!csv) return;
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
