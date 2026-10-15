import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SystemErrorLogPage from '../components/SystemErrorLogPage';

const apiMocks = vi.hoisted(() => ({
  fetchBacktestResourceStatus: vi.fn(),
  fetchBacktestRunDiagnostics: vi.fn(),
  fetchStrategyRuntimeErrorStats: vi.fn(),
  fetchSystemErrorLogs: vi.fn(),
  fetchUsers: vi.fn(),
}));

vi.mock('../api/client', () => apiMocks);

describe('SystemErrorLogPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.fetchUsers.mockResolvedValue([]);
    apiMocks.fetchBacktestResourceStatus.mockResolvedValue({
      baseLimit: 16,
      burstLimit: 24,
      effectiveLimit: 16,
      overrideLimit: null,
      overrideExpiresAt: null,
      activeCount: 16,
      barActiveCount: 12,
      l2ActiveCount: 4,
      runnerStatus: 'ok',
      runners: {
        bar: { healthy: 16, errors: 0, queued: 7, oldestQueuedSeconds: 83 },
        l2: { healthy: 16, errors: 0, queued: 2, oldestQueuedSeconds: 18 },
      },
    });
    apiMocks.fetchStrategyRuntimeErrorStats.mockResolvedValue([]);
    apiMocks.fetchSystemErrorLogs.mockResolvedValue([{
      id: 1,
      runId: '117f14fd-203a-4705-8fe3-c4a3a42d4746',
      displayNo: 42,
      userId: 'user-1',
      username: 'tester',
      displayName: '测试用户',
      symbol: '688981.SH',
      timeframe: '1m',
      source: 'l2_runner',
      stage: 'l2.market_replay',
      code: 'worker.execution_rejected',
      category: 'worker',
      retryable: false,
      reason: '执行异常',
      exceptionType: 'RuntimeError',
      context: {},
      rawLogRef: 'taskId=117f14fd-203a-4705-8fe3-c4a3a42d4746',
      attemptNo: 1,
      attemptToken: '6e6a7c64-0594-4ff5-a0a8-0477de5c5c59',
      workerId: 'runner:42',
      containerId: 'runner',
      terminationReason: 'exception',
      exitCode: null,
      logCorrelationId: '6e6a7c64-0594-4ff5-a0a8-0477de5c5c59',
      occurredAt: '2026-09-23 10:00:00',
    }]);
    apiMocks.fetchBacktestRunDiagnostics.mockResolvedValue({
      run: {
        runId: '117f14fd-203a-4705-8fe3-c4a3a42d4746',
        displayNo: 42,
        status: 'failed',
        safeReason: '执行异常',
        symbol: '688981.SH',
        timeframe: '1m',
        fillModel: 'l2_quote',
        createdAt: '2026-09-23 09:59:00',
        startedAt: '2026-09-23 10:00:00',
        finishedAt: '2026-09-23 10:01:00',
      },
      attempts: [{
        attemptNo: 1,
        attemptToken: '6e6a7c64-0594-4ff5-a0a8-0477de5c5c59',
        jobKind: 'l2',
        workerId: 'runner:42',
        containerId: 'runner',
        processId: 42,
        applicationBuildId: 'dev',
        runtimeVersion: 'runtime-v1',
        capabilityDigest: 'digest',
        status: 'failed',
        stage: 'l2.market_replay',
        startedAt: '2026-09-23 10:00:00',
        heartbeatAt: '2026-09-23 10:00:30',
        finishedAt: '2026-09-23 10:01:00',
        exitCode: 7,
        terminationReason: 'strategy_exit_nonzero',
        errorCode: 'worker.execution_rejected',
        logCorrelationId: '6e6a7c64-0594-4ff5-a0a8-0477de5c5c59',
      }],
      errors: [],
      logStatus: 'unavailable',
      timeline: [{
        event: 'attempt.failed',
        timestamp: '2026-09-23 10:01:00',
        message: '执行尝试已结束',
        stage: 'l2.market_replay',
        terminationReason: 'strategy_exit_nonzero',
      }],
    });
  });

  it('keeps database attempt timeline visible when Loki is unavailable', async () => {
    render(<SystemErrorLogPage />);
    const row = await screen.findByText('#42');

    fireEvent.click(row.closest('tr') as HTMLTableRowElement);

    await screen.findByText('Attempt 1 · failed');
    expect(screen.getByText('执行时间线')).toBeTruthy();
    expect(screen.getByText(/并不代表发生了多次错误/)).toBeTruthy();
    expect(screen.getByText(/集中日志暂不可用/)).toBeTruthy();
    expect(screen.getAllByText('strategy_exit_nonzero').length).toBeGreaterThan(0);
    await waitFor(() => expect(apiMocks.fetchBacktestRunDiagnostics).toHaveBeenCalledWith(
      '117f14fd-203a-4705-8fe3-c4a3a42d4746',
    ));
  });

  it('shows scheduler capacity and runner pressure before diagnostic logs', async () => {
    render(<SystemErrorLogPage />);

    const progress = await screen.findByRole('progressbar');
    expect(progress.getAttribute('aria-valuenow')).toBe('16');
    expect(progress.getAttribute('aria-valuemax')).toBe('16');
    expect(screen.getByText('基础容量已满')).toBeTruthy();
    expect(screen.getByText('Bar')).toBeTruthy();
    expect(screen.getByText('L2')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '回测监控与诊断' })).toBeTruthy();
  });

  it('keeps log data visible when the resource snapshot cannot be loaded', async () => {
    apiMocks.fetchBacktestResourceStatus.mockRejectedValueOnce(new Error('容量接口不可用'));

    render(<SystemErrorLogPage />);

    expect(await screen.findByText('容量接口不可用')).toBeTruthy();
    expect(await screen.findByText('#42')).toBeTruthy();
  });
});
