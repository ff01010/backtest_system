import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SystemManagementPage from '../components/SystemManagementPage';

vi.mock('../components/SystemErrorLogPage', () => ({
  default: () => <div>监控页面内容</div>,
}));

vi.mock('../components/UserManagementPage', () => ({
  default: () => <div>人员页面内容</div>,
}));

describe('SystemManagementPage', () => {
  it('defaults to monitoring and keeps personnel management as a separate page', () => {
    render(<SystemManagementPage />);

    expect(screen.getByText('监控页面内容')).toBeTruthy();
    expect(screen.queryByText('人员页面内容')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '人员与权限' }));

    expect(screen.getByText('人员页面内容')).toBeTruthy();
    expect(screen.queryByText('监控页面内容')).toBeNull();
  });
});
