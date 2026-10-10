import type { EvaluationDimensionDefinition } from './contracts';

export type ScenarioValue = {
  dateScenarioId?: string;
  dateScenarioOrder?: number;
  role?: string;
  startDate?: string;
  endDate?: string;
  value?: unknown;
  runId?: string;
};
export type ComparisonCandidate = {
  candidateKey: string;
  dimensions?: Record<string, unknown>;
  values: ScenarioValue[];
};
export type ComparisonMetric = {
  metricId: string;
  candidates: ComparisonCandidate[];
};
export type Condition = { key: string; dimensions: Record<string, unknown> };
export type ScenarioName = { scenarioId: string; name: string; startDate: string; endDate: string };

export function frozenScenarioNames(plan: Record<string, unknown>): ScenarioName[] {
  if (!Array.isArray(plan.stages)) return [];
  return plan.stages.flatMap((stage) => Array.isArray(stage?.dateScenarios) ? stage.dateScenarios : [])
    .filter((item): item is ScenarioName => item && ['scenarioId', 'name', 'startDate', 'endDate']
      .every((key) => typeof item[key] === 'string'));
}

export function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function decimal(value: unknown): string {
  const number = numeric(value);
  return number === null ? '—' : number.toFixed(4);
}

export function dimensionValue(value: unknown, definition?: EvaluationDimensionDefinition): string {
  if (value === null || value === undefined) return '未记录';
  if (definition?.dataType === 'boolean' || typeof value === 'boolean') {
    return value === true || value === 'true' ? '是' : '否';
  }
  if (definition?.dataType === 'integer') return String(value);
  if (['decimal', 'number'].includes(definition?.dataType ?? '') || typeof value === 'number') {
    return decimal(value);
  }
  return String(value);
}

export function readComparisons(visualizations: Record<string, unknown>[]): ComparisonMetric[] {
  return visualizations.flatMap((item) => {
    if (item.type !== 'time_stability_v1' || !Array.isArray(item.candidates)) return [];
    const candidates = item.candidates.filter((candidate): candidate is ComparisonCandidate => (
      candidate && typeof candidate === 'object' && typeof candidate.candidateKey === 'string'
      && Array.isArray(candidate.values)
    ));
    return [{ metricId: String(item.metricId), candidates }];
  });
}

export function dimensionIds(candidates: ComparisonCandidate[]): string[] {
  return [...new Set(candidates.flatMap((item) => Object.keys(item.dimensions ?? {})))].sort();
}

export function curveAvailability(
  candidates: ComparisonCandidate[],
  parameterIds: string[],
  definitions: Map<string, EvaluationDimensionDefinition>,
): { axes: string[]; reason: string | null } {
  if (!candidates.length) return { axes: [], reason: '当前比较条件下没有候选结果。' };
  if (!parameterIds.length) return { axes: [], reason: '该报告未保存候选参数取值，无法绘制参数曲线。' };
  const numericIds = parameterIds.filter((id) => ['integer', 'number', 'decimal'].includes(definitions.get(id)?.dataType ?? ''));
  if (!numericIds.length) return { axes: [], reason: '当前报告没有可作为横轴的数值参数。' };
  // 只校验横轴；其他维度的空值仍保留在分组条件中，不能全局禁用或猜测默认值。
  const values = numericIds.map((id) => candidates.map((item) => numeric(item.dimensions?.[id])));
  const axes = numericIds.filter((_, index) => values[index].every((value) => value !== null)
    && new Set(values[index]).size > 1);
  if (axes.length) return { axes, reason: null };
  return { axes, reason: values.some((items) => items.includes(null))
    ? '数值参数取值缺失或无效，当前报告无法绘制参数曲线。'
    : '当前比较条件下，没有包含至少两个不同取值的数值参数。' };
}

export function orderedCandidates(
  candidates: ComparisonCandidate[],
  ids: string[],
  definitions: Map<string, EvaluationDimensionDefinition>,
): ComparisonCandidate[] {
  return [...candidates].sort((left, right) => {
    for (const id of ids) {
      const a = left.dimensions?.[id];
      const b = right.dimensions?.[id];
      if (a == null || b == null) {
        if (a != null || b != null) return a == null ? 1 : -1;
        continue;
      }
      const isNumber = ['integer', 'number', 'decimal'].includes(definitions.get(id)?.dataType ?? '');
      const difference = isNumber && numeric(a) !== null && numeric(b) !== null
        ? numeric(a)! - numeric(b)! : String(a).localeCompare(String(b), 'zh-CN', { numeric: true });
      if (difference) return difference;
    }
    return left.candidateKey.localeCompare(right.candidateKey);
  });
}

export function conditionKey(candidate: ComparisonCandidate, ids: string[]): string {
  // 缺失维度单独成组，不能与已知条件下的结果混合。
  return JSON.stringify(ids.map((id) => candidate.dimensions?.[id] ?? null));
}

export function conditionsFor(candidates: ComparisonCandidate[], ids: string[]): Condition[] {
  const groups = new Map<string, Condition>();
  candidates.forEach((candidate) => {
    const key = conditionKey(candidate, ids);
    groups.set(key, { key, dimensions: Object.fromEntries(ids.map((id) => [id, candidate.dimensions?.[id]])) });
  });
  return [...groups.values()];
}

export function scenarioKey(value: ScenarioValue): string {
  return JSON.stringify([value.dateScenarioId, value.startDate, value.endDate, value.role]);
}

export function scenariosFor(candidates: ComparisonCandidate[]): ScenarioValue[] {
  const scenarios = new Map<string, ScenarioValue>();
  candidates.flatMap((item) => item.values).forEach((value) => scenarios.set(scenarioKey(value), value));
  return [...scenarios.values()].sort((left, right) => (
    (left.dateScenarioOrder ?? 0) - (right.dateScenarioOrder ?? 0)
    || scenarioKey(left).localeCompare(scenarioKey(right))
  ));
}

export function scenarioName(value: ScenarioValue, names: ScenarioName[]): string {
  return names.find((item) => item.scenarioId === value.dateScenarioId
    && item.startDate === value.startDate && item.endDate === value.endDate)?.name
    ?? value.dateScenarioId ?? '未命名场景';
}

export function cellsFor(candidate: ComparisonCandidate, scenario: ScenarioValue): ScenarioValue[] {
  return candidate.values.filter((value) => scenarioKey(value) === scenarioKey(scenario));
}

export function displayExtent(candidates: ComparisonCandidate[]): [number, number] | null {
  const values = candidates.flatMap((item) => item.values)
    .map((item) => numeric(item.value)).filter((value): value is number => value !== null)
    .map((value) => Number(value.toFixed(4)));
  return values.length ? [Math.min(...values), Math.max(...values)] : null;
}

export function heatColor(value: unknown, extent: [number, number] | null): string | undefined {
  const number = numeric(value);
  if (number === null || !extent) return undefined;
  const [min, max] = extent;
  const ratio = min === max ? 0.5 : (Number(number.toFixed(4)) - min) / (max - min);
  return `color-mix(in srgb, var(--accent) ${Math.round(8 + Math.max(0, Math.min(1, ratio)) * 24)}%, var(--surface))`;
}
