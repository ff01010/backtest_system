import { describe, expect, it } from 'vitest';
import {
  buildStrategyDraftFingerprint,
  buildSavedStrategyFingerprint,
  buildExperimentFingerprint,
  getConfigSectionForError,
  getResearchSignalSelectionError,
  getResearchSignalTimeframeError,
  getStrategySaveNoticeState,
  hasEnabledTradeDirection,
  hasLegacyTimeSlice,
  hasSavedStrategyId,
  isCustomScriptStrategy,
  serializeStrategyParameterValues,
  strategyParameterSchemaFromConfig,
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

  it('用红黄绿区分策略保存与实验参数状态', () => {
    expect(getStrategySaveNoticeState(false, false, false)).toEqual({
      tone: 'blocked',
      message: '当前为新建策略，尚未保存；请先保存后再运行回测。',
    });
    expect(getStrategySaveNoticeState(true, false, true)).toEqual({
      tone: 'blocked',
      message: '策略内容已修改，暂不可运行；请先保存后再运行回测。',
    });
    expect(getStrategySaveNoticeState(false, true, true)).toEqual({
      tone: 'warning',
      message: '仅本次实验参数已修改，可直接运行；不会影响已保存策略。',
    });
    expect(getStrategySaveNoticeState(false, false, true)).toEqual({
      tone: 'ready',
      message: '当前策略内容已保存，可直接运行回测。',
    });
  });

  it('识别应迁移的旧时间切片写法', () => {
    expect(hasLegacyTimeSlice("hhmm = bar['time'][8:12]")).toBe(true);
    expect(hasLegacyTimeSlice("hhmm = bar['hhmm']")).toBe(false);
  });

  it('研究信号选择必须与策略声明一致', () => {
    expect(getResearchSignalSelectionError(true, '')).toBe(
      '当前策略必须选择研究信号后才能运行'
    );
    expect(getResearchSignalSelectionError(false, 'dataset-1')).toBe(
      '当前策略未声明为研究信号驱动策略，不能附加研究信号'
    );
    expect(getResearchSignalSelectionError(true, 'dataset-1')).toBeNull();
    expect(getResearchSignalSelectionError(false, '')).toBeNull();
  });

  it('研究信号只能对齐到相同或更细的回测周期', () => {
    expect(getResearchSignalTimeframeError('1d', '5m')).toBeNull();
    expect(getResearchSignalTimeframeError('5m', '5m')).toBeNull();
    expect(getResearchSignalTimeframeError('1m', '5m')).toBe(
      '研究信号周期 1m 细于回测周期 5m，不能无损对齐'
    );
    expect(getResearchSignalTimeframeError('2h', '5m')).toBe('研究信号周期 2h 暂不受支持');
  });

  it('将运行前错误定位到对应的参数分组', () => {
    expect(getConfigSectionForError('当前日期范围数据不完整')).toBe('scope');
    expect(getConfigSectionForError('当前策略必须选择研究信号后才能运行')).toBe('inputs');
    expect(getConfigSectionForError('TWAP 执行时长必须是不少于 3 秒的整数')).toBe('execution');
    expect(getConfigSectionForError('初始资金必须大于 0')).toBe('capital');
    expect(getConfigSectionForError('启用初始底仓时底仓数量必须是正整数')).toBe('strategy');
  });

  it('研究信号声明属于策略草稿指纹', () => {
    const base = {
      strategy: 'user_script',
      mode: 'backtest',
      symbol: '688981.SH',
      timeframe: '5m',
      benchmarkSymbol: '688981.SH',
      accountId: '' as const,
      params: {},
      script: { code: 'def on_bar(bar, state): return []', entryType: 'on_bar', signalRequired: false },
    };
    const signalDriven = { ...base, script: { ...base.script, signalRequired: true } };
    expect(buildStrategyDraftFingerprint(base)).not.toBe(buildStrategyDraftFingerprint(signalDriven));
  });

  it('已声明策略参数只改变实验指纹，不改变版本指纹', () => {
    const base = {
      strategy: 'user_script',
      mode: 'backtest',
      symbol: '688981.SH',
      timeframe: '5m',
      benchmarkSymbol: '688981.SH',
      accountId: '' as const,
      params: { window: '20', allowLong: true, startDate: '2026-06-01' },
      strategyParameterNames: ['window'],
      script: { code: 'def on_bar(bar, state): return []', entryType: 'on_bar', signalRequired: false },
    };
    const override = { ...base, params: { ...base.params, window: '10' } };
    const structuralChange = { ...base, params: { ...base.params, allowLong: false } };

    expect(buildStrategyDraftFingerprint(base)).toBe(buildStrategyDraftFingerprint(override));
    expect(buildStrategyDraftFingerprint(base)).not.toBe(
      buildStrategyDraftFingerprint(structuralChange),
    );
    expect(buildExperimentFingerprint(base.params, ['window'])).not.toBe(
      buildExperimentFingerprint(override.params, ['window']),
    );
  });

  it('保存后立即运行不会被判定为未保存修改', () => {
    const saved = {
      id: 'config-1',
      name: '策略 A',
      scriptId: 'script-1',
      strategyVersionId: 'version-1',
      scriptVersionNo: 1,
      description: '说明',
      code: 'def on_bar(bar, state): return []',
      entryType: 'on_bar',
      signalRequired: false,
      strategy: 'user_script',
      mode: 'backtest',
      symbol: '688981.SH',
      timeframe: '5m',
      benchmarkSymbol: '688981.SH',
      accountId: null,
      paramsJson: {
        params: { startDate: '2026-08-27', endDate: '2026-08-28', window: 20 },
        strategyParameterDefinitions: { window: { type: 'integer', default: 20 } },
        strategyParams: { window: 20 },
      },
      enabled: true,
      published: true,
      publishedVersionId: 'config-version-1',
      revision: 1,
      createdAt: '2026-09-03 10:00:00',
      updatedAt: '2026-09-03 10:00:00',
    };
    const draft = buildStrategyDraftFingerprint({
      strategy: saved.strategy,
      mode: saved.mode,
      symbol: saved.symbol,
      timeframe: saved.timeframe,
      benchmarkSymbol: saved.benchmarkSymbol,
      accountId: '',
      params: saved.paramsJson.params,
      strategyParameterNames: ['window'],
      script: {
        code: saved.code,
        entryType: saved.entryType,
        signalRequired: saved.signalRequired,
      },
    });

    expect(buildSavedStrategyFingerprint(saved)).toBe(draft);
  });

  it('从完整策略生成四种基础参数控件并按声明类型序列化', () => {
    const config = {
      id: 'config-1', name: '参数策略', scriptId: 'script-1', strategyVersionId: 'version-1',
      scriptVersionNo: 1, description: '', code: 'def on_bar(bar, state): return []',
      entryType: 'on_bar', signalRequired: false, strategy: 'user_script', mode: 'backtest',
      symbol: '688981.SH', timeframe: '5m', benchmarkSymbol: null, accountId: null,
      paramsJson: {
        strategyParameterDefinitions: {
          window: { type: 'integer', default: 20 },
          threshold: { type: 'number', default: 0.5 },
          enabled: { type: 'boolean', default: true },
          label: { type: 'string', default: 'demo' },
        },
      },
      enabled: true, published: true, publishedVersionId: 'published-1', revision: 1,
      createdAt: '2026-09-28', updatedAt: '2026-09-28',
    };
    const schema = strategyParameterSchemaFromConfig(config);

    expect(Object.values(schema).map((field) => field.type)).toEqual([
      'integer', 'number', 'boolean', 'string',
    ]);
    expect(serializeStrategyParameterValues({
      window: '12', threshold: '0.25', enabled: false, label: 'test',
    }, schema)).toEqual({
      window: 12, threshold: 0.25, enabled: false, label: 'test',
    });
  });
});
