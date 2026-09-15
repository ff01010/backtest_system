import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SharedBacktestPage from '../components/SharedBacktestPage';

const apiMocks = vi.hoisted(() => ({
  fetchSharedBacktest: vi.fn(),
  fetchSharedBacktests: vi.fn(),
  fetchAdminSharedBacktests: vi.fn(),
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
      strategy: { code: 'def on_bar(bar, state):\n  return []' },
      configuration: { initialCapital: 1000000 },
      data: { marketData: { provider: 'qmt_kline' } },
      result: { summary: { totalReturn: 0.12 } },
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
  });

  it('shows the safe list projection and opens the basic detail', async () => {
    render(<SharedBacktestPage />);

    expect(await screen.findByRole('heading', { name: '均线回测' })).toBeTruthy();
    expect(screen.getByText('测试作者 · 688981.SH · 5m')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看详情' }));

    expect(await screen.findByText('历史回测结果不代表未来表现')).toBeTruthy();
    expect(screen.getByText(/def on_bar/)).toBeTruthy();
    expect(apiMocks.fetchSharedBacktest).toHaveBeenCalledWith('publication-1');
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
    expect(await screen.findByText(/def on_bar/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /返回策略广场/ }));
    apiMocks.fetchSharedBacktest.mockRejectedValueOnce(new Error('该共享回测已不可用'));
    fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));

    expect((await screen.findByRole('alert')).textContent).toContain('该共享回测已不可用');
    expect(screen.queryByText(/def on_bar/)).toBeNull();
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
