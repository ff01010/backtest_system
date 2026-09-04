import { describe, expect, it } from 'vitest';
import { formatTradeDirection, formatTradeNote, getClosedLoopTradePnl } from '../components/BacktestPage';
import type { BacktestTrade } from '../types';

function makeTrade(overrides: Partial<BacktestTrade>): BacktestTrade {
  return {
    time: '2026-01-02 09:31:00',
    direction: 'BUY_OPEN',
    side: 'BUY',
    anchor: 0,
    price: 10,
    quantity: 100,
    pnl: 0,
    note: '',
    status: 'filled',
    ...overrides,
  };
}

describe('trade display', () => {
  it('将方向限制为买入或卖出', () => {
    expect(formatTradeDirection('BUY')).toBe('买入');
    expect(formatTradeDirection('SELL')).toBe('卖出');
  });

  it('将订单状态显示在说明中', () => {
    expect(formatTradeNote('SELL_OPEN', 'lower_peak_open_short')).toBe('低尖点确认，卖出开空');
    expect(formatTradeNote('BUY_CLOSE', 'stop')).toBe('触及止损价，买入平空');
    expect(formatTradeNote('BUY_COVER', 'CLOSE_LINE')).toBe('触及平仓线');
  });

  it('对资金受限成交保留策略说明并追加原因', () => {
    expect(formatTradeNote('BUY_OPEN', '开多', true)).toBe('开多；资金不足限制，已成交可负担部分');
  });

  it('方向性持仓只在平仓成交展示闭环收益', () => {
    expect(getClosedLoopTradePnl(makeTrade({ action: 'BUY_OPEN', pnl: 0 }), false)).toBeNull();
    expect(getClosedLoopTradePnl(makeTrade({ action: 'SELL_CLOSE', pnl: 198 }), false)).toBe(198);
    expect(getClosedLoopTradePnl(makeTrade({ action: 'SELL_CLOSE', pnl: 0 }), false)).toBe(0);
  });

  it('底仓增强使用增强周期收益而不是账户记账收益', () => {
    expect(getClosedLoopTradePnl(makeTrade({
      action: 'SELL_CLOSE',
      pnl: 100,
      cycleEffect: 'open',
      realizedOverlayPnl: 0,
    }), true)).toBeNull();
    expect(getClosedLoopTradePnl(makeTrade({
      action: 'BUY_OPEN',
      pnl: 0,
      cycleEffect: 'close',
      realizedOverlayPnl: 100,
    }), true)).toBe(100);
    expect(getClosedLoopTradePnl(makeTrade({
      status: 'skipped',
      cycleEffect: 'close',
      realizedOverlayPnl: 100,
    }), true)).toBeNull();
  });
});
