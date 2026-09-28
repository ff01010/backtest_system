import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiResponseError, fetchChart, fetchDataStatus } from '../api/client';

describe('chart API errors', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('只向页面暴露结构化行情错误提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: {
        errorDetail: {
          category: 'data',
          code: 'data.bar_load_failed',
          message: '行情数据异常，请联系管理员',
        },
      },
    }), { status: 503, headers: { 'Content-Type': 'application/json' } })));

    await expect(fetchChart('688981.SH', '2026-07-30', '5m')).rejects.toMatchObject({
      name: 'ApiResponseError',
      code: 'data.bar_load_failed',
      message: '行情数据异常，请联系管理员',
    } satisfies Partial<ApiResponseError>);
  });

  it('L2 范围查询显式携带撮合方式', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: {
        symbol: '688981.SH', timeframe: '5m', path: '', exists: false,
        firstDate: null, lastDate: null, barCount: 0, fileSize: 0, mtime: null,
        duplicateCount: 0, nonTradingCount: 0, gapDates: [], preview: [],
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    await fetchDataStatus('688981.SH', '5m', 'l2_quote');

    expect(String(fetchMock.mock.calls[0][0])).toContain('fillModel=l2_quote');
  });
});
