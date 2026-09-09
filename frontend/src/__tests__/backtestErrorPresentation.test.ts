import { describe, expect, it } from 'vitest';
import { backtestErrorPresentation } from '../utils/backtestErrorPresentation';

describe('backtestErrorPresentation', () => {
  it('将运行时契约缺失归为平台责任', () => {
    const result = backtestErrorPresentation({
      category: 'system_error',
      code: 'runtime.contract_missing',
      message: '回测因平台运行环境兼容问题未完成。',
      retryable: false,
      taskId: 'run-1',
      missingName: 'tuple',
      runtimeVersion: 'python3.12-runtime-v2',
    });

    expect(result.title).toBe('平台运行环境异常');
    expect(result.isUserActionable).toBe(false);
    expect(result.location).toContain('tuple');
  });

  it('将真实未定义名称归为用户可修改错误并显示行号', () => {
    const result = backtestErrorPresentation({
      category: 'strategy_error',
      code: 'strategy.undefined_name',
      message: '策略引用了未定义的名称 signal。',
      retryable: false,
      callback: 'on_bar',
      missingName: 'signal',
      strategyLine: 12,
    });

    expect(result.title).toBe('策略执行失败');
    expect(result.isUserActionable).toBe(true);
    expect(result.location).toBe('on_bar · 第 12 行 · 名称：signal');
  });
});
