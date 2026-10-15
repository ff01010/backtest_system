import { describe, expect, it } from 'vitest';
import type { BacktestResourceStatus } from '../api/client';
import { diagnoseBacktestResources } from '../components/BacktestResourceStatusPanel';

function status(overrides: Partial<BacktestResourceStatus> = {}): BacktestResourceStatus {
  return {
    baseLimit: 16,
    burstLimit: 24,
    effectiveLimit: 16,
    overrideLimit: null,
    overrideExpiresAt: null,
    activeCount: 8,
    barActiveCount: 4,
    l2ActiveCount: 4,
    runnerStatus: 'ok',
    runners: {
      bar: { healthy: 16, errors: 0, queued: 0, oldestQueuedSeconds: null },
      l2: { healthy: 16, errors: 0, queued: 0, oldestQueuedSeconds: null },
    },
    ...overrides,
  };
}

describe('diagnoseBacktestResources', () => {
  it('identifies the fairness burst independently from the base limit', () => {
    const diagnosis = diagnoseBacktestResources(status({ activeCount: 18 }));

    expect(diagnosis.label).toBe('公平突发中');
  });

  it('identifies a saturated runner pool before blaming global capacity', () => {
    const diagnosis = diagnoseBacktestResources(status({
      activeCount: 16,
      barActiveCount: 16,
      l2ActiveCount: 0,
      runners: {
        bar: { healthy: 16, errors: 0, queued: 3, oldestQueuedSeconds: 60 },
        l2: { healthy: 16, errors: 0, queued: 0, oldestQueuedSeconds: null },
      },
    }));

    expect(diagnosis.label).toBe('Bar 执行池已满');
  });

  it('prioritizes runner degradation over queue pressure', () => {
    const diagnosis = diagnoseBacktestResources(status({
      runnerStatus: 'degraded',
      runners: {
        bar: { healthy: 0, errors: 1, queued: 3, oldestQueuedSeconds: 60 },
        l2: { healthy: 16, errors: 0, queued: 0, oldestQueuedSeconds: null },
      },
    }));

    expect(diagnosis).toMatchObject({ tone: 'danger', label: '执行器异常' });
  });
});
