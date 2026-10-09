import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Copy, Download, Play, Plus, RefreshCw, RotateCcw, Save, Trash2, X } from 'lucide-react';
import { createBrowserUuid } from '../../utils/browserUuid';
import { statusLabel } from './displayLabels';
import EvaluationComparison from './EvaluationComparison';
import type { ScenarioName } from './comparisonData';
import {
  cancelEvaluationExport,
  createEvaluationExport,
  createEvaluationRun,
  evaluationExportDownloadUrl,
  fetchEvaluationCatalogs,
  fetchEvaluationExports,
  fetchEvaluationRun,
  fetchEvaluationRuns,
  fetchEvaluationSpecs,
  retryEvaluationExport,
  retryEvaluationRun,
  saveEvaluationSpec,
} from './api';
import type {
  EvaluationCatalogs,
  EvaluationAggregate,
  EvaluationDimensionDefinition,
  EvaluationExport,
  EvaluationExportFormat,
  EvaluationFilter,
  EvaluationFormula,
  EvaluationMetricDefinition,
  EvaluationRun,
  EvaluationRunDetail,
  EvaluationSort,
  EvaluationSpecPayload,
  EvaluationVisualization,
  SavedEvaluationSpec,
} from './contracts';
import {
  BASIC_AGGREGATE_LABELS,
  buildBasicEvaluationDefaults,
  metricMeaning,
  validateFormulaDraft,
} from './evaluationDefaults';

type Props = {
  experimentId: string;
  symbol: string;
  timeframe: string;
  scenarioNames?: ScenarioName[];
  trialSummary?: {
    planned: number;
    generated: number;
    succeeded: number;
    failed: number;
    cancelled: number;
  };
  onRetryFailed?: () => void;
  onOpenRun?: (runId: string, symbol: string, timeframe: string, date: string) => void;
};

const DEFAULT_METRICS = ['totalReturn', 'annualSharpe', 'maxDrawdownRate'];
const NULL_OPERATORS = new Set(['is_null', 'is_not_null']);

type MetricOption = {
  metricId: string;
  displayName: string;
  unit?: string | null;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '评价操作失败';
}

function metricLabel(metric: MetricOption): string {
  return metric.unit ? `${metric.displayName}（${metric.unit}）` : metric.displayName;
}

function formatDateTime(value: string | null): string {
  if (!value) return '尚未完成';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

type EvaluationDataType = 'decimal' | 'integer' | 'string' | 'boolean';
const FOUR_DECIMAL_FORMATTER = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
  useGrouping: false,
});

function numericValue(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatDecimal(value: unknown): string {
  const numeric = numericValue(value);
  return numeric === null ? '—' : FOUR_DECIMAL_FORMATTER.format(numeric);
}

function formatEvaluationValue(
  value: unknown,
  fieldId: string,
  dataTypes: Map<string, EvaluationDataType>,
): string {
  if (value === null || value === undefined || value === '') return '—';
  const dataType = dataTypes.get(fieldId);
  if (dataType === 'boolean') return value === true || value === 'true' ? '是' : '否';
  if (dataType === 'integer') return String(value);
  if (dataType === 'string') return String(value);
  if (dataType === 'decimal' || numericValue(value) !== null) return formatDecimal(value);
  return String(value);
}

function buildSpec(
  catalogs: EvaluationCatalogs,
  name: string,
  columns: string[],
  filters: EvaluationFilter[],
  sort: EvaluationSort[],
  allowPartial: boolean,
  minimumCompletionRate: string,
  requiredDimensionCoverage: EvaluationSpecPayload['completeness']['requiredDimensionCoverage'],
  derivedMetrics: EvaluationFormula[],
  groupBy: string[],
  aggregates: EvaluationAggregate[],
  groupFilters: EvaluationSpecPayload['groupFilters'],
  visualizations: EvaluationVisualization[],
  normalizerVersion: EvaluationSpecPayload['catalogs']['normalizerVersion'] = null,
): EvaluationSpecPayload {
  return {
    schemaVersion: 'evaluation_spec_v1',
    name: name.trim(),
    source: {
      experimentRevisionId: catalogs.experimentRevisionId,
      stageIds: [catalogs.stages[0].stageId],
      executionSelection: 'selected_execution_at_run_creation',
    },
    catalogs: {
      metricCatalogVersion: 'metric_catalog_v1',
      dimensionCatalogVersion: 'dimension_catalog_v1',
      normalizerVersion,
    },
    completeness: {
      allowPartial,
      minimumCompletionRate: allowPartial ? minimumCompletionRate : '1.0000',
      requiredDimensionCoverage: allowPartial ? requiredDimensionCoverage : [],
    },
    columns,
    derivedMetrics,
    filters,
    groupBy,
    aggregates,
    groupFilters,
    sort,
    visualizations,
    reportSections: ['completeness', 'ranking', 'analysis', 'exceptions', 'dataset_notice'],
  };
}

function CoverageEditor({
  coverage,
  dimensions,
  onChange,
}: {
  coverage: EvaluationSpecPayload['completeness']['requiredDimensionCoverage'];
  dimensions: EvaluationDimensionDefinition[];
  onChange: (coverage: EvaluationSpecPayload['completeness']['requiredDimensionCoverage']) => void;
}) {
  const update = (index: number, change: Partial<(typeof coverage)[number]>) => {
    onChange(coverage.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } : item
    )));
  };
  const available = dimensions.filter((dimension) => (
    !coverage.some((item) => item.dimensionId === dimension.dimensionId)
  ));
  return (
    <div className="evaluation-rules">
      {coverage.map((item, index) => (
        <div className="evaluation-rule" key={item.dimensionId}>
          <select value={item.dimensionId} onChange={(event) => update(index, { dimensionId: event.target.value })}>
            {dimensions.filter((dimension) => (
              dimension.dimensionId === item.dimensionId
              || !coverage.some((entry) => entry.dimensionId === dimension.dimensionId)
            )).map((dimension) => <option value={dimension.dimensionId} key={dimension.dimensionId}>{dimension.displayName}</option>)}
          </select>
          <input aria-label={`最低覆盖率 ${item.dimensionId}`} value={item.minimumCoverageRate} onChange={(event) => update(index, { minimumCoverageRate: event.target.value })} />
          <button className="app-icon-button" type="button" aria-label="删除维度覆盖" onClick={() => onChange(coverage.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14} /></button>
        </div>
      ))}
      <button className="app-button is-quiet" type="button" disabled={available.length === 0} onClick={() => {
        const dimension = available[0];
        if (dimension) onChange([...coverage, { dimensionId: dimension.dimensionId, minimumCoverageRate: '1.0000' }]);
      }}><Plus size={14} /> 添加维度覆盖</button>
    </div>
  );
}

