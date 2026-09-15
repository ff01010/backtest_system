import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MySharedBacktestsPanel from '../components/MySharedBacktestsPanel';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';

const apiMocks = vi.hoisted(() => ({
  fetchMySharedBacktests: vi.fn(),
  relistSharedBacktest: vi.fn(),
  updateSharedBacktestMetadata: vi.fn(),
  withdrawSharedBacktest: vi.fn(),
}));

vi.mock('../api/client', () => apiMocks);

function item(status: SharedBacktestStatus): SharedBacktestManagementItem {
  return {
    id: `publication-${status}`,
    title: `${status} 策略`,
    description: `${status} 说明`,
    authorName: '测试作者',
    tags: ['均线'],
    status,
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
    withdrawnAt: status === 'withdrawn' ? '2026-09-16T10:00:00+08:00' : null,
    removedAt: status === 'removed' ? '2026-09-17T10:00:00+08:00' : null,
    removalReason: status === 'removed' ? '策略不再适用' : null,
    updatedAt: '2026-09-17T10:00:00+08:00',
    sourceRunAvailable: status !== 'withdrawn',
  };
}

describe('MySharedBacktestsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const items = [item('active'), item('withdrawn'), item('removed')];
    apiMocks.fetchMySharedBacktests.mockResolvedValue({
      items, total: items.length, limit: 20, offset: 0,
    });
  });

  it('显示作者的全部状态、原任务删除提示和下架原因', async () => {
    render(<MySharedBacktestsPanel onPublicationChanged={vi.fn()} />);

    expect(await screen.findByRole('heading', { name: 'active 策略' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'withdrawn 策略' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'removed 策略' })).toBeTruthy();
    expect(screen.getByText('原回测已删除，共享快照仍保留。')).toBeTruthy();
    expect(screen.getByText(/原因：策略不再适用/)).toBeTruthy();
    expect(screen.queryAllByRole('button', { name: '重新上架' })).toHaveLength(1);
  });

  it('只提交可编辑的展示信息', async () => {
    const changed = vi.fn();
    const updated = { ...item('active'), title: '新标题', description: '新说明', tags: ['新标签'] };
    apiMocks.updateSharedBacktestMetadata.mockResolvedValue(updated);
    render(<MySharedBacktestsPanel onPublicationChanged={changed} />);
    await screen.findByRole('heading', { name: 'active 策略' });

    fireEvent.click(screen.getAllByRole('button', { name: '编辑展示信息' })[0]);
    fireEvent.change(screen.getByLabelText('共享标题'), { target: { value: '新标题' } });
    fireEvent.change(screen.getByLabelText('策略说明'), { target: { value: '新说明' } });
    fireEvent.change(screen.getByLabelText('标签（逗号分隔）'), { target: { value: '新标签' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(apiMocks.updateSharedBacktestMetadata).toHaveBeenCalledWith(
      'publication-active',
      { title: '新标题', description: '新说明', tags: ['新标签'] },
    ));
    expect(await screen.findByRole('heading', { name: '新标题' })).toBeTruthy();
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it('仅为已上架和已撤回条目提供合法状态转换', async () => {
    const withdrawn = { ...item('active'), status: 'withdrawn' as const, withdrawnAt: '2026-09-18T10:00:00+08:00' };
    const relisted = { ...item('withdrawn'), status: 'active' as const, withdrawnAt: null };
    apiMocks.withdrawSharedBacktest.mockResolvedValue(withdrawn);
    apiMocks.relistSharedBacktest.mockResolvedValue(relisted);
    render(<MySharedBacktestsPanel onPublicationChanged={vi.fn()} />);
    await screen.findByRole('heading', { name: 'active 策略' });

    fireEvent.click(screen.getByRole('button', { name: '撤回' }));
    await waitFor(() => expect(apiMocks.withdrawSharedBacktest).toHaveBeenCalledWith('publication-active'));
    fireEvent.click(screen.getAllByRole('button', { name: '重新上架' })[1]);
    await waitFor(() => expect(apiMocks.relistSharedBacktest).toHaveBeenCalledWith('publication-withdrawn'));
    expect(screen.queryByRole('button', { name: '恢复已下架条目' })).toBeNull();
  });
});
