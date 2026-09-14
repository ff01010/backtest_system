import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ReleaseNotificationManager from '../components/ReleaseNotificationManager';

const apiMocks = vi.hoisted(() => ({
  acknowledgeRelease: vi.fn(),
  fetchReleaseHistory: vi.fn(),
  fetchReleaseStatus: vi.fn(),
}));

vi.mock('../api/client', () => ({
  ...apiMocks,
  ReleaseApiError: class ReleaseApiError extends Error {
    status: number;

    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

const releaseStatus = {
  serverBuildId: 'a'.repeat(40),
  requiredClientCompatibilityVersion: 1,
  clientOutdated: true,
  activationKind: 'release' as const,
  presentationMode: 'release_notice' as const,
  acknowledged: false,
  unread: true,
  upgradeRequired: false,
  release: {
    id: 'release-1',
    releaseNo: 1,
    version: '2026.09.14',
    title: '能力更新',
    summary: '更新摘要',
    highlights: ['更新一'],
    level: 'normal' as const,
    publishedAt: '2026-09-14T00:00:00Z',
  },
};

describe('ReleaseNotificationManager', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('确认公告失败时保留弹窗并显示可重试提示', async () => {
    apiMocks.fetchReleaseStatus.mockResolvedValue({ status: releaseStatus, etag: '"release-1"' });
    apiMocks.acknowledgeRelease.mockRejectedValue(new Error('network failed'));

    render(
      <ReleaseNotificationManager
        userId="user-1"
        hasUnsavedStrategy={false}
        isStrategySavePending={false}
        onRequestStrategySave={vi.fn().mockResolvedValue(true)}
        clientBuildId={'b'.repeat(40)}
        clientCompatibilityVersion={1}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '知道了' }));

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe('确认失败，请重试。');
    });
    expect(screen.getByRole('dialog', { name: '版本更新' })).toBeTruthy();
  });
});