function FilterEditor({
  filters,
  metrics,
  onChange,
}: {
  filters: EvaluationFilter[];
  metrics: MetricOption[];
  onChange: (filters: EvaluationFilter[]) => void;
}) {
  const update = (index: number, change: Partial<EvaluationFilter>) => {
    onChange(filters.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } : item
    )));
  };
  return (
    <div className="evaluation-rules">
      {filters.map((filter, index) => (
        <div className="evaluation-rule" key={`${filter.metricId}-${index}`}>
          <select value={filter.metricId} onChange={(event) => update(index, { metricId: event.target.value })}>
            {metrics.map((metric) => <option value={metric.metricId} key={metric.metricId}>{metricLabel(metric)}</option>)}
          </select>
          <select
            value={filter.operator}
            onChange={(event) => update(index, {
              operator: event.target.value as EvaluationFilter['operator'],
              value: NULL_OPERATORS.has(event.target.value) ? null : filter.value ?? '0',
            })}
          >
            {['>=', '<=', '>', '<', '=', '!=', 'is_null', 'is_not_null'].map((operator) => (
              <option value={operator} key={operator}>{operator}</option>
            ))}
          </select>
          {!NULL_OPERATORS.has(filter.operator) && (
            <input value={filter.value ?? ''} onChange={(event) => update(index, { value: event.target.value })} />
          )}
          <button className="app-icon-button" type="button" aria-label="删除过滤条件" onClick={() => onChange(filters.filter((_, itemIndex) => itemIndex !== index))}>
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <button className="app-button is-quiet" type="button" disabled={metrics.length === 0} onClick={() => onChange([...filters, {
        type: 'metric_comparison_v1',
        metricId: metrics[0]?.metricId ?? 'totalReturn',
        operator: '>=',
        value: '0',
      }])}>
        <Plus size={14} /> 添加过滤
      </button>
    </div>
  );
}

function SortEditor({
  sort,
  metrics,
  onChange,
}: {
  sort: EvaluationSort[];
  metrics: MetricOption[];
  onChange: (sort: EvaluationSort[]) => void;
}) {
  const update = (index: number, change: Partial<EvaluationSort>) => {
    onChange(sort.map((item, itemIndex) => itemIndex === index ? { ...item, ...change } : item));
  };
  return (
    <div className="evaluation-rules">
      {sort.map((item, index) => (
        <div className="evaluation-rule" key={`${item.metricId}-${index}`}>
          <span className="evaluation-priority">{index + 1}</span>
          <select value={item.metricId} onChange={(event) => update(index, { metricId: event.target.value })}>
            {metrics.map((metric) => <option value={metric.metricId} key={metric.metricId}>{metricLabel(metric)}</option>)}
          </select>
          <select value={item.direction} onChange={(event) => update(index, { direction: event.target.value as 'asc' | 'desc' })}>
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </select>
          <button className="app-icon-button" type="button" aria-label="删除排序条件" onClick={() => onChange(sort.filter((_, itemIndex) => itemIndex !== index))}>
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <button className="app-button is-quiet" type="button" disabled={metrics.length === 0} onClick={() => onChange([...sort, {
        type: 'metric_sort_v1',
        metricId: metrics[0]?.metricId ?? 'totalReturn',
        direction: 'desc',
      }])}>
        <Plus size={14} /> 添加排序
      </button>
    </div>
  );
}

function FormulaEditor({
  formulas,
  baseMetrics,
  aggregates,
  onChange,
}: {
  formulas: EvaluationFormula[];
  baseMetrics: EvaluationMetricDefinition[];
  aggregates: EvaluationAggregate[];
  onChange: (formulas: EvaluationFormula[]) => void;
}) {
  const update = (index: number, change: Partial<EvaluationFormula>) => {
    onChange(formulas.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } : item
    )));
  };
  return (
    <div className="evaluation-rules">
      {formulas.map((formula, index) => {
        const priorRowMetrics = formulas.slice(0, index)
          .filter((item) => item.type === 'row_formula_v1')
          .map((item) => item.metricId);
        const available = formula.type === 'row_formula_v1'
          ? [
            ...baseMetrics.filter((item) => item.dataType !== 'string').map((item) => item.metricId),
            ...priorRowMetrics,
          ]
          : aggregates.map((item) => item.aggregateId);
        const validationError = validateFormulaDraft(formula, available);
        return (
          <div className="evaluation-formula-block" key={`${formula.metricId}-${index}`}>
            <div className="evaluation-rule evaluation-rule-formula">
              <select value={formula.type} onChange={(event) => update(index, { type: event.target.value as EvaluationFormula['type'] })}>
                <option value="row_formula_v1">行公式</option>
                <option value="group_formula_v1">分组公式</option>
              </select>
              <input aria-label="派生指标 ID" value={formula.metricId} onChange={(event) => update(index, { metricId: event.target.value })} />
              <input aria-label="派生指标名称" value={formula.displayName} onChange={(event) => update(index, { displayName: event.target.value })} />
              <input aria-label="安全公式" value={formula.formula} onChange={(event) => update(index, { formula: event.target.value })} />
              <button className="app-icon-button" type="button" aria-label="删除派生公式" onClick={() => onChange(formulas.filter((_, itemIndex) => itemIndex !== index))}>
                <Trash2 size={14} />
              </button>
            </div>
            <div className="evaluation-formula-catalog" aria-label={`公式可用指标 ${formula.metricId}`}>
              {formula.type === 'row_formula_v1'
                ? baseMetrics.filter((item) => item.dataType !== 'string').map((metric) => (
                  <button
                    key={metric.metricId}
                    type="button"
                    title={`${metric.displayName} · ${metric.unit ?? '无单位'} · ${metricMeaning(metric.metricId)}`}
                    onClick={() => update(index, { formula: `${formula.formula}${formula.formula.trim() ? ' ' : ''}${metric.metricId}` })}
                  >
                    <strong>{metric.displayName}</strong><code>{metric.metricId}</code><span>{metric.unit ?? '无单位'}</span>
                  </button>
                ))
                : aggregates.map((aggregate) => (
                  <button
                    key={aggregate.aggregateId}
                    type="button"
                    title="分组公式只能引用已定义的聚合指标"
                    onClick={() => update(index, { formula: `${formula.formula}${formula.formula.trim() ? ' ' : ''}${aggregate.aggregateId}` })}
                  >
                    <strong>{BASIC_AGGREGATE_LABELS[aggregate.aggregateId] ?? aggregate.aggregateId}</strong>
                    <code>{aggregate.aggregateId}</code>
                  </button>
                ))}
            </div>
            {validationError && <span className="evaluation-inline-error" role="alert">{validationError}</span>}
          </div>
        );
      })}
      <button className="app-button is-quiet" type="button" onClick={() => onChange([...formulas, {
        type: 'row_formula_v1',
        metricId: `customMetric${formulas.length + 1}`,
        displayName: `派生指标 ${formulas.length + 1}`,
        formula: 'coalesce(totalReturn, 0)',
        precision: 8,
        direction: 'neutral',
      }])}>
        <Plus size={14} /> 添加安全公式
      </button>
    </div>
  );
}

function AggregateEditor({
  aggregates,
  metrics,
  onChange,
}: {
  aggregates: EvaluationAggregate[];
  metrics: MetricOption[];
  onChange: (aggregates: EvaluationAggregate[]) => void;
}) {
  const update = (index: number, change: Partial<EvaluationAggregate>) => {
    onChange(aggregates.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } : item
    )));
  };
  return (
    <div className="evaluation-rules">
      {aggregates.map((aggregate, index) => (
        <div className="evaluation-rule evaluation-rule-aggregate" key={`${aggregate.aggregateId}-${index}`}>
          <input aria-label="聚合指标 ID" value={aggregate.aggregateId} onChange={(event) => update(index, { aggregateId: event.target.value })} />
          <select value={aggregate.metricId} onChange={(event) => update(index, { metricId: event.target.value })}>
            {metrics.map((metric) => <option value={metric.metricId} key={metric.metricId}>{metricLabel(metric)}</option>)}
          </select>
          <select value={aggregate.function} onChange={(event) => update(index, {
            function: event.target.value as EvaluationAggregate['function'],
            percentile: event.target.value === 'nearest_rank_percentile' ? aggregate.percentile ?? '0.5' : null,
          })}>
            <option value="count">计数</option>
            <option value="mean">均值</option>
            <option value="median">中位数</option>
            <option value="min">最小值</option>
            <option value="max">最大值</option>
            <option value="sample_stddev">样本标准差</option>
            <option value="nearest_rank_percentile">最近秩分位数</option>
          </select>
          {aggregate.function === 'nearest_rank_percentile' && <input aria-label="分位数" value={aggregate.percentile ?? '0.5'} onChange={(event) => update(index, { percentile: event.target.value })} />}
          <button className="app-icon-button" type="button" aria-label="删除聚合指标" onClick={() => onChange(aggregates.filter((_, itemIndex) => itemIndex !== index))}>
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <button className="app-button is-quiet" type="button" onClick={() => onChange([...aggregates, {
        type: 'metric_aggregate_v1',
        aggregateId: `aggregate${aggregates.length + 1}`,
        metricId: metrics[0]?.metricId ?? 'totalReturn',
        function: 'mean',
        percentile: null,
      }])}>
        <Plus size={14} /> 添加聚合
      </button>
    </div>
  );
}

function VisualizationEditor({
  visualizations,
  metrics,
  dimensions,
  onChange,
}: {
  visualizations: EvaluationVisualization[];
  metrics: MetricOption[];
  dimensions: EvaluationDimensionDefinition[];
  onChange: (visualizations: EvaluationVisualization[]) => void;
}) {
  const parameters = dimensions.filter((item) => (
    item.dimensionId.startsWith('parameter.')
    && ['integer', 'number', 'decimal'].includes(item.dataType)
  ));
  const cost = dimensions.find((item) => item.dimensionId === 'costScenarioId');
  const varyingExperimentDimensions = dimensions.filter((item) => (
    ['dateScenarioId', 'initialCapital', 'costScenarioId', 'executionScenarioId'].includes(item.dimensionId)
    && (item.orderedValues?.length ?? 0) > 1
  )).map((item) => item.dimensionId);
  const seriesFor = (axis: string) => [
    ...parameters.filter((item) => item.dimensionId !== axis && (item.orderedValues?.length ?? 0) > 1).map((item) => item.dimensionId),
    ...varyingExperimentDimensions,
  ];
  const facetsFor = (xAxis: string, yAxis: string) => [
    ...parameters.filter((item) => ![xAxis, yAxis].includes(item.dimensionId) && (item.orderedValues?.length ?? 0) > 1).map((item) => item.dimensionId),
    ...varyingExperimentDimensions,
  ];
  const add = (type: EvaluationVisualization['type']) => {
    const metricId = metrics[0]?.metricId ?? 'totalReturn';
    if (type === 'parameter_curve_v1' && parameters[0]) {
      const seriesBy = seriesFor(parameters[0].dimensionId);
      onChange([...visualizations, { type, parameterDimensionId: parameters[0].dimensionId, metricId, seriesBy }]);
    } else if (type === 'parameter_heatmap_v1' && parameters[0] && parameters[1]) {
      const facetBy = facetsFor(parameters[0].dimensionId, parameters[1].dimensionId);
      onChange([...visualizations, { type, xDimensionId: parameters[0].dimensionId, yDimensionId: parameters[1].dimensionId, metricId, facetBy }]);
    } else if (type === 'cost_stress_v1') {
      onChange([...visualizations, { type, metricId, baselineScenarioId: String(cost?.orderedValues?.[0] ?? 'cost-1'), degradationThreshold: '0.1000' }]);
    } else if (type === 'time_stability_v1') {
      onChange([...visualizations, { type, metricId }]);
    }
  };
  const update = (index: number, change: Partial<EvaluationVisualization>) => {
    onChange(visualizations.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } as EvaluationVisualization : item
    )));
  };
  return (
    <div className="evaluation-rules">
      {visualizations.map((item, index) => (
        <div className="evaluation-rule" key={`${item.type}-${index}`}>
          <strong>{item.type}</strong>
          <select aria-label="分析指标" value={item.metricId} onChange={(event) => update(index, { metricId: event.target.value })}>
            {metrics.map((metric) => <option key={metric.metricId} value={metric.metricId}>{metricLabel(metric)}</option>)}
          </select>
          {item.type === 'parameter_curve_v1' && <select aria-label="曲线参数" value={item.parameterDimensionId} onChange={(event) => update(index, { parameterDimensionId: event.target.value, seriesBy: seriesFor(event.target.value) })}>{parameters.map((parameter) => <option key={parameter.dimensionId} value={parameter.dimensionId}>{parameter.displayName}</option>)}</select>}
          {item.type === 'parameter_heatmap_v1' && <span className="evaluation-axis-pair"><select aria-label="热力图横轴" value={item.xDimensionId} onChange={(event) => update(index, { xDimensionId: event.target.value, facetBy: facetsFor(event.target.value, item.yDimensionId) })}>{parameters.map((parameter) => <option key={parameter.dimensionId} value={parameter.dimensionId}>{parameter.displayName}</option>)}</select><select aria-label="热力图纵轴" value={item.yDimensionId} onChange={(event) => update(index, { yDimensionId: event.target.value, facetBy: facetsFor(item.xDimensionId, event.target.value) })}>{parameters.map((parameter) => <option key={parameter.dimensionId} value={parameter.dimensionId}>{parameter.displayName}</option>)}</select></span>}
          {item.type === 'cost_stress_v1' && <><select aria-label="成本基线" value={item.baselineScenarioId} onChange={(event) => update(index, { baselineScenarioId: event.target.value })}>{(cost?.orderedValues ?? []).map((value) => <option key={String(value)} value={String(value)}>{String(value)}</option>)}</select><input aria-label="成本退化阈值" value={item.degradationThreshold ?? ''} onChange={(event) => update(index, { degradationThreshold: event.target.value || null })} /></>}
          <button className="app-icon-button" type="button" aria-label="删除分析图" onClick={() => onChange(visualizations.filter((_, itemIndex) => itemIndex !== index))}>
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <div className="evaluation-visualization-actions">
        <button className="app-button is-quiet" type="button" disabled={parameters.length < 1} onClick={() => add('parameter_curve_v1')}>参数曲线</button>
        <button className="app-button is-quiet" type="button" disabled={parameters.length < 2} onClick={() => add('parameter_heatmap_v1')}>二维热力图</button>
        <button className="app-button is-quiet" type="button" onClick={() => add('cost_stress_v1')}>成本压力</button>
        <button className="app-button is-quiet" type="button" onClick={() => add('time_stability_v1')}>时间稳定性</button>
      </div>
    </div>
  );
}

type VisualizationProps = {
  item: Record<string, unknown>;
  labels: Map<string, string>;
  dataTypes: Map<string, EvaluationDataType>;
};

function LegacyParameterResult({ item, labels, dataTypes }: VisualizationProps) {
  const series = (item.series ?? []) as Array<{ seriesKey: string; dimensions?: Record<string, unknown>; points: Array<Record<string, unknown>> }>;
  const axis = String(item.parameterDimensionId);
  return <article className="evaluation-analysis-card">
    <h4>{labels.get(axis) ?? axis} · {labels.get(String(item.metricId)) ?? String(item.metricId)}</h4>
    {series.map((group, index) => <div key={group.seriesKey ?? index}>
      <p>{Object.entries(group.dimensions ?? {}).map(([id, value]) => `${labels.get(id) ?? id}=${formatEvaluationValue(value, id, dataTypes)}`).join(' · ') || `历史分组 ${index + 1}（未保存条件）`}</p>
      <table className="evaluation-table"><thead><tr><th>{labels.get(axis) ?? axis}</th><th>中位数</th><th>样本数</th></tr></thead>
        <tbody>{group.points.map((point, pointIndex) => <tr key={pointIndex}><td>{formatEvaluationValue(point.x, axis, dataTypes)}</td><td>{formatDecimal(point.median)}</td><td>{String(point.count ?? '—')}</td></tr>)}</tbody>
      </table>
    </div>)}
  </article>;
}

function VisualizationResult(props: VisualizationProps) {
  const { item, labels, dataTypes } = props;
  if (item.type === 'parameter_curve_v1') return <LegacyParameterResult {...props} />;
  if (item.type === 'parameter_heatmap_v1') {
    const facets = item.facets as Array<{ facetKey: string; cells: Array<Record<string, unknown>> }>;
    return <article className="evaluation-analysis-card"><h4>二维参数对比 · {labels.get(String(item.metricId)) ?? String(item.metricId)}</h4>{facets.map((facet) => <div key={facet.facetKey} className="evaluation-heatmap">{facet.cells.map((cell) => <span key={`${String(cell.x)}-${String(cell.y)}`} title={`样本 ${String(cell.count)}`}>{formatDecimal(cell.x)} × {formatDecimal(cell.y)}<strong>{formatDecimal(cell.median)}</strong></span>)}</div>)}</article>;
  }
  const comparisons = item.comparisons as Array<Record<string, unknown>>;
  return <article className="evaluation-analysis-card"><h4>成本压力 · {labels.get(String(item.metricId)) ?? String(item.metricId)}</h4><div className="evaluation-point-list">{comparisons.map((entry, index) => <span key={`${String(entry.pressureRunId)}-${index}`}>{String(entry.baselineScenarioId)} → {String(entry.pressureScenarioId)}：{formatDecimal(entry.delta)}</span>)}</div></article>;
}

const EXPORT_STATUS_LABELS: Record<EvaluationExport['status'], string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  failed: '失败',
  cancel_requested: '取消中',
  cancelled: '已取消',
  expired: '已过期',
};

function EvaluationExports({
  detail,
  labels,
}: {
  detail: EvaluationRunDetail;
  labels: Map<string, string>;
}) {
  const outputColumns = detail.output?.columns ?? [];
  const [items, setItems] = useState<EvaluationExport[]>([]);
  const [format, setFormat] = useState<EvaluationExportFormat>('xlsx');
  const [columns, setColumns] = useState<string[]>(outputColumns);
  const [locale, setLocale] = useState<'zh-CN' | 'en-US'>('zh-CN');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const createIntentRef = useRef<{ signature: string; key: string } | null>(null);
  const commandKeysRef = useRef(new Map<string, string>());

  const refresh = useCallback(async () => {
    const result = await fetchEvaluationExports(detail.evaluationRunId);
    setItems(result.items);
    return result.items;
  }, [detail.evaluationRunId]);

  useEffect(() => {
    setColumns(outputColumns);
  }, [detail.evaluationRunId, outputColumns.join('|')]);

  useEffect(() => {
    let stopped = false;
    const load = async () => {
      try {
        await refresh();
        if (stopped) return;
        setError('');
      } catch (reason) {
        if (!stopped) setError(messageOf(reason));
      }
    };
    void load();
    return () => { stopped = true; };
  }, [refresh]);

  const hasActiveExport = items.some((item) => (
    ['queued', 'running', 'cancel_requested'].includes(item.status)
  ));

  useEffect(() => {
    if (!hasActiveExport) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      try {
        await refresh();
        if (!stopped) setError('');
      } catch (reason) {
        if (!stopped) setError(messageOf(reason));
      } finally {
        if (!stopped) timer = setTimeout(() => { void poll(); }, 1500);
      }
    };
    timer = setTimeout(() => { void poll(); }, 1500);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [hasActiveExport, refresh]);

  const create = async () => {
    if (busy || columns.length === 0) return;
    setBusy(true); setError('');
    const signature = JSON.stringify({ format, columns, locale });
    const previous = createIntentRef.current;
    const key = previous?.signature === signature ? previous.key : createBrowserUuid();
    createIntentRef.current = { signature, key };
    try {
      await createEvaluationExport(
        detail.evaluationRunId, format, columns, locale, key,
      );
      createIntentRef.current = null;
      await refresh();
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (exportId: string) => {
    setBusy(true); setError('');
    const command = `cancel:${exportId}`;
    const key = commandKeysRef.current.get(command) ?? createBrowserUuid();
    commandKeysRef.current.set(command, key);
    try {
      await cancelEvaluationExport(exportId, key);
      commandKeysRef.current.delete(command);
      await refresh();
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  const retry = async (exportId: string) => {
    setBusy(true); setError('');
    const command = `retry:${exportId}`;
    const key = commandKeysRef.current.get(command) ?? createBrowserUuid();
    commandKeysRef.current.set(command, key);
    try {
      await retryEvaluationExport(exportId, key);
      commandKeysRef.current.delete(command);
      await refresh();
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="evaluation-export-panel" aria-label="评价导出">
      <div className="evaluation-export-heading"><h3>异步导出</h3></div>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      <div className="evaluation-export-form">
        <label>格式<select aria-label="导出格式" value={format} onChange={(event) => setFormat(event.target.value as EvaluationExportFormat)}><option value="csv">CSV 结果表</option><option value="xlsx">XLSX 结果表</option><option value="report">HTML 研究报告</option></select></label>
        <label>语言<select aria-label="导出语言" value={locale} onChange={(event) => setLocale(event.target.value as 'zh-CN' | 'en-US')}><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></label>
        <div className="evaluation-export-columns" aria-label="导出字段">{outputColumns.map((column) => <label key={column}><input type="checkbox" checked={columns.includes(column)} onChange={(event) => setColumns(event.target.checked ? [...columns, column] : columns.filter((item) => item !== column))} /> {labels.get(column) ?? column}</label>)}</div>
        <button className="app-button is-primary" type="button" disabled={busy || columns.length === 0} onClick={() => { void create(); }}><Download size={15} /> {busy ? '正在提交…' : '创建导出'}</button>
      </div>
      <div className="evaluation-export-history">
        {items.map((item) => {
          const active = ['queued', 'running', 'cancel_requested'].includes(item.status);
          const retryable = ['failed', 'cancelled', 'expired'].includes(item.status);
          return <article key={item.exportId} className={`evaluation-export-item is-${item.status}`}><div><strong>{item.format.toUpperCase()} · {EXPORT_STATUS_LABELS[item.status]}</strong><span>{item.progress.completed}/{item.progress.total} · 尝试 {item.attemptCount}</span>{item.error && <span className="is-error">{item.error.message}（{item.error.code}）</span>}{item.artifact && <><span>{item.artifact.filename} · {item.artifact.byteSize} bytes</span><code>SHA-256 {item.artifact.sha256}</code></>}</div><div>{item.status === 'succeeded' && <a className="app-button is-primary" href={evaluationExportDownloadUrl(item.exportId)}><Download size={14} /> 下载</a>}{active && item.status !== 'cancel_requested' && <button className="app-icon-button" type="button" aria-label="取消评价导出" disabled={busy} onClick={() => { void cancel(item.exportId); }}><X size={14} /></button>}{retryable && <button className="app-icon-button" type="button" aria-label="重新创建评价导出" disabled={busy} onClick={() => { void retry(item.exportId); }}><RotateCcw size={14} /></button>}</div></article>;
        })}
        {items.length === 0 && <div className="experiment-empty">尚未创建评价导出。</div>}
      </div>
    </section>
  );
}

function EvaluationResult({
  detail,
  catalogs,
  symbol,
  timeframe,
  scenarioNames,
  onOpenRun,
}: {
  detail: EvaluationRunDetail;
  catalogs: EvaluationCatalogs;
  symbol: string;
  timeframe: string;
  scenarioNames?: ScenarioName[];
  onOpenRun?: Props['onOpenRun'];
}) {
  const labels = new Map([
    ...catalogs.metricCatalog.metrics.map((item) => [item.metricId, item.displayName] as const),
    ...catalogs.dimensionCatalog.dimensions.map((item) => [item.dimensionId, item.displayName] as const),
    ...Object.entries(BASIC_AGGREGATE_LABELS),
    ...detail.spec.derivedMetrics.map((item) => [item.metricId, item.displayName] as const),
  ]);
  const dataTypes = new Map<string, EvaluationDataType>([
    ...catalogs.metricCatalog.metrics.map((item) => [item.metricId, item.dataType] as const),
    ...catalogs.dimensionCatalog.dimensions.map((item) => [
      item.dimensionId,
      item.dataType as EvaluationDataType,
    ] as const),
    ...detail.spec.derivedMetrics.map((item) => [item.metricId, 'decimal' as const] as const),
    ...detail.spec.aggregates.map((item) => [
      item.aggregateId,
      item.function === 'count' ? 'integer' as const : 'decimal' as const,
    ] as const),
  ]);
  if (detail.status !== 'succeeded' || !detail.output) return null;
  const completeness = detail.output.completeness ?? detail.completeness;
  const isPartial = detail.output.isPartial ?? completeness.isPartial;
  const visualizations = detail.output.visualizations ?? [];
  // 自定义参数系列可能使用不同分组口径，保留为折叠明细，不能因指标相同而丢弃。
  const additionalVisualizations = visualizations.filter((item) => item.type !== 'time_stability_v1');
  const firstRow = detail.output.rows[0];
  const candidateColumns = (detail.output.groupBy ?? [])
    .filter((id) => firstRow?.values[id] !== null && firstRow?.values[id] !== undefined);
  const candidateSummaryColumns = candidateColumns.length > 0
    ? candidateColumns
    : detail.output.columns.filter((id) => (
      firstRow?.values[id] !== null && firstRow?.values[id] !== undefined
    )).slice(0, 3);
  const rankLabel = detail.output.sort.length > 0 ? '排名' : '序号';
  return (
    <div className="evaluation-result">
      <div className="evaluation-report-identity">
        <div>
          <span>评价报告</span>
          <strong>{detail.specName ?? detail.spec.name} · 规则 v{detail.specVersionNo ?? '—'}</strong>
          <small>创建 {formatDateTime(detail.createdAt)} · 完成 {formatDateTime(detail.finishedAt)}</small>
        </div>
        <div>
          <span>{detail.output.sort.length > 0 ? '排序首行' : '首行记录'}</span>
          <strong>{firstRow ? candidateSummaryColumns.map((id) => (
            `${labels.get(id) ?? id}=${formatEvaluationValue(firstRow.values[id], id, dataTypes)}`
          )).join(' · ') : '没有符合规则的候选'}</strong>
        </div>
      </div>
      <div className={`evaluation-completeness ${isPartial ? 'is-partial' : ''}`}>
        <strong>{isPartial ? '临时评价' : '完整评价'}</strong>
        <span>完成率 {formatDecimal(completeness.completionRate)}</span>
        <span>成功 {completeness.succeededCount}</span>
        <span>失败 {completeness.failedCount}</span>
        <span>取消 {completeness.cancelledCount}</span>
        <span>缺失 {completeness.missingCount}</span>
      </div>
      <details className="evaluation-evidence">
        <summary>技术标识与冻结摘要</summary>
        <div>
          <span>规则摘要</span>
          <code>{detail.specHash ?? detail.evaluationSpecId}</code>
          <span>执行集合 {detail.executionCount} 项</span>
          <code>{detail.resultSetHash}</code>
          <span>输出摘要</span>
          <code>{detail.outputHash}</code>
        </div>
      </details>
      <div className="evaluation-table-wrap">
        <table className="evaluation-table">
          <thead><tr><th>{rankLabel}</th>{detail.output.columns.map((id) => <th key={id}>{labels.get(id) ?? id}</th>)}<th>成员结果</th></tr></thead>
          <tbody>{detail.output.rows.map((row, index) => (
            <tr key={row.trialExecutionId ?? row.groupKey ?? `${row.stageIndex}-${row.trialIndex}`}>
              <td>{index + 1}</td>
              {detail.output!.columns.map((id) => (
                <td key={id} title={row.unsupportedReasons[id] ?? ''}>{formatEvaluationValue(row.values[id], id, dataTypes)}</td>
              ))}
              <td>{row.runId ? <button className="app-button is-quiet" type="button" onClick={() => onOpenRun?.(row.runId!, symbol, timeframe, row.startDate ?? '')}>查看结果</button> : (
                <details className="evaluation-member-results">
                  <summary>{row.memberCount ?? 0} 项聚合</summary>
                  {(row.members ?? []).map((member) => (
                    <button
                      key={member.trialExecutionId}
                      type="button"
                      onClick={() => onOpenRun?.(member.runId, symbol, timeframe, member.startDate ?? '')}
                    >
                      Trial #{member.trialIndex + 1} · 查看单次结果
                    </button>
                  ))}
                  {!row.members?.length && <span>历史报告未保存成员明细。</span>}
                </details>
              )}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <EvaluationComparison key={detail.evaluationRunId} visualizations={visualizations} catalogs={catalogs} labels={labels} names={scenarioNames}
        onOpenRun={onOpenRun ? (runId, date) => onOpenRun(runId, symbol, timeframe, date) : undefined} />
      {additionalVisualizations.length > 0 && <details className="evaluation-additional-analysis"><summary>其他已保存分析（{additionalVisualizations.length}）</summary>
        <div className="evaluation-analysis-grid">{additionalVisualizations.map((item, index) => <VisualizationResult item={item} labels={labels} dataTypes={dataTypes} key={`${String(item.type)}-${index}`} />)}</div>
      </details>}
      {completeness.dimensionCoverage.length > 0 && <details className="evaluation-exceptions"><summary>关键维度覆盖（{completeness.dimensionCoverage.length}）</summary>{completeness.dimensionCoverage.map((dimension) => <div key={dimension.dimensionId}><strong>{labels.get(dimension.dimensionId) ?? dimension.dimensionId}</strong><span>最低观察值 {formatDecimal(dimension.minimumObservedRate)} / 门槛 {formatDecimal(dimension.requiredRate)}</span>{dimension.values.map((item) => <span key={JSON.stringify(item.value)}>{String(item.value ?? '空值')}：{item.succeededCount}/{item.expectedCount}（{formatDecimal(item.coverageRate)}）</span>)}</div>)}</details>}
      {completeness.exceptions.length > 0 && <details className="evaluation-exceptions"><summary>失败、取消与缺失项（{completeness.exceptions.length}）</summary>{completeness.exceptions.map((item) => <div key={`${item.stageId}-${item.trialIndex}`}><span>Trial #{item.trialIndex + 1}</span><strong>{statusLabel(item.status)}</strong><span>{item.error ? JSON.stringify(item.error) : '无结果'}</span></div>)}</details>}
      <details className="evaluation-execution-set">
        <summary>查看冻结执行集合</summary>
        {detail.executionSet.map((item) => (
          <div key={item.trialExecutionId}>
            <span>Trial #{item.trialIndex + 1}</span>
            <code>{item.trialExecutionId}</code>
            <code>{item.resultHash}</code>
          </div>
        ))}
      </details>
      <EvaluationExports detail={detail} labels={labels} />
    </div>
  );
}

export default function EvaluationBuilder({
  experimentId,
  symbol,
  timeframe,
  scenarioNames,
  trialSummary,
  onRetryFailed,
  onOpenRun,
}: Props) {
  const [catalogs, setCatalogs] = useState<EvaluationCatalogs | null>(null);
  const [specs, setSpecs] = useState<SavedEvaluationSpec[]>([]);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<EvaluationRunDetail | null>(null);
  const [name, setName] = useState('基础评价');
  const [columns, setColumns] = useState<string[]>(DEFAULT_METRICS);
  const [filters, setFilters] = useState<EvaluationFilter[]>([]);
  const [sort, setSort] = useState<EvaluationSort[]>([{
    type: 'metric_sort_v1', metricId: 'annualSharpe', direction: 'desc',
  }]);
  const [allowPartial, setAllowPartial] = useState(false);
  const [minimumCompletionRate, setMinimumCompletionRate] = useState('0.8000');
  const [requiredDimensionCoverage, setRequiredDimensionCoverage] = useState<EvaluationSpecPayload['completeness']['requiredDimensionCoverage']>([]);
  const [derivedMetrics, setDerivedMetrics] = useState<EvaluationFormula[]>([]);
  const [groupBy, setGroupBy] = useState<string[]>([]);
  const [aggregates, setAggregates] = useState<EvaluationAggregate[]>([]);
  const [groupFilters, setGroupFilters] = useState<EvaluationSpecPayload['groupFilters']>([]);
  const [visualizations, setVisualizations] = useState<EvaluationVisualization[]>([]);
  const [normalizerVersion, setNormalizerVersion] = useState<EvaluationSpecPayload['catalogs']['normalizerVersion']>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [temporaryMode, setTemporaryMode] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    const [catalogResult, specResult, runResult] = await Promise.all([
      fetchEvaluationCatalogs(experimentId),
      fetchEvaluationSpecs(experimentId),
      fetchEvaluationRuns(experimentId),
    ]);
    setCatalogs(catalogResult);
    setSpecs(specResult.items);
    setRuns(runResult.items);
    return runResult.items;
  }, [experimentId]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const items = await refresh();
        if (stopped) return;
        setError('');
        if (items.some((item) => item.status === 'queued' || item.status === 'running')) {
          timer = setTimeout(() => { void load(); }, 1500);
        }
      } catch (reason) {
        if (!stopped) setError(messageOf(reason));
      }
    };
    void load();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [refresh]);

  useEffect(() => {
    if (!selectedRun || !['queued', 'running'].includes(selectedRun.status)) return;
    let stopped = false;
    const timer = setTimeout(async () => {
      try {
        const detail = await fetchEvaluationRun(experimentId, selectedRun.evaluationRunId);
        if (!stopped) {
          setSelectedRun(detail);
          await refresh();
        }
      } catch (reason) {
        if (!stopped) setError(messageOf(reason));
      }
    }, 1200);
    return () => { stopped = true; clearTimeout(timer); };
  }, [experimentId, refresh, selectedRun]);

  const selectableFields = useMemo(() => {
    if (!catalogs) return [];
    const fields = new Map<string, EvaluationMetricDefinition | EvaluationDimensionDefinition | MetricOption>();
    catalogs.dimensionCatalog.dimensions.forEach((item) => fields.set(item.dimensionId, item));
    catalogs.metricCatalog.metrics.forEach((item) => fields.set(item.metricId, item));
    derivedMetrics.forEach((item) => fields.set(item.metricId, {
      metricId: item.metricId, displayName: item.displayName, unit: item.unit,
    }));
    aggregates.forEach((item) => fields.set(item.aggregateId, {
      metricId: item.aggregateId, displayName: item.aggregateId, unit: null,
    }));
    return [...fields.values()];
  }, [aggregates, catalogs, derivedMetrics]);
  const rowMetrics = useMemo<MetricOption[]>(() => [
    ...(catalogs?.metricCatalog.metrics ?? []),
    ...derivedMetrics.filter((item) => item.type === 'row_formula_v1').map((item) => ({
      metricId: item.metricId, displayName: item.displayName, unit: item.unit,
    })),
  ], [catalogs, derivedMetrics]);
  const outputMetrics = useMemo<MetricOption[]>(() => {
    if (groupBy.length === 0 && aggregates.length === 0 && !derivedMetrics.some((item) => item.type === 'group_formula_v1')) return rowMetrics;
    return [
      ...aggregates.map((item) => ({ metricId: item.aggregateId, displayName: item.aggregateId, unit: null })),
      ...derivedMetrics.filter((item) => item.type === 'group_formula_v1').map((item) => ({
        metricId: item.metricId, displayName: item.displayName, unit: item.unit,
      })),
    ];
  }, [aggregates, derivedMetrics, groupBy.length, rowMetrics]);
  const groupedOutput = groupBy.length > 0 || aggregates.length > 0 || derivedMetrics.some((item) => item.type === 'group_formula_v1');
  const coverageDimensions = useMemo(() => (catalogs?.dimensionCatalog.dimensions ?? []).filter((dimension) => (
    dimension.dimensionId.startsWith('parameter.')
    || ['dateScenarioId', 'dateRole', 'dateStart', 'dateEnd', 'initialCapital', 'costScenarioId', 'slippage', 'executionScenarioId'].includes(dimension.dimensionId)
  )), [catalogs]);
  const formulaErrors = useMemo(() => derivedMetrics.map((formula, index) => {
    const allowed = formula.type === 'row_formula_v1'
      ? [
        ...(catalogs?.metricCatalog.metrics ?? [])
          .filter((item) => item.dataType !== 'string')
          .map((item) => item.metricId),
        ...derivedMetrics.slice(0, index)
          .filter((item) => item.type === 'row_formula_v1')
          .map((item) => item.metricId),
      ]
      : aggregates.map((item) => item.aggregateId);
    return validateFormulaDraft(formula, allowed);
  }), [aggregates, catalogs, derivedMetrics]);
  const advancedError = useMemo(() => {
    const formulaError = formulaErrors.find(Boolean);
    if (formulaError) return formulaError;
    if (groupBy.length > 0 && aggregates.length === 0) {
      return '选择分组维度后至少添加一个聚合指标，否则只能得到分组顺序，不能形成有效排名。';
    }
    if (groupedOutput && sort.length === 0) {
      return '分组评价至少需要一个排序指标；未排序结果只能称为序号，不能称为排名。';
    }
    return '';
  }, [aggregates.length, formulaErrors, groupBy.length, groupedOutput, sort.length]);

  const planned = trialSummary?.planned ?? 0;
  const succeeded = trialSummary?.succeeded ?? planned;
  const failed = trialSummary?.failed ?? 0;
  const cancelled = trialSummary?.cancelled ?? 0;
  const missing = trialSummary ? Math.max(0, planned - succeeded - failed - cancelled) : 0;
  const hasIncompleteTrials = Boolean(trialSummary) && (
    failed > 0 || cancelled > 0 || missing > 0 || succeeded < planned
  );
  const observedCompletionRate = planned > 0
    ? (succeeded / planned).toFixed(4)
    : '0.0000';

  useEffect(() => {
    if (!groupedOutput) setGroupFilters([]);
  }, [groupedOutput]);

  useEffect(() => {
    const allowedSort = new Set(outputMetrics.map((item) => item.metricId));
    setSort((current) => current.filter((item) => allowedSort.has(item.metricId)));
    setGroupFilters((current) => current.filter((item) => allowedSort.has(item.metricId)));
    if (!groupedOutput) return;
    const allowedColumns = new Set([
      ...groupBy,
      ...aggregates.map((item) => item.aggregateId),
      ...derivedMetrics.filter((item) => item.type === 'group_formula_v1').map((item) => item.metricId),
    ]);
    setColumns((current) => {
      const next = current.filter((item) => allowedColumns.has(item));
      if (next.length > 0) return next;
      return [...groupBy, ...aggregates.slice(0, 1).map((item) => item.aggregateId)].slice(0, 2);
    });
  }, [aggregates, derivedMetrics, groupBy, groupedOutput, outputMetrics]);

  const saveSpecAndRun = async (spec: EvaluationSpecPayload) => {
    if (!catalogs || busy) return;
    setBusy(true); setError('');
    setNotice('');
    try {
      const saved = await saveEvaluationSpec(
        experimentId,
        spec,
        createBrowserUuid(),
      );
      const run = await createEvaluationRun(experimentId, saved.evaluationSpecId, createBrowserUuid());
      await refresh();
      setSelectedRun(await fetchEvaluationRun(experimentId, run.evaluationRunId));
      setNotice(run.replayed
        ? '相同规则、结果集合和流水线的报告已经存在，已直接打开原报告。'
        : '评价报告已创建；后台会继续完成计算。');
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  const generateBasic = async (partial: boolean) => {
    if (!catalogs) return;
    const defaults = buildBasicEvaluationDefaults(catalogs);
    await saveSpecAndRun(buildSpec(
      catalogs,
      '基础评价',
      defaults.columns,
      [],
      defaults.sort,
      partial,
      partial ? minimumCompletionRate : '1.0000',
      partial ? requiredDimensionCoverage : [],
      [],
      defaults.groupBy,
      defaults.aggregates,
      [],
      defaults.visualizations,
      'candidate_rate_distribution_v1',
    ));
  };

  const saveAdvanced = async () => {
    if (!catalogs || advancedError || !name.trim() || columns.length === 0) return;
    await saveSpecAndRun(buildSpec(
      catalogs, name, columns, filters, sort, allowPartial,
      minimumCompletionRate, requiredDimensionCoverage,
      derivedMetrics, groupBy, aggregates,
      groupFilters, visualizations,
      normalizerVersion,
    ));
  };

  const runSaved = async (specId: string) => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const run = await createEvaluationRun(
        experimentId, specId, createBrowserUuid(), true,
      );
      await refresh();
      setSelectedRun(await fetchEvaluationRun(experimentId, run.evaluationRunId));
    } catch (reason) { setError(messageOf(reason)); } finally { setBusy(false); }
  };

  const loadRun = async (runId: string) => {
    try { setSelectedRun(await fetchEvaluationRun(experimentId, runId)); }
    catch (reason) { setError(messageOf(reason)); }
  };

  const retryRun = async (runId: string) => {
    setBusy(true); setError('');
    try {
      await retryEvaluationRun(experimentId, runId, createBrowserUuid());
      await refresh();
      setSelectedRun(await fetchEvaluationRun(experimentId, runId));
    }
    catch (reason) { setError(messageOf(reason)); } finally { setBusy(false); }
  };

  if (!catalogs) return <div className="experiment-empty"><RefreshCw className="is-spinning" size={16} /> {error || '正在读取评价目录…'}</div>;
  return (
    <section className="experiment-preview-card evaluation-builder">
      <div className="evaluation-heading"><h2>结果评价</h2></div>
      {hasIncompleteTrials && (
        <section className="evaluation-failure-panel" aria-label="失败任务处理">
          <div className="evaluation-failure-summary">
            <div><span>成功</span><strong>{succeeded}</strong></div>
            <div><span>失败</span><strong>{failed}</strong></div>
            <div><span>取消</span><strong>{cancelled}</strong></div>
            <div><span>未完成</span><strong>{missing}</strong></div>
          </div>
          <div className="evaluation-failure-actions">
            <button className="app-button is-quiet" type="button" disabled={failed === 0 || !onRetryFailed} onClick={onRetryFailed}>
              <RotateCcw size={14} /> 重试失败项
            </button>
            <button
              className="app-button is-primary"
              type="button"
              disabled={succeeded === 0}
              onClick={() => {
                setTemporaryMode((current) => !current);
                setAllowPartial(true);
                setMinimumCompletionRate(observedCompletionRate);
              }}
            >
              使用成功结果生成临时评价
            </button>
          </div>
          {temporaryMode && (
            <div className="evaluation-temporary-policy">
              <p>临时评价会明确标记为“临时”，不能作为最终候选或最终测试结论。</p>
              <label>最低完成率<input aria-label="最低完成率" value={minimumCompletionRate} onChange={(event) => setMinimumCompletionRate(event.target.value)} /></label>
              <CoverageEditor coverage={requiredDimensionCoverage} dimensions={coverageDimensions} onChange={setRequiredDimensionCoverage} />
              <button className="app-button is-primary" type="button" disabled={busy} onClick={() => { void generateBasic(true); }}>
                {busy ? '正在提交…' : '确认生成临时评价'}
              </button>
            </div>
          )}
        </section>
      )}
      {!hasIncompleteTrials && (
        <section className="evaluation-basic-card">
          <div>
            <span className="evaluation-basic-kicker">系统预设 / 候选排名</span>
            <h3>一键生成基础评价</h3>
          </div>
          <div className="evaluation-basic-rules">
            <span>候选：全部策略参数</span>
            <span>排序：夏普中位数 ↓</span>
            <span>风控：最差回撤 ↑</span>
            <span>稳定：夏普离散度 ↑</span>
          </div>
          <button className="app-button is-primary" type="button" disabled={busy} onClick={() => { void generateBasic(false); }}>
            <Play size={15} /> {busy ? '正在提交…' : '生成基础评价'}
          </button>
        </section>
      )}
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      {notice && <div className="experiment-alert" role="status">{notice}</div>}
      <section className="evaluation-report-browser" aria-label="评价报告选择">
        <div>
          <span>评价报告</span>
          <strong>{runs.length > 0 ? `${runs.length} 份已保存报告` : '尚未生成报告'}</strong>
        </div>
        {runs.length > 0 && <label><span>打开报告</span><select aria-label="选择评价报告" value={selectedRun?.evaluationRunId ?? ''} onChange={(event) => { if (event.target.value) void loadRun(event.target.value); else setSelectedRun(null); }}><option value="">请选择一份报告</option>{runs.map((run, index) => <option value={run.evaluationRunId} key={run.evaluationRunId}>报告 #{runs.length - index} · {run.specName ?? '评价'} v{run.specVersionNo ?? '—'} · {statusLabel(run.status)} · {formatDateTime(run.finishedAt)}</option>)}</select></label>}
        {selectedRun?.status === 'failed' && <button className="app-button is-quiet" type="button" disabled={busy} onClick={() => { void retryRun(selectedRun.evaluationRunId); }}><RotateCcw size={14} /> 重试当前报告</button>}
      </section>
      {selectedRun?.error && <div className="experiment-alert is-error" role="alert">{selectedRun.error.message}（{selectedRun.error.code}）</div>}
      {selectedRun && <EvaluationResult detail={selectedRun} catalogs={catalogs} symbol={symbol} timeframe={timeframe} scenarioNames={scenarioNames} onOpenRun={onOpenRun} />}
      <div className={`evaluation-layout${advancedOpen ? ' is-advanced-open' : ''}`}>
        <details className="evaluation-advanced" open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}>
          <summary><span><strong>自定义评价（高级）</strong></span></summary>
          <div className="evaluation-form">
            <label>评价名称<input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} /></label>
            <fieldset><legend>展示字段</legend><div className="evaluation-columns">{selectableFields.map((field) => {
              const id = 'metricId' in field ? field.metricId : field.dimensionId;
              return <label key={id}><input type="checkbox" checked={columns.includes(id)} onChange={(event) => setColumns(event.target.checked ? [...columns, id] : columns.filter((item) => item !== id))} /> {field.displayName}</label>;
            })}</div></fieldset>
            <fieldset><legend>安全派生公式</legend><FormulaEditor formulas={derivedMetrics} baseMetrics={catalogs.metricCatalog.metrics} aggregates={aggregates} onChange={setDerivedMetrics} /></fieldset>
            <fieldset><legend>过滤条件</legend><FilterEditor filters={filters} metrics={rowMetrics} onChange={setFilters} /></fieldset>
            <fieldset><legend>分组维度</legend><div className="evaluation-columns">{catalogs.dimensionCatalog.dimensions.map((dimension) => <label key={dimension.dimensionId}><input type="checkbox" checked={groupBy.includes(dimension.dimensionId)} onChange={(event) => setGroupBy(event.target.checked ? [...groupBy, dimension.dimensionId] : groupBy.filter((item) => item !== dimension.dimensionId))} /> {dimension.displayName}</label>)}</div></fieldset>
            <fieldset><legend>聚合统计</legend><AggregateEditor aggregates={aggregates} metrics={rowMetrics} onChange={setAggregates} /></fieldset>
            {groupedOutput && <fieldset><legend>分组过滤</legend><FilterEditor filters={groupFilters.map((item) => ({ ...item, type: 'metric_comparison_v1' }))} metrics={outputMetrics} onChange={(items) => setGroupFilters(items.map((item) => ({ ...item, type: 'group_metric_comparison_v1' })))} /></fieldset>}
            <fieldset><legend>多级排序</legend><SortEditor sort={sort} metrics={outputMetrics} onChange={setSort} /></fieldset>
            <fieldset><legend>高级分析</legend><VisualizationEditor visualizations={visualizations} metrics={rowMetrics} dimensions={catalogs.dimensionCatalog.dimensions} onChange={setVisualizations} /></fieldset>
            {advancedError && <div className="experiment-alert is-error" role="alert">{advancedError}</div>}
            <button className="app-button is-primary" type="button" disabled={busy || Boolean(advancedError) || !name.trim() || columns.length === 0} onClick={() => { void saveAdvanced(); }}><Save size={15} /> {busy ? '正在提交…' : '保存自定义评价并运行'}</button>
          </div>
        </details>
        <details className="evaluation-spec-library">
          <summary><span><strong>自定义规则版本（{specs.length}）</strong></span></summary>
          <div className="evaluation-history">
            {specs.map((spec) => <div className="evaluation-history-item" key={spec.evaluationSpecId}><div><strong>v{spec.versionNo} · {spec.name}</strong><details className="evaluation-card-technical"><summary>规则标识</summary><code>{spec.specHash}</code></details></div><div><button className="app-icon-button" type="button" aria-label="载入为副本" onClick={() => { setAdvancedOpen(true); setName(`${spec.name} 副本`); setColumns(spec.spec.columns); setFilters(spec.spec.filters); setSort(spec.spec.sort); setAllowPartial(spec.spec.completeness.allowPartial); setTemporaryMode(spec.spec.completeness.allowPartial); setMinimumCompletionRate(spec.spec.completeness.minimumCompletionRate); setRequiredDimensionCoverage(spec.spec.completeness.requiredDimensionCoverage); setDerivedMetrics(spec.spec.derivedMetrics); setGroupBy(spec.spec.groupBy); setAggregates(spec.spec.aggregates); setGroupFilters(spec.spec.groupFilters); setVisualizations(spec.spec.visualizations); setNormalizerVersion(spec.spec.catalogs.normalizerVersion); }}><Copy size={14} /></button><button className="app-icon-button" type="button" aria-label="重新计算评价版本" title="显式重新计算，不复用已有报告" disabled={busy} onClick={() => { void runSaved(spec.evaluationSpecId); }}><RefreshCw size={14} /></button></div></div>)}
            {specs.length === 0 && <div className="experiment-empty">尚未保存评价版本。</div>}
          </div>
        </details>
      </div>
    </section>
  );
}
