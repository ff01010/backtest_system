import { afterEach, describe, expect, it, vi } from 'vitest';
import { saveCompleteStrategy } from '../api/client';

describe('saveCompleteStrategy', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('从结构化导包错误中提取可读消息而不是整段 JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: {
        errorDetail: {
          category: 'strategy_error',
          code: 'strategy.module_unavailable',
          message: '当前运行环境未提供模块 package_that_does_not_exist_123',
          hint: '请改用平台已支持的模块。',
        },
      },
    }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    })));

    const request = saveCompleteStrategy(
      {
        name: '导包错误测试',
        description: '',
        code: 'import package_that_does_not_exist_123',
        entryType: 'on_bar',
        signalRequired: false,
      },
      {
        name: '导包错误测试',
        scriptId: null,
        strategy: 'user_script',
        mode: 'backtest',
        symbol: '688981.SH',
        timeframe: '5m',
        benchmarkSymbol: null,
        accountId: null,
        paramsJson: {},
        enabled: true,
      },
      null,
      null,
      null,
    );

    await expect(request).rejects.toThrow(
      '当前运行环境未提供模块 package_that_does_not_exist_123',
    );
    await expect(request).rejects.not.toThrow('errorDetail');
  });
});
