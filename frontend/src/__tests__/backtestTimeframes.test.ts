import { describe, expect, it } from 'vitest';
import {
  BACKTEST_TIMEFRAMES,
  buildDataRangeMetadata,
  buildTimeframeAvailabilityCacheKey,
  normalizeApiDate,
  resolveRangeAvailability,
} from '../components/BacktestPage';
import type { DataStatus } from '../types';

describe('backtest timeframe metadata', () => {
  it('固定展示平台支持周期，不混入日期范围可用性', () => {
    expect(BACKTEST_TIMEFRAMES).toEqual([
      '1m', '3m', '5m', '15m', '30m', '45m', '60m', '1d',
    ]);
  });

  it('只展示元数据中的可回测起止日期，不解释区间缺口', () => {
    const status: DataStatus = {
      symbol: '688981.SH',
      timeframe: '5m',
      fillModel: 'bar_next_open',
      path: '',
      exists: true,
      firstDate: '2020-01-02',
      lastDate: '2026-09-18',
      barCount: 100,
      fileSize: 0,
      mtime: null,
      duplicateCount: 0,
      nonTradingCount: 0,
      gapDates: [{ date: '2026-07-30', barCount: 0, expected: 48, missing: 48 }],
      preview: [],
    };

    expect(buildDataRangeMetadata('运行标的', '5m', resolveRangeAvailability(status, false, ''))).toEqual({
      tone: 'ok',
      text: '运行标的 5m 可回测范围：2020-01-02-2026-09-18',
    });
  });

  it('范围接口失败时只显示统一行情异常', () => {
    expect(buildDataRangeMetadata(
      '运行标的', '5m', resolveRangeAvailability(null, false, 'internal table timeout'),
    )).toEqual({
      tone: 'warn',
      text: '行情数据异常，请联系管理员',
    });
  });

  it('distinguishes a finished L2 range query with no data from loading', () => {
    const status: DataStatus = {
      symbol: '688449.SH',
      timeframe: '1m',
      fillModel: 'l2_quote',
      path: '',
      exists: false,
      firstDate: null,
      lastDate: null,
      barCount: 0,
      fileSize: 0,
      mtime: null,
      duplicateCount: 0,
      nonTradingCount: 0,
      gapDates: [],
      preview: [],
    };

    const availability = resolveRangeAvailability(status, false, '');
    expect(availability).toEqual({ state: 'empty' });
    expect(buildDataRangeMetadata('运行标的', 'L2', availability)).toEqual({
      tone: 'warn',
      text: '运行标的 L2 暂无可回测数据',
    });
    expect(resolveRangeAvailability(null, false, '')).toEqual({ state: 'loading' });
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
