import { describe, expect, it } from 'vitest';
import {
  dimensionDisplayLabel,
  executionAlgorithmLabel,
  resourceClassLabel,
  stageTypeLabel,
  statusLabel,
  systemDimensionValueLabel,
  visualizationTypeLabel,
} from '../features/experiments/displayLabels';

describe('experiment display labels', () => {
  it('localizes built-in execution, resource, stage and analysis enums', () => {
    expect(executionAlgorithmLabel('immediate')).toBe('立即成交');
    expect(resourceClassLabel('light')).toBe('轻量');
    expect(resourceClassLabel('medium')).toBe('中等');
    expect(resourceClassLabel('heavy')).toBe('较重');
    expect(stageTypeLabel('finite_grid_v1')).toBe('有限网格');
    expect(statusLabel('missing')).toBe('缺失');
    expect(visualizationTypeLabel('parameter_curve_v1')).toBe('参数曲线');
    expect(visualizationTypeLabel('cost_stress_v1')).toBe('成本压力');
  });

  it('uses readable labels for built-in evaluation dimension values', () => {
    const scenarios = [{ scenarioId: 'scenario-2', name: '震荡验证区间' }];
    expect(systemDimensionValueLabel('dateScenarioId', 'scenario-2', scenarios)).toBe('震荡验证区间');
    expect(systemDimensionValueLabel('costScenarioId', 'cost-2')).toBe('成本场景 2');
    expect(systemDimensionValueLabel('executionScenarioId', 'immediate-1')).toBe('立即成交（默认）');
    expect(systemDimensionValueLabel('dateRole', 'validation')).toBe('验证');
    expect(systemDimensionValueLabel('fillModel', 'bar_next_open')).toBe('Bar 下一根开盘价');
    expect(systemDimensionValueLabel('resultStatus', 'succeeded')).toBe('成功');
    expect(systemDimensionValueLabel('stageType', 'finite_grid_v1')).toBe('有限网格');
    expect(systemDimensionValueLabel('adjustmentMode', 'none')).toBe('不复权');
  });

  it('preserves unknown values and user-defined identifiers for callers to display', () => {
    expect(executionAlgorithmLabel('future_algorithm')).toBe('future_algorithm');
    expect(systemDimensionValueLabel('costScenarioId', 'user-baseline')).toBeNull();
    expect(systemDimensionValueLabel('parameter.strategyParameters.note', '研发')).toBeNull();
    expect(dimensionDisplayLabel('stageType', 'Stage 类型')).toBe('阶段类型');
    expect(dimensionDisplayLabel('custom', '用户字段')).toBe('用户字段');
  });
});
