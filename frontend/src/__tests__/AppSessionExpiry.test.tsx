import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';

const EVENT_NAME = 'quant-backtest:auth-session-expired';
const apiMocks = vi.hoisted(() => ({
  fetchCurrentUser: vi.fn(),
}));

vi.mock('../api/client', () => ({
  AUTH_SESSION_EXPIRED_EVENT: 'quant-backtest:auth-session-expired',
  createResearchAuthorizationCode: vi.fn(),
  fetchBacktestRunChart: vi.fn(),
  fetchChart: vi.fn(),
  fetchCurrentUser: apiMocks.fetchCurrentUser,
  fetchDates: vi.fn().mockResolvedValue([]),
  fetchHealth: vi.fn().mockResolvedValue({ experiments: { enabled: false } }),
  fetchInstruments: vi.fn().mockResolvedValue([]),
  fetchStrategies: vi.fn().mockResolvedValue(['user_script']),
  login: vi.fn(),
  logout: vi.fn(),
}));

vi.mock('../components/BacktestPage', () => ({ default: () => <div>策略工作区</div> }));
vi.mock('../components/Toolbar', () => ({ default: () => null }));
vi.mock('../components/KlineChart', () => ({ default: () => null }));
vi.mock('../components/StatusBar', () => ({ default: () => null }));
vi.mock('../components/SystemManagementPage', () => ({ default: () => null }));
vi.mock('../components/ApiKeyPage', () => ({ default: () => null }));
vi.mock('../components/SharedBacktestPage', () => ({ default: () => null }));
vi.mock('../components/ReleaseNotificationManager', () => ({ default: () => null }));
vi.mock('../features/experiments/ExperimentPage', () => ({ default: () => null }));

describe('App session expiry', () => {
  it('returns an active user to login with an explicit expiry notice', async () => {
    apiMocks.fetchCurrentUser.mockResolvedValue({
      id: 'user-1',
      username: 'researcher',
      displayName: '研究员',
      role: 'user',
    });
    render(<App />);
    expect(await screen.findByText('策略工作区')).toBeTruthy();

    fireEvent(window, new Event(EVENT_NAME));

    expect(await screen.findByText('登录已过期，请重新登录。')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '青衡量化回测系统' })).toBeTruthy();
  });
});
