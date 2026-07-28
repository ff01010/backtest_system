import { describe, expect, it } from 'vitest';

import { formatChartTimestamp, toChartTimestamp } from '../utils/chartTime';


describe('chart time', () => {
  it('keeps exchange wall-clock time independent of local timezone', () => {
    const timestamp = toChartTimestamp('2026-06-22T09:35:00');
    expect(formatChartTimestamp(timestamp)).toBe('09:35');
  });

  it('rejects invalid datetime strings', () => {
    expect(() => toChartTimestamp('20260622093500')).toThrow('无效图表时间');
  });
});
