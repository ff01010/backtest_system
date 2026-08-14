import { describe, expect, it } from 'vitest';
import {
  buildTimeframeAvailabilityCacheKey,
  normalizeApiDate,
  shouldFetchBacktestTimeframes,
} from '../components/BacktestPage';

describe('shouldFetchBacktestTimeframes', () => {
  it('仅在两个日期均完整时查询可用周期', () => {
    expect(shouldFetchBacktestTimeframes('2026-06-01', '2026-06-26')).toBe(true);
    expect(shouldFetchBacktestTimeframes('2026-06-01', '2026061')).toBe(false);
    expect(shouldFetchBacktestTimeframes('', '2026-06-26')).toBe(false);
  });

  it('将同一组标的生成稳定的周期可用性缓存键', () => {
    expect(buildTimeframeAvailabilityCacheKey(['688981.SH', '002747.SZ'], '2026-06-01', '2026-06-26'))
      .toBe(buildTimeframeAvailabilityCacheKey(['002747.SZ', '688981.SH'], '2026-06-01', '2026-06-26'));
  });

  it('将成交时间派生的紧凑交易日转换为信号页使用的 ISO 日期', () => {
    expect(normalizeApiDate('20260506')).toBe('2026-05-06');
    expect(normalizeApiDate('2026-05-06')).toBe('2026-05-06');
  });
});
