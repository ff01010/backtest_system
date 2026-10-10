import type {
  EvaluationAggregate,
  EvaluationCatalogs,
  EvaluationFormula,
  EvaluationSort,
  EvaluationVisualization,
} from './contracts';

export type BasicEvaluationDefaults = {
  columns: string[];
  groupBy: string[];
  aggregates: EvaluationAggregate[];
  sort: EvaluationSort[];
  visualizations: EvaluationVisualization[];
};

const BASIC_AGGREGATES: EvaluationAggregate[] = [
  {
    type: 'metric_aggregate_v1',
    aggregateId: 'medianAnnualSharpe',
    metricId: 'annualSharpe',
    function: 'median',
    percentile: null,
  },
  {
    type: 'metric_aggregate_v1',
    aggregateId: 'meanTotalReturn',
    metricId: 'totalReturn',
    function: 'mean',
    percentile: null,
  },
  {
    type: 'metric_aggregate_v1',
    aggregateId: 'worstMaxDrawdownRate',
    metricId: 'maxDrawdownRate',
    function: 'max',
    percentile: null,
  },
  {
    type: 'metric_aggregate_v1',
    aggregateId: 'annualSharpeDispersion',
    metricId: 'annualSharpe',
    function: 'sample_stddev',
    percentile: null,
  },
  {
    type: 'metric_aggregate_v1',
    aggregateId: 'successfulTrialCount',
    metricId: 'totalReturn',
    function: 'count',
    percentile: null,
  },
];

export const BASIC_AGGREGATE_LABELS: Record<string, string> = {
  medianAnnualSharpe: '夏普中位数',
  meanTotalReturn: '平均总收益率',
  worstMaxDrawdownRate: '最差最大回撤率',
  annualSharpeDispersion: '夏普离散度',
  successfulTrialCount: '成功样本数',
};

export function buildBasicEvaluationDefaults(
  catalogs: EvaluationCatalogs,
): BasicEvaluationDefaults {
  const metricIds = new Set(catalogs.metricCatalog.metrics.map((item) => item.metricId));
  const aggregates = BASIC_AGGREGATES.filter((item) => metricIds.has(item.metricId));
  const groupBy = catalogs.dimensionCatalog.dimensions
    .filter((item) => item.dimensionId.startsWith('parameter.'))
    .map((item) => item.dimensionId);
  // 场景明细直接驱动矩阵和曲线；即使只有一个日期，也保留实际观测值。
  const visualizations: EvaluationVisualization[] = ['annualSharpe', 'totalReturn', 'maxDrawdownRate']
    .filter((metricId) => metricIds.has(metricId))
    .map((metricId) => ({ type: 'time_stability_v1', metricId }));
  const costDimension = catalogs.dimensionCatalog.dimensions.find(
    (item) => item.dimensionId === 'costScenarioId',
  );
  if ((costDimension?.orderedValues?.length ?? 0) > 1) {
    visualizations.push({
      type: 'cost_stress_v1',
      metricId: 'totalReturn',
      baselineScenarioId: String(costDimension?.orderedValues?.[0]),
      degradationThreshold: '0.1000',
    });
  }
  const sort: EvaluationSort[] = [
    ['medianAnnualSharpe', 'desc'],
    ['worstMaxDrawdownRate', 'asc'],
    ['annualSharpeDispersion', 'asc'],
  ].flatMap(([metricId, direction]) => (
    aggregates.some((item) => item.aggregateId === metricId)
      ? [{ type: 'metric_sort_v1' as const, metricId, direction: direction as 'asc' | 'desc' }]
      : []
  ));
  return {
    columns: [...groupBy, ...aggregates.map((item) => item.aggregateId)],
    groupBy,
    aggregates,
    sort,
    visualizations: visualizations.slice(0, 16),
  };
}

const SAFE_FUNCTIONS = new Set(['abs', 'min', 'max', 'coalesce']);
const FORMULA_CHARACTERS = /^[A-Za-z0-9_+\-*/().,\s]+$/;
const IDENTIFIER = /[A-Za-z][A-Za-z0-9_]*/g;
const LITERAL_ZERO_DIVISOR = /\/\s*[+-]?0+(?:\.0+)?\s*(?=$|[),+\-*/])/;

export function validateFormulaDraft(
  formula: EvaluationFormula,
  allowedMetricIds: string[],
): string | null {
  const expression = formula.formula.trim();
  if (!expression) return '公式不能为空';
  if (!FORMULA_CHARACTERS.test(expression)) return '公式包含不允许的字符';
  let depth = 0;
  for (const character of expression) {
    if (character === '(') depth += 1;
    if (character === ')') depth -= 1;
    if (depth < 0) return '公式括号不匹配';
  }
  if (depth !== 0) return '公式括号不匹配';
  if (LITERAL_ZERO_DIVISOR.test(expression)) return '公式存在明确的除零';
  const allowed = new Set(allowedMetricIds);
  const unknown = [...expression.matchAll(IDENTIFIER)]
    .map((match) => match[0])
    .filter((name) => !SAFE_FUNCTIONS.has(name) && !allowed.has(name));
  if (unknown.length > 0) return `未知指标：${unknown[0]}`;
  const invalidCall = [...expression.matchAll(IDENTIFIER)].find((match) => {
    const name = match[0];
    const suffix = expression.slice((match.index ?? 0) + name.length);
    return /^\s*\(/.test(suffix) && !SAFE_FUNCTIONS.has(name);
  });
  if (invalidCall) return `不允许调用：${invalidCall[0]}`;
  if (/^[*/]|[+\-*/]\s*$|[+\-*/]\s*[+*/]/.test(expression)) return '公式运算符不完整';
  return null;
}

export function metricMeaning(metricId: string): string {
  const meanings: Record<string, string> = {
    totalPnl: '期末权益相对初始资金的总盈亏金额',
    finalEquity: '回测结束时的账户权益',
    totalReturn: '完整回测区间的累计收益率',
    annualReturn: '按当前指标口径折算的年化收益率',
    maxDrawdown: '权益曲线从峰值到谷值的最大金额回撤',
    maxDrawdownRate: '权益曲线最大回撤比例',
    annualVolatility: '按 252 个交易日折算的年化波动率',
    annualSharpe: '按 252 个交易日折算的年化夏普比率',
    sortino: '仅惩罚下行波动的风险调整收益指标',
    completedTradeCount: '已完成闭环交易的数量',
    winRate: '盈利闭环交易占比',
    profitLossRatio: '平均盈利与平均亏损绝对值之比',
    benchmarkReturn: '冻结基准在同一区间的收益率',
    excessReturn: '策略总收益率减基准收益率',
    queueDurationMs: '任务从排队到开始执行的毫秒数',
    runDurationMs: '任务实际执行耗时毫秒数',
  };
  return meanings[metricId] ?? '评价目录提供的数值指标';
}
