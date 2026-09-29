import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ApiKeyPage from '../components/ApiKeyPage';
import { createApiKey, fetchApiKeys } from '../api/client';

vi.mock('../api/client', () => ({
  createApiKey: vi.fn(),
  fetchApiKeys: vi.fn(),
  revokeApiKey: vi.fn(),
}));

describe('ApiKeyPage', () => {
  beforeEach(() => {
    vi.mocked(fetchApiKeys).mockResolvedValue([]);
    vi.mocked(createApiKey).mockReset();
  });

  it('新增高权限策略 Scope 默认不勾选', async () => {
    render(<ApiKeyPage />);
    await waitFor(() => expect(fetchApiKeys).toHaveBeenCalled());

    expect((screen.getByRole('checkbox', { name: '读取策略与版本' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('checkbox', { name: '创建、修改与恢复策略' }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('checkbox', { name: '发布策略版本' }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('checkbox', { name: '归档策略' }) as HTMLInputElement).checked).toBe(false);
  });

  it('创建未完成时重复点击只提交一次并保留选择', async () => {
    let resolveCreate: ((value: Awaited<ReturnType<typeof createApiKey>>) => void) | undefined;
    vi.mocked(createApiKey).mockImplementation(() => new Promise((resolve) => {
      resolveCreate = resolve;
    }));
    render(<ApiKeyPage />);
    await waitFor(() => expect(fetchApiKeys).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText('例如：研究脚本'), {
      target: { value: '自动化调用' },
    });
    const createButton = screen.getByRole('button', { name: '创建 Key' });
    fireEvent.click(createButton);
    fireEvent.click(createButton);

    expect(createApiKey).toHaveBeenCalledTimes(1);
    expect((screen.getByRole('checkbox', { name: '读取策略与版本' }) as HTMLInputElement).checked).toBe(true);
    resolveCreate?.({
      key: {
        id: 'key-1',
        name: '自动化调用',
        prefix: 'qbk_test',
        scopes: ['strategy:read'],
        expiresAt: null,
        revokedAt: null,
        lastUsedAt: null,
        createdAt: '2026-09-28T10:00:00+08:00',
      },
      plainTextKey: 'qbk_test_secret',
    });
    expect((await screen.findByText('qbk_test_secret')).textContent).toBe('qbk_test_secret');
  });
});
