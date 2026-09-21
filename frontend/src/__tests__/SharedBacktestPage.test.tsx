import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SharedBacktestPage from '../components/SharedBacktestPage';

const apiMocks = vi.hoisted(() => ({
  fetchSharedBacktest: vi.fn(),
  fetchSharedBacktests: vi.fn(),
  fetchAdminSharedBacktests: vi.fn(),
  fetchSharedBacktestChart: vi.fn(),
  fetchSharedBacktestResultPage: vi.fn(),
  copySharedBacktest: vi.fn(),
}));

vi.mock('../api/client', () => apiMocks);

const listItem = {
  id: 'publication-1',
  title: '均线回测',
  authorName: '测试作者',
  tags: ['均线'],
  symbol: '688981.SH',
  timeframe: '5m',
  strategyCategory: 'directional_holding',
  startDate: '2026-01-01',
  endDate: '2026-01-31',
  totalReturn: 0.12,
  maxDrawdownRate: 0.04,
  annualSharpe: 1.5,
  copyCount: 0,
  firstPublishedAt: '2026-09-15T10:00:00+08:00',
};

describe('SharedBacktestPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.fetchSharedBacktests.mockResolvedValue({
      items: [listItem], total: 1, limit: 20, offset: 0,
    });
    apiMocks.fetchSharedBacktest.mockResolvedValue({
      ...listItem,
      description: '适合震荡行情',
      snapshotSchemaVersion: 1,
      snapshotDigest: 'a'.repeat(64),
      strategy: {
        code: '<img src=x onerror="window.__unsafe = true">',
        codeHash: 'c'.repeat(64),
        entryType: 'on_bar',
        runtimeVersion: 'runtime-1',
      },
      configuration: {
        initialCapital: 1000000,
        commissionRate: 0.0003,
        slippage: 0.0001,
        fillModel: 'bar_next_open',
        executionAlgorithm: 'immediate',
      },
      data: {
        marketData: { provider: 'qmt_kline' },
        resultData: { actualStartDate: '2026-01-02', actualEndDate: '2026-01-30' },
      },
      result: {
        summary: { totalReturn: 0.12 },
        benchmarkSymbol: '000300.SH',
        benchmarkReturn: 0.08,
        warnings: ['样本期较短'],
      },
      artifacts: [{
        id: 'artifact-1',
        type: 'research_signal',
        displayName: 'signals.ndjson',
        contentType: 'application/x-ndjson',
        byteSize: 128,
        contentSha256: 'b'.repeat(64),
        snapshotSchemaVersion: 1,
        reproduction: { rowCount: 1 },
        downloadUrl: '/api/shared-backtests/publication-1/artifacts/artifact-1',
      }],
      origin: {},
      riskNotice: '历史回测结果不代表未来表现',
    });
    apiMocks.copySharedBacktest.mockResolvedValue({
      configId: 'config-copy-1',
      scriptId: 'script-copy-1',
      name: '均线回测-副本',
      created: true,
      accountCleared: true,
      sharedSource: {
        publicationId: 'publication-1',
        authorName: '测试作者',
        title: '均线回测',
      },
    });
    apiMocks.fetchAdminSharedBacktests.mockResolvedValue({
      items: [], total: 0, limit: 20, offset: 0,
    });
    apiMocks.fetchSharedBacktestChart.mockResolvedValue({
      equity: [{ date: '2026-01-02', value: 1010000, pnl: 10000 }],
      benchmarkSymbol: '000300.SH',
      benchmarkEquity: [{ date: '2026-01-02', value: 1.01, returnRate: 0.01 }],
    });
    apiMocks.fetchSharedBacktestResultPage.mockResolvedValue({
      items: [], nextCursor: null, total: 0,
    });
  });

  it('shows the safe list projection and opens the basic detail', async () => {
    render(<SharedBacktestPage />);

    expect(await screen.findByRole('heading', { name: '均线回测' })).toBeTruthy();
    expect(screen.getByText('测试作者 · 688981.SH · 5m')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看详情' }));

    expect(await screen.findByText('历史回测结果不代表未来表现')).toBeTruthy();
    const rangeField = screen.getByText('回测区间').parentElement;
    expect(rangeField).toBeTruthy();
    expect(within(rangeField!).getByText('2026-01-01 至 2026-01-31')).toBeTruthy();
    expect(screen.getByText('bar_next_open')).toBeTruthy();
    expect(screen.getByText('runtime-1')).toBeTruthy();
    expect(screen.getByText('样本期较短')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '策略代码' }));
    expect(screen.getByText(/window.__unsafe/)).toBeTruthy();
    expect(screen.getByText(`代码校验值（SHA-256）：${'c'.repeat(64)}`)).toBeTruthy();
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByRole('link', { name: 'signals.ndjson' }).getAttribute('href')).toContain(
      '/artifacts/artifact-1',
    );
    expect(apiMocks.fetchSharedBacktest).toHaveBeenCalledWith('publication-1');
  });

  it('loads large result sections only after opening their tabs', async () => {
    render(<SharedBacktestPage initialPublicationId="publication-1" />);

    expect(await screen.findByText('判断上下文')).toBeTruthy();
    expect(apiMocks.fetchSharedBacktestChart).not.toHaveBeenCalled();
    expect(apiMocks.fetchSharedBacktestResultPage).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: '收益与持仓' }));
    await waitFor(() => {
      expect(apiMocks.fetchSharedBacktestChart).toHaveBeenCalledWith('publication-1');
      expect(apiMocks.fetchSharedBacktestResultPage).toHaveBeenCalledWith(
        'publication-1', 'holdings', '0',
      );
      expect(apiMocks.fetchSharedBacktestResultPage).toHaveBeenCalledWith(
        'publication-1', 'cycles', '0',
      );
    });

    fireEvent.click(screen.getByRole('tab', { name: '成交与信号' }));
    await waitFor(() => {
      expect(apiMocks.fetchSharedBacktestResultPage).toHaveBeenCalledWith(
        'publication-1', 'trades', '0',
      );
      expect(apiMocks.fetchSharedBacktestResultPage).toHaveBeenCalledWith(
        'publication-1', 'signals', '0',
      );
      expect(apiMocks.fetchSharedBacktestResultPage).toHaveBeenCalledWith(
        'publication-1', 'execution-plans', '0',
      );
    });
  });

  it('copies without running and opens the independent strategy', async () => {
    const onOpenCopiedStrategy = vi.fn();
    render(
      <SharedBacktestPage
        initialPublicationId="publication-1"
        onOpenCopiedStrategy={onOpenCopiedStrategy}
      />,
    );

    expect(await screen.findByText(/原作者的回测账户不会复制/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '复制到我的策略' }));

    await waitFor(() => {
      expect(apiMocks.copySharedBacktest).toHaveBeenCalledWith('publication-1');
      expect(onOpenCopiedStrategy).toHaveBeenCalledWith('config-copy-1');
    });
  });

  it('clears the prior detail when an old link becomes unavailable', async () => {
    render(<SharedBacktestPage initialPublicationId="publication-1" />);
    await screen.findByText('判断上下文');
    fireEvent.click(screen.getByRole('tab', { name: '策略代码' }));
    expect(await screen.findByText(/window\.__unsafe/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /返回策略广场/ }));
    apiMocks.fetchSharedBacktest.mockRejectedValueOnce(new Error('该共享回测已不可用'));
    fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));

    expect((await screen.findByRole('alert')).textContent).toContain('该共享回测已不可用');
    expect(screen.queryByText(/window\.__unsafe/)).toBeNull();
  });

  it('shows the management section only to administrators', async () => {
    const { rerender } = render(<SharedBacktestPage />);
    await screen.findByRole('heading', { name: '均线回测' });
    expect(screen.queryByRole('tab', { name: '共享管理' })).toBeNull();

    rerender(<SharedBacktestPage isAdmin />);
    fireEvent.click(screen.getByRole('tab', { name: '共享管理' }));
    expect(await screen.findByLabelText('共享回测管理')).toBeTruthy();
  });
});
