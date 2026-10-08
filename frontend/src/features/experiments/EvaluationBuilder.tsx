import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Copy, Download, Play, Plus, RefreshCw, RotateCcw, Save, Trash2, X } from 'lucide-react';
import { createBrowserUuid } from '../../utils/browserUuid';
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

type Props = {
  experimentId: string;
  symbol: string;
  timeframe: string;
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
      normalizerVersion: null,
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
  onChange,
}: {
  formulas: EvaluationFormula[];
  onChange: (formulas: EvaluationFormula[]) => void;
}) {
  const update = (index: number, change: Partial<EvaluationFormula>) => {
    onChange(formulas.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...change } : item
    )));
  };
  return (
    <div className="evaluation-rules">
      {formulas.map((formula, index) => (
        <div className="evaluation-rule evaluation-rule-formula" key={`${formula.metricId}-${index}`}>
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
      ))}
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
          {item.type === 'time_stability_v1' && <span>按冻结日期顺序比较</span>}
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

function VisualizationResult({ item }: { item: Record<string, unknown> }) {
  if (item.type === 'parameter_curve_v1') {
    const series = item.series as Array<{ seriesKey: string; points: Array<Record<string, unknown>> }>;
    return <div className="evaluation-analysis-card"><h4>参数曲线 · {String(item.metricId)}</h4>{series.map((group) => <div key={group.seriesKey} className="evaluation-point-list">{group.points.map((point) => <span key={String(point.x)}><strong>{String(point.x)}</strong> 中位数 {String(point.median ?? '—')} · 样本 {String(point.count)}</span>)}</div>)}</div>;
  }
  if (item.type === 'parameter_heatmap_v1') {
    const facets = item.facets as Array<{ facetKey: string; cells: Array<Record<string, unknown>> }>;
    return <div className="evaluation-analysis-card"><h4>二维热力图 · {String(item.metricId)}</h4>{facets.map((facet) => <div key={facet.facetKey} className="evaluation-heatmap">{facet.cells.map((cell) => <span key={`${String(cell.x)}-${String(cell.y)}`} title={`样本 ${String(cell.count)}`}>{String(cell.x)} × {String(cell.y)}<strong>{String(cell.median ?? '—')}</strong></span>)}</div>)}</div>;
  }
  if (item.type === 'cost_stress_v1') {
    const comparisons = item.comparisons as Array<Record<string, unknown>>;
    return <div className="evaluation-analysis-card"><h4>成本压力 · {String(item.metricId)}</h4><p>仅比较独立运行的真实 Slippage Trial，不在展示层扣减收益。</p><div className="evaluation-point-list">{comparisons.map((entry, index) => <span key={`${String(entry.pressureRunId)}-${index}`}>{String(entry.baselineScenarioId)} → {String(entry.pressureScenarioId)}：{String(entry.delta ?? '—')}</span>)}</div></div>;
  }
  const candidates = item.candidates as Array<{ candidateKey: string; values: Array<Record<string, unknown>> }>;
  return <div className="evaluation-analysis-card"><h4>时间稳定性 · {String(item.metricId)}</h4><p>reference 仅是冻结用途标签，不代表最终 test。</p>{candidates.map((candidate) => <div className="evaluation-point-list" key={candidate.candidateKey}>{candidate.values.map((value) => <span key={String(value.dateScenarioId)}>{String(value.dateScenarioId)}（{String(value.role)}）：{String(value.value ?? '—')}</span>)}</div>)}</div>;
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
      <div className="evaluation-export-heading"><div><h3>异步导出</h3><p>制品冻结当前输出摘要；过期后会创建新的导出作业。</p></div><code>{detail.outputHash}</code></div>
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
  onOpenRun,
}: {
  detail: EvaluationRunDetail;
  catalogs: EvaluationCatalogs;
  symbol: string;
  timeframe: string;
  onOpenRun?: Props['onOpenRun'];
}) {
  const labels = new Map([
    ...catalogs.metricCatalog.metrics.map((item) => [item.metricId, item.displayName] as const),
    ...catalogs.dimensionCatalog.dimensions.map((item) => [item.dimensionId, item.displayName] as const),
  ]);
  if (detail.status !== 'succeeded' || !detail.output) return null;
  const completeness = detail.output.completeness ?? detail.completeness;
  const isPartial = detail.output.isPartial ?? completeness.isPartial;
  const visualizations = detail.output.visualizations ?? [];
  return (
    <div className="evaluation-result">
      <div className={`evaluation-completeness ${isPartial ? 'is-partial' : ''}`}>
        <strong>{isPartial ? '部分评价（provisional）' : '完整评价'}</strong>
        <span>完成率 {completeness.completionRate}</span>
        <span>成功 {completeness.succeededCount}</span>
        <span>失败 {completeness.failedCount}</span>
        <span>取消 {completeness.cancelledCount}</span>
        <span>缺失 {completeness.missingCount}</span>
      </div>
      <p className="evaluation-dataset-notice">{detail.output.datasetNotice ?? '历史基础评价按保存时的流水线与输出语义展示。'}</p>
      <div className="evaluation-evidence">
        <span>执行集合 {detail.executionCount} 项</span>
        <code>{detail.resultSetHash}</code>
        <span>输出摘要</span>
        <code>{detail.outputHash}</code>
      </div>
      <div className="evaluation-table-wrap">
        <table className="evaluation-table">
          <thead><tr><th>排名</th>{detail.output.columns.map((id) => <th key={id}>{labels.get(id) ?? id}</th>)}<th>单次回测</th></tr></thead>
          <tbody>{detail.output.rows.map((row, index) => (
            <tr key={row.trialExecutionId ?? row.groupKey ?? `${row.stageIndex}-${row.trialIndex}`}>
              <td>{index + 1}</td>
              {detail.output!.columns.map((id) => (
                <td key={id} title={row.unsupportedReasons[id] ?? ''}>{row.values[id] ?? '—'}</td>
              ))}
              <td>{row.runId ? <button className="app-button is-quiet" type="button" onClick={() => onOpenRun?.(row.runId!, symbol, timeframe, row.startDate ?? '')}>查看结果</button> : <span>{row.memberCount ?? '—'} 项聚合</span>}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {visualizations.length > 0 && <div className="evaluation-analysis-grid">{visualizations.map((item, index) => <VisualizationResult item={item} key={`${String(item.type)}-${index}`} />)}</div>}
      {completeness.dimensionCoverage.length > 0 && <details className="evaluation-exceptions"><summary>关键维度覆盖（{completeness.dimensionCoverage.length}）</summary>{completeness.dimensionCoverage.map((dimension) => <div key={dimension.dimensionId}><strong>{labels.get(dimension.dimensionId) ?? dimension.dimensionId}</strong><span>最低观察值 {dimension.minimumObservedRate} / 门槛 {dimension.requiredRate}</span>{dimension.values.map((item) => <span key={JSON.stringify(item.value)}>{String(item.value ?? '空值')}：{item.succeededCount}/{item.expectedCount}（{item.coverageRate}）</span>)}</div>)}</details>}
      {completeness.exceptions.length > 0 && <details className="evaluation-exceptions"><summary>失败、取消与缺失项（{completeness.exceptions.length}）</summary>{completeness.exceptions.map((item) => <div key={`${item.stageId}-${item.trialIndex}`}><span>Trial #{item.trialIndex + 1}</span><strong>{item.status}</strong><span>{item.error ? JSON.stringify(item.error) : '无结果'}</span></div>)}</details>}
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

export default function EvaluationBuilder({ experimentId, symbol, timeframe, onOpenRun }: Props) {
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

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

  const saveAndRun = async () => {
    if (!catalogs || !name.trim() || columns.length === 0 || busy) return;
    setBusy(true); setError('');
    try {
      const saved = await saveEvaluationSpec(
        experimentId,
        buildSpec(
          catalogs, name, columns, filters, sort, allowPartial,
          minimumCompletionRate, requiredDimensionCoverage,
          derivedMetrics, groupBy, aggregates,
          groupFilters, visualizations,
        ),
        createBrowserUuid(),
      );
      const run = await createEvaluationRun(experimentId, saved.evaluationSpecId, createBrowserUuid());
      await refresh();
      setSelectedRun(await fetchEvaluationRun(experimentId, run.evaluationRunId));
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  const runSaved = async (specId: string) => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const run = await createEvaluationRun(experimentId, specId, createBrowserUuid());
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
      <div className="evaluation-heading"><div><h2>结果评价</h2><p>评价只读取冻结结果，不会重新运行回测或修改 Trial。</p></div><code>{catalogs.pipelineVersion}</code></div>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      <div className="evaluation-layout">
        <div className="evaluation-form">
          <label>评价名称<input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} /></label>
          <fieldset><legend>展示字段</legend><div className="evaluation-columns">{selectableFields.map((field) => {
            const id = 'metricId' in field ? field.metricId : field.dimensionId;
            return <label key={id}><input type="checkbox" checked={columns.includes(id)} onChange={(event) => setColumns(event.target.checked ? [...columns, id] : columns.filter((item) => item !== id))} /> {field.displayName}</label>;
          })}</div></fieldset>
          <fieldset><legend>完成性门禁</legend><label className="evaluation-toggle"><input type="checkbox" checked={allowPartial} onChange={(event) => setAllowPartial(event.target.checked)} /> 允许 provisional 部分评价</label>{allowPartial && <><label>最低完成率<input aria-label="最低完成率" value={minimumCompletionRate} onChange={(event) => setMinimumCompletionRate(event.target.value)} /></label><CoverageEditor coverage={requiredDimensionCoverage} dimensions={coverageDimensions} onChange={setRequiredDimensionCoverage} /></>}</fieldset>
          <fieldset><legend>安全派生公式</legend><FormulaEditor formulas={derivedMetrics} onChange={setDerivedMetrics} /></fieldset>
          <fieldset><legend>过滤条件</legend><FilterEditor filters={filters} metrics={rowMetrics} onChange={setFilters} /></fieldset>
          <fieldset><legend>分组维度</legend><div className="evaluation-columns">{catalogs.dimensionCatalog.dimensions.map((dimension) => <label key={dimension.dimensionId}><input type="checkbox" checked={groupBy.includes(dimension.dimensionId)} onChange={(event) => setGroupBy(event.target.checked ? [...groupBy, dimension.dimensionId] : groupBy.filter((item) => item !== dimension.dimensionId))} /> {dimension.displayName}</label>)}</div></fieldset>
          <fieldset><legend>聚合统计</legend><AggregateEditor aggregates={aggregates} metrics={rowMetrics} onChange={setAggregates} /></fieldset>
          {groupedOutput && <fieldset><legend>分组过滤</legend><FilterEditor filters={groupFilters.map((item) => ({ ...item, type: 'metric_comparison_v1' }))} metrics={outputMetrics} onChange={(items) => setGroupFilters(items.map((item) => ({ ...item, type: 'group_metric_comparison_v1' })))} /></fieldset>}
          <fieldset><legend>多级排序</legend><SortEditor sort={sort} metrics={outputMetrics} onChange={setSort} /></fieldset>
          <fieldset><legend>高级分析</legend><VisualizationEditor visualizations={visualizations} metrics={rowMetrics} dimensions={catalogs.dimensionCatalog.dimensions} onChange={setVisualizations} /></fieldset>
          <button className="app-button is-primary" type="button" disabled={busy || !name.trim() || columns.length === 0} onClick={() => { void saveAndRun(); }}><Save size={15} /> {busy ? '正在提交…' : '保存版本并运行'}</button>
        </div>
        <div className="evaluation-history">
          <h3>评价版本</h3>
          {specs.map((spec) => <div className="evaluation-history-item" key={spec.evaluationSpecId}><div><strong>v{spec.versionNo} · {spec.name}</strong><code>{spec.specHash.slice(0, 12)}…</code></div><div><button className="app-icon-button" type="button" aria-label="载入为副本" onClick={() => { setName(`${spec.name} 副本`); setColumns(spec.spec.columns); setFilters(spec.spec.filters); setSort(spec.spec.sort); setAllowPartial(spec.spec.completeness.allowPartial); setMinimumCompletionRate(spec.spec.completeness.minimumCompletionRate); setRequiredDimensionCoverage(spec.spec.completeness.requiredDimensionCoverage); setDerivedMetrics(spec.spec.derivedMetrics); setGroupBy(spec.spec.groupBy); setAggregates(spec.spec.aggregates); setGroupFilters(spec.spec.groupFilters); setVisualizations(spec.spec.visualizations); }}><Copy size={14} /></button><button className="app-icon-button" type="button" aria-label="运行评价版本" disabled={busy} onClick={() => { void runSaved(spec.evaluationSpecId); }}><Play size={14} /></button></div></div>)}
          {specs.length === 0 && <div className="experiment-empty">尚未保存评价版本。</div>}
          <h3>评价运行</h3>
          {runs.map((run) => <div className="evaluation-history-item" key={run.evaluationRunId}><button type="button" className="evaluation-run-link" onClick={() => { void loadRun(run.evaluationRunId); }}><strong>{run.specName ?? '评价'} · {run.status}{run.completeness.isPartial ? ' · provisional' : ''}</strong><span>{run.completeness.succeededCount}/{run.completeness.plannedTrialCount} 成功 · {run.resultSetHash.slice(0, 12)}…</span></button>{run.status === 'failed' && <button className="app-icon-button" type="button" aria-label="重试评价" disabled={busy} onClick={() => { void retryRun(run.evaluationRunId); }}><RotateCcw size={14} /></button>}</div>)}
          {runs.length === 0 && <div className="experiment-empty">尚未运行评价。</div>}
        </div>
      </div>
      {selectedRun?.error && <div className="experiment-alert is-error" role="alert">{selectedRun.error.message}（{selectedRun.error.code}）</div>}
      {selectedRun && <EvaluationResult detail={selectedRun} catalogs={catalogs} symbol={symbol} timeframe={timeframe} onOpenRun={onOpenRun} />}
    </section>
  );
}
