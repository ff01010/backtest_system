import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AdminSharedBacktestsPanel from '../components/AdminSharedBacktestsPanel';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';

const apiMocks = vi.hoisted(() => ({
  fetchAdminSharedBacktests: vi.fn(),
  removeSharedBacktest: vi.fn(),
  restoreSharedBacktest: vi.fn(),
}));

vi.mock('../api/client', () => apiMocks);

function item(status: SharedBacktestStatus): SharedBacktestManagementItem {
  return {
    id: `publication-${status}`,
    title: `${status} 策略`,
    description: '策略说明',
    authorName: '测试作者',
    tags: [],
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
    withdrawnAt: null,
    removedAt: status === 'removed' ? '2026-09-17T10:00:00+08:00' : null,
    removalReason: status === 'removed' ? '测试原因' : null,
    updatedAt: '2026-09-17T10:00:00+08:00',
    sourceRunAvailable: true,
  };
}

describe('AdminSharedBacktestsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const items = [item('active'), item('withdrawn'), item('removed')];
    apiMocks.fetchAdminSharedBacktests.mockResolvedValue({
      items, total: items.length, limit: 20, offset: 0,
    });
  });

  it('requires a reason before removing an active publication', async () => {
    const changed = vi.fn();
    const removed = {
      ...item('active'),
      status: 'removed' as const,
      removedAt: '2026-09-18T10:00:00+08:00',
      removalReason: '策略说明存在误导',
    };
    apiMocks.removeSharedBacktest.mockResolvedValue(removed);
    render(<AdminSharedBacktestsPanel onPublicationChanged={changed} />);
    await screen.findByRole('heading', { name: 'active 策略' });

    fireEvent.click(screen.getByRole('button', { name: '下架' }));
    expect((screen.getByRole('button', { name: '确认下架' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('下架原因'), {
      target: { value: '  策略说明存在误导  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: '确认下架' }));

    await waitFor(() => expect(apiMocks.removeSharedBacktest).toHaveBeenCalledWith(
      'publication-active',
      '策略说明存在误导',
    ));
    expect(changed).toHaveBeenCalledWith(removed);
  });

  it('restores only removed publications and preserves the management record', async () => {
    const restored = { ...item('removed'), status: 'active' as const };
    apiMocks.restoreSharedBacktest.mockResolvedValue(restored);
    render(<AdminSharedBacktestsPanel onPublicationChanged={vi.fn()} />);
    await screen.findByRole('heading', { name: 'removed 策略' });

    expect(screen.queryAllByRole('button', { name: '下架' })).toHaveLength(1);
    expect(screen.queryAllByRole('button', { name: '恢复上架' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '恢复上架' }));

    await waitFor(() => expect(apiMocks.restoreSharedBacktest).toHaveBeenCalledWith(
      'publication-removed',
    ));
    expect(screen.queryByText(/原因：测试原因/)).toBeNull();
  });
});
