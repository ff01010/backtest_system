import { describe, expect, it } from 'vitest';
import {
  hasEnabledTradeDirection,
  hasLegacyTimeSlice,
  hasSavedStrategyId,
  isCustomScriptStrategy,
} from '../components/BacktestPage';

describe('isCustomScriptStrategy', () => {
  it('仅将自定义策略标识识别为受控脚本', () => {
    expect(isCustomScriptStrategy('user_script')).toBe(true);
    expect(isCustomScriptStrategy('trend_grid')).toBe(false);
  });

  it('要求策略至少启用一个交易方向', () => {
    expect(hasEnabledTradeDirection(true, false)).toBe(true);
    expect(hasEnabledTradeDirection(false, true)).toBe(true);
    expect(hasEnabledTradeDirection(true, true)).toBe(true);
    expect(hasEnabledTradeDirection(false, false)).toBe(false);
  });

  it('空策略 ID 不应被当作已保存的策略', () => {
    expect(hasSavedStrategyId('')).toBe(false);
    expect(hasSavedStrategyId('9f5e9cf6-83d5-44d5-a070-0d1d062d11aa')).toBe(true);
  });

  it('识别应迁移的旧时间切片写法', () => {
    expect(hasLegacyTimeSlice("hhmm = bar['time'][8:12]")).toBe(true);
    expect(hasLegacyTimeSlice("hhmm = bar['hhmm']")).toBe(false);
  });
});
