import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BacktestPage from '../components/BacktestPage';
import type { BacktestRunPage } from '../api/client';
import type { BacktestRunHistory } from '../types';


const apiMocks = vi.hoisted(() => ({
  fetchBacktestRunPage: vi.fn(),
  fetchBacktestRuns: vi.fn(),
  fetchBacktestRun: vi.fn(),
  fetchStrategyConfigs: vi.fn(),
  fetchResearchSignalDatasets: vi.fn(),
  fetchDataStatus: vi.fn(),
  fetchBacktestTimeframes: vi.fn(),
  fetchBacktestPreflight: vi.fn(),
  searchMarketInstruments: vi.fn(),
}));

vi.mock('../api/client', () => ({
  ...apiMocks,
  BacktestRequestError: class BacktestRequestError extends Error {},
  cancelBacktestRun: vi.fn(),
  createAccount: vi.fn(),
  deleteBacktestRun: vi.fn(),
  deleteBacktestRuns: vi.fn(),
  deleteStrategyConfig: vi.fn(),
  fetchAccounts: vi.fn().mockResolvedValue([]),
  fetchStrategyConfigVersions: vi.fn().mockResolvedValue([]),
  fetchStrategyParameterSchema: vi.fn(),
  fetchStrategySource: vi.fn(),
  runBacktest: vi.fn(),
  saveCompleteStrategy: vi.fn(),
  updateBacktestRunMeta: vi.fn(),
}));

function makeRun(displayNo: number): BacktestRunHistory {
  return {
    id: `run-${displayNo}`,
    displayNo,
    configId: null,
    configVersionId: null,
    scriptId: null,
    accountId: null,
    symbol: '688981.SH',
    timeframe: '5m',
    fillModel: 'bar_close_signal_fill',
    scriptSnapshot: null,
    paramsSnapshot: {},
    dataVersion: null,
    benchmarkSymbol: null,
    strategy: 'user_script',
    strategyCategory: 'directional_holding',
    startDate: '2026-01-01',
    endDate: '2026-01-31',
    paramsJson: {},
    status: 'ok',
    summaryJson: {},
    outputDir: '',
    note: '',
    tags: [],
    queuedAt: '',
    startedAt: '',
    cancelRequested: false,
    resultJson: null,
    createdAt: `2026-01-${String(Math.min(displayNo, 31)).padStart(2, '0')}T00:00:00+08:00`,
    finishedAt: '',
  };
}

describe('BacktestPage history pagination', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const history = Array.from({ length: 35 }, (_, index) => makeRun(35 - index));
    apiMocks.fetchBacktestRuns.mockResolvedValue(history.slice(0, 10));
    apiMocks.fetchBacktestRun.mockResolvedValue(history[0]);
    apiMocks.fetchBacktestRunPage.mockImplementation(async ({ limit, offset, query }) => {
      const filtered = query === '候选' || query === 'run-1' ? [history[34]] : history;
      return {
        runs: filtered.slice(offset, offset + limit),
        total: filtered.length,
        limit,
        offset,
        symbols: ['688981.SH'],
      };
    });
    apiMocks.fetchStrategyConfigs.mockResolvedValue([]);
    apiMocks.fetchResearchSignalDatasets.mockResolvedValue([]);
    apiMocks.fetchDataStatus.mockRejectedValue(new Error('测试环境无行情'));
    apiMocks.fetchBacktestTimeframes.mockResolvedValue([]);
    apiMocks.fetchBacktestPreflight.mockResolvedValue(null);
    apiMocks.searchMarketInstruments.mockResolvedValue([]);
  });

  it('can navigate to records after the first thirty', async () => {
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    expect(await screen.findByText('显示 1-10 / 35 条实验')).toBeTruthy();

    for (const expectedPage of [2, 3, 4]) {
      fireEvent.click(screen.getByRole('button', { name: '下一页' }));
      await waitFor(() => {
        expect(screen.getByText(`${expectedPage} / 4`)).toBeTruthy();
      });
    }

    expect(screen.getByText('显示 31-35 / 35 条实验')).toBeTruthy();
    expect(apiMocks.fetchBacktestRunPage).toHaveBeenLastCalledWith({
      limit: 10,
      offset: 30,
      query: '',
      symbol: '',
    });
  });

  it('keeps the selected history detail while navigating pages', async () => {
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    await screen.findByText('显示 1-10 / 35 条实验');
    fireEvent.click(screen.getByRole('button', { name: '#35' }));
    expect((await screen.findAllByText('回测 #35')).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('显示 11-20 / 35 条实验');

    expect(screen.getAllByText('回测 #35').length).toBeGreaterThan(0);
  });

  it('searches the complete history and resets to the first page', async () => {
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    await screen.findByText('显示 1-10 / 35 条实验');
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('显示 11-20 / 35 条实验');

    fireEvent.change(screen.getByPlaceholderText('搜索实验、策略、标签或方案'), {
      target: { value: '候选' },
    });

    expect(await screen.findByText('显示 1-1 / 1 条实验')).toBeTruthy();
    expect(apiMocks.fetchBacktestRunPage).toHaveBeenLastCalledWith({
      limit: 10,
      offset: 0,
      query: '候选',
      symbol: '',
    });
  });

  it('does not let an older search response replace the latest results', async () => {
    let resolveOlder!: (page: BacktestRunPage) => void;
    let resolveLatest!: (page: BacktestRunPage) => void;
    const olderResponse = new Promise<BacktestRunPage>((resolve) => {
      resolveOlder = resolve;
    });
    const latestResponse = new Promise<BacktestRunPage>((resolve) => {
      resolveLatest = resolve;
    });
    apiMocks.fetchBacktestRunPage.mockImplementation(async ({ limit, offset, query }) => {
      if (query === '旧条件') return olderResponse;
      if (query === '新条件') return latestResponse;
      return {
        runs: [makeRun(35)],
        total: 1,
        limit,
        offset,
        symbols: ['688981.SH'],
      };
    });
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    await screen.findByRole('button', { name: '#35' });
    const search = screen.getByPlaceholderText('搜索实验、策略、标签或方案');
    fireEvent.change(search, { target: { value: '旧条件' } });
    await waitFor(() => expect(apiMocks.fetchBacktestRunPage).toHaveBeenCalledWith({
      limit: 10,
      offset: 0,
      query: '旧条件',
      symbol: '',
    }));
    fireEvent.change(search, { target: { value: '新条件' } });
    await waitFor(() => expect(apiMocks.fetchBacktestRunPage).toHaveBeenCalledWith({
      limit: 10,
      offset: 0,
      query: '新条件',
      symbol: '',
    }));

    resolveLatest({
      runs: [makeRun(2)], total: 1, limit: 10, offset: 0, symbols: ['688981.SH'],
    });
    expect(await screen.findByRole('button', { name: '#02' })).toBeTruthy();
    resolveOlder({
      runs: [makeRun(1)], total: 1, limit: 10, offset: 0, symbols: ['688981.SH'],
    });

    await waitFor(() => expect(screen.queryByRole('button', { name: '#01' })).toBeNull());
    expect(screen.getByRole('button', { name: '#02' })).toBeTruthy();
  });

  it('can find a comparison record outside the current page', async () => {
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    await screen.findByText('显示 1-10 / 35 条实验');
    fireEvent.change(screen.getByPlaceholderText('搜索对比回测'), {
      target: { value: 'run-1' },
    });

    await waitFor(() => expect(apiMocks.fetchBacktestRunPage).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
      query: 'run-1',
      symbol: '',
    }));
    expect(await screen.findByRole('option', { name: /#01/ })).toBeTruthy();
  });
});
