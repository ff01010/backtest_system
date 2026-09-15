import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SharedBacktestPage from '../components/SharedBacktestPage';

const apiMocks = vi.hoisted(() => ({
  favoriteSharedBacktest: vi.fn(),
  fetchSharedBacktest: vi.fn(),
  fetchSharedBacktests: vi.fn(),
  unfavoriteSharedBacktest: vi.fn(),
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
  copyCount: 7,
  firstPublishedAt: '2026-09-15T10:00:00+08:00',
  isFavorited: false,
  hasCopied: true,
};

describe('SharedBacktestPage discovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.fetchSharedBacktests.mockResolvedValue({
      items: [listItem], total: 25, limit: 12, offset: 0,
    });
    apiMocks.fetchSharedBacktest.mockResolvedValue({
      ...listItem,
      description: '策略说明',
      snapshotSchemaVersion: 1,
      snapshotDigest: 'a'.repeat(64),
      strategy: {}, configuration: {}, data: {}, result: {}, origin: {},
      riskNotice: '历史回测结果不代表未来表现',
    });
    apiMocks.favoriteSharedBacktest.mockResolvedValue({ favorited: true, created: true });
    apiMocks.unfavoriteSharedBacktest.mockResolvedValue({ favorited: false, deleted: true });
  });

  it('submits discovery filters and stable pagination parameters', async () => {
    render(<SharedBacktestPage />);
    await screen.findByRole('heading', { name: '均线回测' });

    fireEvent.change(screen.getByLabelText('搜索共享回测'), { target: { value: '测试作者' } });
    fireEvent.change(screen.getByLabelText('策略类别'), { target: { value: 'directional_holding' } });
    fireEvent.change(screen.getByLabelText('周期'), { target: { value: '5m' } });
    fireEvent.change(screen.getByLabelText('排序'), { target: { value: 'copies' } });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }));

    await waitFor(() => expect(apiMocks.fetchSharedBacktests).toHaveBeenCalledWith({
      query: '测试作者',
      strategyCategory: 'directional_holding',
      timeframe: '5m',
      sort: 'copies',
      limit: 12,
      offset: 12,
      favoritesOnly: false,
    }));
    expect(screen.getByText('已复制 7 次')).toBeTruthy();
    expect(screen.getAllByText('已复制').length).toBeGreaterThan(0);
    expect(screen.getByText(/首次上架：2026-09-15/)).toBeTruthy();
    expect(screen.getByText(/策略类别：方向持仓/)).toBeTruthy();
  });

  it('shows immutable attribution for a derived publication card', async () => {
    apiMocks.fetchSharedBacktests.mockResolvedValue({
      items: [{
        ...listItem,
        sourcePublicationId: 'origin-1',
        sourceTitle: '原始共享',
        sourceAuthorName: '原作者',
      }],
      total: 1,
      limit: 12,
      offset: 0,
    });

    render(<SharedBacktestPage />);

    expect(await screen.findByText('衍生自：原始共享 · 原作者 原作者')).toBeTruthy();
  });

  it('loads personal favorites and removes an unfavorited card', async () => {
    apiMocks.fetchSharedBacktests.mockResolvedValue({
      items: [{ ...listItem, isFavorited: true }], total: 1, limit: 12, offset: 0,
    });
    render(<SharedBacktestPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '我的收藏' }));
    await waitFor(() => expect(apiMocks.fetchSharedBacktests).toHaveBeenCalledWith(
      expect.objectContaining({ favoritesOnly: true }),
    ));
    fireEvent.click(screen.getByRole('button', { name: '取消收藏 均线回测' }));

    await waitFor(() => expect(apiMocks.unfavoriteSharedBacktest).toHaveBeenCalledWith('publication-1'));
    expect(await screen.findByText('暂无收藏的共享回测。')).toBeTruthy();
  });

  it('shows a unified unavailable state for a stale shared link', async () => {
    apiMocks.fetchSharedBacktest.mockRejectedValue(new Error('API 404: removed'));

    render(<SharedBacktestPage initialPublicationId="publication-1" />);

    expect((await screen.findByRole('alert')).textContent).toContain('该共享回测已不可用');
    expect(screen.queryByText('removed')).toBeNull();
  });
});
