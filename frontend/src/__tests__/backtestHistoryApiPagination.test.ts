import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchBacktestRunPage } from '../api/client';


describe('backtest history API pagination', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests records after the first thirty with server-side filters', async () => {
    const response = {
      runs: [{ id: 'run-31', displayNo: 31 }],
      total: 65,
      limit: 20,
      offset: 40,
      symbols: ['688981.SH'],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => response,
    });
    vi.stubGlobal('fetch', fetchMock);

    const page = await fetchBacktestRunPage({
      limit: 20,
      offset: 40,
      query: '候选',
      symbol: '688981.SH',
    });

    const requestedUrl = new URL(String(fetchMock.mock.calls[0][0]), 'http://local');
    expect(requestedUrl.pathname).toBe('/api/backtest/runs');
    expect(Object.fromEntries(requestedUrl.searchParams)).toEqual({
      limit: '20',
      offset: '40',
      query: '候选',
      symbol: '688981.SH',
    });
    expect(page).toEqual(response);
  });
});
