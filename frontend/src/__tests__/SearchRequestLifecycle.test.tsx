import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiResponseError,
  fetchBacktestRunFilterOptions,
  fetchBacktestRunOptions,
  fetchBacktestRunPage,
  fetchJson,
} from '../api/client';


describe('search request lifecycle', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('passes AbortSignal and sends cursor pagination without offset', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        runs: [], total: null, selectableTotal: null, nextCursor: null,
        limit: 20, offset: 0, symbols: [],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    await fetchBacktestRunPage({
      limit: 20,
      cursor: 'next-cursor',
      includeTotals: false,
      query: '候选',
    }, { signal: controller.signal });

    const [url, init] = fetchMock.mock.calls[0];
    const requestedUrl = new URL(String(url), 'http://local');
    expect(Object.fromEntries(requestedUrl.searchParams)).toEqual({
      limit: '20',
      cursor: 'next-cursor',
      includeTotals: 'false',
      includeFilterOptions: 'false',
      query: '候选',
    });
    expect(init.signal).toBe(controller.signal);
  });

  it('uses dedicated filter-option and lightweight comparison endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ symbols: ['688981.SH'] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ items: [], nextCursor: null, limit: 20 }),
      });
    vi.stubGlobal('fetch', fetchMock);

    await fetchBacktestRunFilterOptions();
    await fetchBacktestRunOptions({ query: '#42', limit: 20 });

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/backtest/run-filter-options');
    expect(String(fetchMock.mock.calls[1][0])).toContain('/api/backtest/run-options?');
  });

  it('converts an HTML gateway timeout to a stable Chinese error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 504,
      headers: new Headers({ 'Content-Type': 'text/html' }),
      text: async () => '<html><body>Gateway Timeout</body></html>',
    }));

    const error = await fetchJson('/api/backtest/runs').catch((reason) => reason) as ApiResponseError;

    expect(error).toBeInstanceOf(ApiResponseError);
    expect(error.message).toBe('搜索服务暂时未能及时响应，请缩小条件或稍后重试。');
    expect(error.message).not.toContain('<html>');
  });

  it('preserves structured timeout details returned by the API', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 504,
      headers: new Headers({ 'Content-Type': 'application/json' }),
      text: async () => JSON.stringify({
        detail: { code: 'search_timeout', message: '查询超时，请缩小搜索范围' },
        requestId: 'request-42',
      }),
    }));

    const error = await fetchJson('/api/backtest/runs').catch((reason) => reason) as ApiResponseError;

    expect(error.code).toBe('search_timeout');
    expect(error.message).toBe('查询超时，请缩小搜索范围');
    expect(error.requestId).toBe('request-42');
  });
});
