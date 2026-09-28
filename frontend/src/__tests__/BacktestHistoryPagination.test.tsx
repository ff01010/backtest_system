import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
  publishBacktestRun: vi.fn(),
  setBacktestRunFavorite: vi.fn(),
  createSignalDetailExport: vi.fn(),
  fetchSignalDetailExports: vi.fn(),
  cancelSignalDetailExport: vi.fn(),
  retrySignalDetailExport: vi.fn(),
  summarizeBacktestRunSelection: vi.fn(),
  deleteBacktestSelection: vi.fn(),
}));

vi.mock('../api/client', () => ({
  ...apiMocks,
  BacktestRequestError: class BacktestRequestError extends Error {},
  cancelBacktestRun: vi.fn(),
  createAccount: vi.fn(),
  deleteBacktestRun: vi.fn(),
  deleteBacktestRuns: vi.fn(),
  deleteBacktestSelection: apiMocks.deleteBacktestSelection,
  deleteStrategyConfig: vi.fn(),
  fetchAccounts: vi.fn().mockResolvedValue([]),
  fetchStrategyConfigVersions: vi.fn().mockResolvedValue([]),
  fetchStrategyParameterSchema: vi.fn(),
  fetchStrategySource: vi.fn(),
  runBacktest: vi.fn(),
  saveCompleteStrategy: vi.fn(),
  setBacktestRunFavorite: apiMocks.setBacktestRunFavorite,
  createSignalDetailExport: apiMocks.createSignalDetailExport,
  fetchSignalDetailExports: apiMocks.fetchSignalDetailExports,
  cancelSignalDetailExport: apiMocks.cancelSignalDetailExport,
  retrySignalDetailExport: apiMocks.retrySignalDetailExport,
  signalDetailExportDownloadUrl: (exportId: string) => `/api/export/${exportId}`,
  summarizeBacktestRunSelection: apiMocks.summarizeBacktestRunSelection,
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
    isFavorited: false,
    favoritedAt: null,
    queuedAt: '',
    startedAt: '',
    cancelRequested: false,
    resultJson: null,
    createdAt: `2026-01-${String(Math.min(displayNo, 31)).padStart(2, '0')}T00:00:00+08:00`,
    finishedAt: '',
  };
}

function makeSignalExportTask() {
  return {
    id: 'export-1', selectionMode: 'ids', selectionSnapshot: {}, status: 'queued',
    selectedRunCount: 2, preparedRunCount: 0, totalRunCount: null,
    processedRunCount: 0, excludedRunCount: null, exclusionSummary: null,
    cancelRequested: false, downloadFilename: null, byteSize: null,
    contentSha256: null, errorCode: null, errorMessage: null,
    lastDownloadedAt: null, createdAt: '2026-09-18T10:00:00+08:00',
    startedAt: null, finishedAt: null, expiresAt: null,
  };
}

describe('BacktestPage history pagination', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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
        selectableTotal: filtered.length,
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
    apiMocks.setBacktestRunFavorite.mockImplementation(async (runId, favorited) => ({
      runId,
      isFavorited: favorited,
      favoritedAt: favorited ? '2026-09-16T10:30:00+08:00' : null,
    }));
    apiMocks.fetchSignalDetailExports.mockResolvedValue({
      items: [], nextCursor: null, activeCount: 0, undownloadedSucceededCount: 0,
    });
    apiMocks.createSignalDetailExport.mockResolvedValue(makeSignalExportTask());
    apiMocks.summarizeBacktestRunSelection.mockResolvedValue({
      selectedRunCount: 35,
      selectionDigest: 'a'.repeat(64),
    });
    apiMocks.deleteBacktestSelection.mockResolvedValue(35);
  });

  it('shows the finished L2 no-data state instead of permanent range loading', async () => {
    apiMocks.fetchDataStatus.mockImplementation(async (symbol, timeframe, fillModel = 'bar_next_open') => ({
      symbol,
      timeframe,
      fillModel,
      path: '',
      exists: fillModel !== 'l2_quote',
      firstDate: fillModel === 'l2_quote' ? null : '2026-01-01',
      lastDate: fillModel === 'l2_quote' ? null : '2026-09-18',
      barCount: 0,
      fileSize: 0,
      mtime: null,
      duplicateCount: 0,
      nonTradingCount: 0,
      gapDates: [],
      preview: [],
    }));
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '策略代码' }));
    fireEvent.click(screen.getByText('成交与执行'));
    const fillModelSelect = screen.getAllByRole('combobox').find((element) =>
      element.querySelector('option[value="l2_quote"]'),
    );
    expect(fillModelSelect).toBeTruthy();
    fireEvent.change(fillModelSelect!, {
      target: { value: 'l2_quote' },
    });

    await waitFor(() => expect(apiMocks.fetchDataStatus)
      .toHaveBeenCalledWith('688981.SH', '5m', 'l2_quote'));
    expect(await screen.findByText('暂无可回测数据')).toBeTruthy();
    expect(screen.getByText('运行标的 L2 暂无可回测数据')).toBeTruthy();
    expect(screen.queryByText('正在读取')).toBeNull();
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
    expect(apiMocks.fetchBacktestPreflight).not.toHaveBeenCalled();
    expect(apiMocks.fetchBacktestTimeframes).not.toHaveBeenCalled();

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
      runs: [makeRun(2)], total: 1, selectableTotal: 1,
      limit: 10, offset: 0, symbols: ['688981.SH'],
    });
    expect(await screen.findByRole('button', { name: '#02' })).toBeTruthy();
    resolveOlder({
      runs: [makeRun(1)], total: 1, selectableTotal: 1,
      limit: 10, offset: 0, symbols: ['688981.SH'],
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

  it('favorites a history run without selecting it', async () => {
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
    const favoriteButton = await screen.findByRole('button', { name: '收藏历史回测 #34' });
    const favoriteRow = favoriteButton.closest('.history-run-row');
    expect(favoriteRow?.className.includes('is-selected')).toBe(false);
    fireEvent.click(favoriteButton);

    await waitFor(() => expect(apiMocks.setBacktestRunFavorite).toHaveBeenCalledWith('run-34', true));
    expect(await screen.findByRole('button', { name: '取消收藏历史回测 #34' })).toBeTruthy();
    expect(favoriteRow?.className.includes('is-selected')).toBe(false);
  });

  it('resets pagination and requests only favorites', async () => {
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
    fireEvent.click(screen.getByRole('button', { name: '只看收藏' }));

    await waitFor(() => expect(apiMocks.fetchBacktestRunPage).toHaveBeenLastCalledWith({
      limit: 10,
      offset: 0,
      query: '',
      symbol: '',
      favoritesOnly: true,
    }));
  });

  it('uses one toolbar to export an explicit multi-selection', async () => {
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
    fireEvent.click(screen.getByRole('checkbox', { name: '选择历史回测 #35' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '选择历史回测 #34' }));
    expect(screen.getByText('已选择 2 条')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '批量导出信号明细' }));

    await waitFor(() => expect(apiMocks.createSignalDetailExport).toHaveBeenCalledWith(
      { mode: 'ids', runIds: ['run-34', 'run-35'] },
      expect.any(String),
    ));
    expect(await screen.findByRole('dialog', { name: '信号明细导出记录' })).toBeTruthy();
  });

  it('falls back to getRandomValues when randomUUID is unavailable', async () => {
    const nativeCrypto = globalThis.crypto;
    vi.stubGlobal('crypto', {
      getRandomValues: nativeCrypto.getRandomValues.bind(nativeCrypto),
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
    await screen.findByText('显示 1-10 / 35 条实验');
    fireEvent.click(screen.getByRole('checkbox', { name: '选择历史回测 #35' }));
    fireEvent.click(screen.getByRole('button', { name: '批量导出信号明细' }));

    await waitFor(() => expect(apiMocks.createSignalDetailExport).toHaveBeenCalledTimes(1));
    const idempotencyKey = apiMocks.createSignalDetailExport.mock.calls[0][1];
    expect(idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(await screen.findByRole('dialog', { name: '信号明细导出记录' })).toBeTruthy();
  });

  it('submits a signal detail export only once while creation is pending', async () => {
    let resolveCreate!: (task: ReturnType<typeof makeSignalExportTask>) => void;
    apiMocks.createSignalDetailExport.mockImplementation(() => new Promise((resolve) => {
      resolveCreate = resolve;
    }));
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
    fireEvent.click(screen.getByRole('checkbox', { name: '选择历史回测 #35' }));
    const exportButton = screen.getByRole('button', { name: '批量导出信号明细' });
    fireEvent.click(exportButton);
    fireEvent.click(exportButton);

    expect(apiMocks.createSignalDetailExport).toHaveBeenCalledTimes(1);
    resolveCreate(makeSignalExportTask());
    expect(await screen.findByRole('dialog', { name: '信号明细导出记录' })).toBeTruthy();
  });

  it('shows signal detail export creation errors in history', async () => {
    apiMocks.createSignalDetailExport.mockRejectedValueOnce(new Error('导出服务暂不可用'));
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
    fireEvent.click(screen.getByRole('checkbox', { name: '选择历史回测 #35' }));
    fireEvent.click(screen.getByRole('button', { name: '批量导出信号明细' }));

    const alert = await screen.findByRole('alert', { name: '信号明细导出错误' });
    expect(alert.textContent).toContain('导出服务暂不可用');
    expect(screen.queryByRole('dialog', { name: '信号明细导出记录' })).toBeNull();
  });

  it('extends page selection to all filtered records for bulk deletion', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
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
    fireEvent.click(screen.getByRole('checkbox', {
      name: '选择当前页中的全部可删除回测',
    }));
    fireEvent.click(screen.getByRole('button', {
      name: '选择当前筛选结果中的全部 35 条',
    }));
    expect(screen.getAllByText('已选择 35 条').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '删除所选' }));

    const selection = {
      mode: 'filter', query: '', symbol: null, configId: null,
      favoritesOnly: false, excludedRunIds: [],
    };
    await waitFor(() => expect(apiMocks.summarizeBacktestRunSelection)
      .toHaveBeenCalledWith(selection));
    await waitFor(() => expect(apiMocks.deleteBacktestSelection).toHaveBeenCalledWith(
      selection, 35, 'a'.repeat(64),
    ));
    expect(confirm).toHaveBeenCalledWith('将永久删除 35 条历史回测，删除后无法恢复。');
    confirm.mockRestore();
  });

  it('refreshes the filtered page after unfavoriting a run', async () => {
    const run = {
      ...makeRun(1),
      isFavorited: true,
      favoritedAt: '2026-09-16T10:30:00+08:00',
    };
    let favorited = true;
    apiMocks.fetchBacktestRunPage.mockImplementation(async ({ limit, offset }) => ({
      runs: favorited ? [{ ...run, isFavorited: favorited }] : [],
      total: favorited ? 1 : 0,
      limit,
      offset,
      symbols: [run.symbol],
    }));
    apiMocks.setBacktestRunFavorite.mockImplementation(async (runId) => {
      favorited = false;
      return { runId, isFavorited: false, favoritedAt: null };
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
    fireEvent.click(await screen.findByRole('button', { name: '只看收藏' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消收藏历史回测 #01' }));

    expect(await screen.findByText('没有符合当前筛选条件的历史实验。')).toBeTruthy();
    expect(apiMocks.setBacktestRunFavorite).toHaveBeenCalledWith('run-1', false);
  });

  it('publishes a complete successful history run from the share form', async () => {
    const run = makeRun(1);
    const summary = {
      totalPnl: 0,
      totalReturn: 0,
      maxDrawdown: 0,
      maxDrawdownRate: 0,
      annualVolatility: 0,
      winRate: 0,
      dailyWinRate: 0,
      tradeCount: 0,
      winningTrades: 0,
      losingTrades: 0,
      avgWin: 0,
      avgLoss: 0,
      profitLossRatio: null,
      finalEquity: 100000,
      annualReturn: 0,
      annualSharpe: null,
      sortino: null,
    };
    run.scriptSnapshot = {
      code: 'def on_bar(bar, state):\n  return []',
      name: '测试策略',
    };
    run.paramsSnapshot = { name: '测试方案', params: { window: 20 } };
    run.summaryJson = summary;
    run.resultJson = {
      runId: run.id,
      configId: run.configId,
      configVersionId: run.configVersionId,
      scriptId: run.scriptId,
      outputDir: '',
      summary,
      equity: [],
      dailyHoldings: [],
      cycles: [],
      executionPlans: [],
      benchmarkSymbol: null,
      benchmarkEquity: [],
      benchmarkReturn: null,
      excessReturn: null,
      trades: [],
      skippedSignals: [],
      experimentManifest: {
        experimentHash: 'experiment-hash',
        engineVersion: 'test-engine',
        signalModel: 'on_bar',
        fillModel: run.fillModel,
        randomSeed: 0,
        strategyCodeHash: 'strategy-code-hash',
        configHash: 'config-hash',
        dataHash: 'data-hash',
        barCount: 0,
        barRange: {
          start: run.startDate,
          end: run.endDate,
          timeframe: run.timeframe,
        },
        costModel: {
          commissionRate: 0,
          slippage: 0,
        },
      },
      dataSnapshot: {},
      warnings: [],
    } satisfies NonNullable<BacktestRunHistory['resultJson']>;
    apiMocks.fetchBacktestRunPage.mockResolvedValue({
      runs: [run], total: 1, limit: 10, offset: 0, symbols: [run.symbol],
    });
    apiMocks.fetchBacktestRun.mockResolvedValue(run);
    apiMocks.publishBacktestRun.mockResolvedValue({
      publication: { id: 'publication-1' }, created: true,
    });
    const onViewSharedBacktest = vi.fn();
    render(
      <BacktestPage
        symbol="688981.SH"
        strategy="user_script"
        strategies={['user_script']}
        instruments={[]}
        isAdmin={false}
        onViewTradeChart={vi.fn()}
        onViewSharedBacktest={onViewSharedBacktest}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '历史回测' }));
    fireEvent.click(await screen.findByRole('button', { name: '#01' }));
    fireEvent.click(await screen.findByRole('button', { name: /共享到策略广场/ }));
    const dialog = screen.getByRole('dialog', { name: '共享到策略广场' });
    fireEvent.change(screen.getByRole('textbox', { name: '策略说明' }), {
      target: { value: '适合震荡行情，需关注回撤。' },
    });
    fireEvent.click(screen.getByRole('button', { name: '确认共享' }));

    await waitFor(() => expect(apiMocks.publishBacktestRun).toHaveBeenCalledWith('run-1', {
      title: '688981.SH 自定义脚本回测',
      description: '适合震荡行情，需关注回撤。',
      tags: [],
    }));
    expect(dialog.textContent).toContain('账户关联、内部路径和系统诊断不会被共享');
    await waitFor(() => expect(onViewSharedBacktest).toHaveBeenCalledWith('publication-1'));
  });
});
