import { describe, expect, it } from 'vitest';
import {
  buildTimeframeAvailabilityCacheKey,
  shouldFetchBacktestTimeframes,
} from '../components/BacktestPage';

describe('shouldFetchBacktestTimeframes', () => {
  it('仅在两个日期均完整时查询可用周期', () => {
    expect(shouldFetchBacktestTimeframes('20260601', '20260626')).toBe(true);
    expect(shouldFetchBacktestTimeframes('20260601', '2026061')).toBe(false);
    expect(shouldFetchBacktestTimeframes('', '20260626')).toBe(false);
  });

  it('将同一组标的生成稳定的周期可用性缓存键', () => {
    expect(buildTimeframeAvailabilityCacheKey(['688981.SH', '002747.SZ'], '20260601', '20260626'))
      .toBe(buildTimeframeAvailabilityCacheKey(['002747.SZ', '688981.SH'], '20260601', '20260626'));
  });
});
