import { describe, expect, it } from 'vitest';
import { formatTradeDirection, formatTradeNote } from '../components/BacktestPage';

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
});
