const STATUS_LABELS: Record<string, string> = {
  preparing: '准备中',
  processing: '处理中',
  generation_blocked: '生成受阻',
  pending: '等待中',
  queued: '排队中',
  running: '运行中',
  cancelling: '取消中',
  cancel_requested: '取消中',
  completed: '完成',
  complete: '完成',
  partially_completed: '部分完成',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  expired: '已过期',
  missing: '缺失',
  not_generated: '未生成',
  materialized: '已生成',
  cancelled_before_materialization: '生成前已取消',
};

const FILL_MODEL_LABELS: Record<string, string> = {
  bar_next_open: 'Bar 下一根开盘价',
  l2_quote: 'L2 行情撮合',
};

const DATE_ROLE_LABELS: Record<string, string> = {
  development: '开发',
  validation: '验证',
  reference: '参考',
};

const EXECUTION_ALGORITHM_LABELS: Record<string, string> = {
  immediate: '立即成交',
};

const RESOURCE_CLASS_LABELS: Record<string, string> = {
  light: '轻量',
  medium: '中等',
  heavy: '较重',
};

const STAGE_TYPE_LABELS: Record<string, string> = {
  finite_grid_v1: '有限网格',
};

const ADJUSTMENT_MODE_LABELS: Record<string, string> = {
  none: '不复权',
  forward: '前复权',
  backward: '后复权',
  qfq: '前复权',
  hfq: '后复权',
};

const VISUALIZATION_TYPE_LABELS: Record<string, string> = {
  parameter_curve_v1: '参数曲线',
  parameter_heatmap_v1: '二维热力图',
  cost_stress_v1: '成本压力',
  time_stability_v1: '时间稳定性',
};

const DIMENSION_LABELS: Record<string, string> = {
  stageId: '阶段 ID',
  stageIndex: '阶段顺序',
  stageType: '阶段类型',
};

type ScenarioLabel = {
  scenarioId: string;
  name: string;
};

function scenarioNumber(value: string, prefix: string): string | null {
  const match = new RegExp(`^${prefix}-(\\d+)$`).exec(value);
  return match?.[1] ?? null;
}

export function statusLabel(value: unknown): string {
  const status = String(value ?? '');
  return (STATUS_LABELS[status] ?? status) || '—';
}

export function fillModelLabel(value: unknown): string {
  const fillModel = String(value ?? '');
  return (FILL_MODEL_LABELS[fillModel] ?? fillModel) || '—';
}

export function dateRoleLabel(value: unknown): string {
  const role = String(value ?? '');
  return (DATE_ROLE_LABELS[role] ?? role) || '—';
}

export function executionAlgorithmLabel(value: unknown): string {
  const algorithm = String(value ?? '');
  return (EXECUTION_ALGORITHM_LABELS[algorithm] ?? algorithm) || '—';
}

export function resourceClassLabel(value: unknown): string {
  const resourceClass = String(value ?? '');
  return (RESOURCE_CLASS_LABELS[resourceClass] ?? resourceClass) || '—';
}

export function stageTypeLabel(value: unknown): string {
  const stageType = String(value ?? '');
  return (STAGE_TYPE_LABELS[stageType] ?? stageType) || '—';
}

export function visualizationTypeLabel(value: unknown): string {
  const type = String(value ?? '');
  return (VISUALIZATION_TYPE_LABELS[type] ?? type) || '—';
}

export function dimensionDisplayLabel(dimensionId: string, fallback: string): string {
  return DIMENSION_LABELS[dimensionId] ?? fallback;
}

export function systemDimensionValueLabel(
  dimensionId: string,
  value: unknown,
  scenarios: ScenarioLabel[] = [],
): string | null {
  const raw = String(value ?? '');
  if (!raw) return null;
  if (dimensionId === 'dateScenarioId') {
    const scenario = scenarios.find((item) => item.scenarioId === raw);
    if (scenario) return scenario.name;
    const number = scenarioNumber(raw, 'scenario');
    return number ? `日期场景 ${number}` : null;
  }
  if (dimensionId === 'costScenarioId') {
    const number = scenarioNumber(raw, 'cost');
    return number ? `成本场景 ${number}` : null;
  }
  if (dimensionId === 'executionScenarioId') {
    if (raw === 'immediate') return '立即成交';
    const number = scenarioNumber(raw, 'immediate');
    if (number === '1') return '立即成交（默认）';
    return number ? `立即成交场景 ${number}` : null;
  }
  if (dimensionId === 'dateRole') return dateRoleLabel(value);
  if (dimensionId === 'fillModel') return fillModelLabel(value);
  if (dimensionId === 'resultStatus') return statusLabel(value);
  if (dimensionId === 'stageType') return stageTypeLabel(value);
  if (dimensionId === 'adjustmentMode') {
    return ADJUSTMENT_MODE_LABELS[raw] ?? null;
  }
  return null;
}
