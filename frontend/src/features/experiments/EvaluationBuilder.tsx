import { useCallback, useEffect, useMemo, useState } from 'react';
import { Copy, Play, Plus, RefreshCw, RotateCcw, Save, Trash2 } from 'lucide-react';
import { createBrowserUuid } from '../../utils/browserUuid';
import {
  createEvaluationRun,
  fetchEvaluationCatalogs,
  fetchEvaluationRun,
  fetchEvaluationRuns,
  fetchEvaluationSpecs,
  retryEvaluationRun,
  saveEvaluationSpec,
} from './api';
import type {
  EvaluationCatalogs,
  EvaluationDimensionDefinition,
  EvaluationFilter,
  EvaluationMetricDefinition,
  EvaluationRun,
  EvaluationRunDetail,
  EvaluationSort,
  EvaluationSpecPayload,
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

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '评价操作失败';
}

function metricLabel(metric: EvaluationMetricDefinition): string {
  return metric.unit ? `${metric.displayName}（${metric.unit}）` : metric.displayName;
}

function buildSpec(
  catalogs: EvaluationCatalogs,
  name: string,
  columns: string[],
  filters: EvaluationFilter[],
  sort: EvaluationSort[],
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
      allowPartial: false,
      minimumCompletionRate: '1.0000',
      requiredDimensionCoverage: [],
    },
    columns,
    derivedMetrics: [],
    filters,
    groupBy: [],
    aggregates: [],
    groupFilters: [],
    sort,
    visualizations: [],
    reportSections: [],
  };
}

function FilterEditor({
  filters,
  metrics,
  onChange,
}: {
  filters: EvaluationFilter[];
  metrics: EvaluationMetricDefinition[];
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
      <button className="app-button is-quiet" type="button" onClick={() => onChange([...filters, {
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
  metrics: EvaluationMetricDefinition[];
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
      <button className="app-button is-quiet" type="button" onClick={() => onChange([...sort, {
        type: 'metric_sort_v1',
        metricId: metrics[0]?.metricId ?? 'totalReturn',
        direction: 'desc',
      }])}>
        <Plus size={14} /> 添加排序
      </button>
    </div>
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
  return (
    <div className="evaluation-result">
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
            <tr key={row.trialExecutionId}>
              <td>{index + 1}</td>
              {detail.output!.columns.map((id) => (
                <td key={id} title={row.unsupportedReasons[id] ?? ''}>{row.values[id] ?? '—'}</td>
              ))}
              <td><button className="app-button is-quiet" type="button" onClick={() => onOpenRun?.(row.runId, symbol, timeframe, row.startDate ?? '')}>查看结果</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
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
    const fields = new Map<string, EvaluationMetricDefinition | EvaluationDimensionDefinition>();
    catalogs.dimensionCatalog.dimensions.forEach((item) => fields.set(item.dimensionId, item));
    catalogs.metricCatalog.metrics.forEach((item) => fields.set(item.metricId, item));
    return [...fields.values()];
  }, [catalogs]);
  const metrics = catalogs?.metricCatalog.metrics ?? [];

  const saveAndRun = async () => {
    if (!catalogs || !name.trim() || columns.length === 0 || busy) return;
    setBusy(true); setError('');
    try {
      const saved = await saveEvaluationSpec(
        experimentId, buildSpec(catalogs, name, columns, filters, sort), createBrowserUuid(),
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
          <fieldset><legend>过滤条件</legend><FilterEditor filters={filters} metrics={metrics} onChange={setFilters} /></fieldset>
          <fieldset><legend>多级排序</legend><SortEditor sort={sort} metrics={metrics} onChange={setSort} /></fieldset>
          <button className="app-button is-primary" type="button" disabled={busy || !name.trim() || columns.length === 0} onClick={() => { void saveAndRun(); }}><Save size={15} /> {busy ? '正在提交…' : '保存版本并运行'}</button>
        </div>
        <div className="evaluation-history">
          <h3>评价版本</h3>
          {specs.map((spec) => <div className="evaluation-history-item" key={spec.evaluationSpecId}><div><strong>v{spec.versionNo} · {spec.name}</strong><code>{spec.specHash.slice(0, 12)}…</code></div><div><button className="app-icon-button" type="button" aria-label="载入为副本" onClick={() => { setName(`${spec.name} 副本`); setColumns(spec.spec.columns); setFilters(spec.spec.filters); setSort(spec.spec.sort); }}><Copy size={14} /></button><button className="app-icon-button" type="button" aria-label="运行评价版本" disabled={busy} onClick={() => { void runSaved(spec.evaluationSpecId); }}><Play size={14} /></button></div></div>)}
          {specs.length === 0 && <div className="experiment-empty">尚未保存评价版本。</div>}
          <h3>评价运行</h3>
          {runs.map((run) => <div className="evaluation-history-item" key={run.evaluationRunId}><button type="button" className="evaluation-run-link" onClick={() => { void loadRun(run.evaluationRunId); }}><strong>{run.specName ?? '评价'} · {run.status}</strong><span>{run.executionCount} 项 · {run.resultSetHash.slice(0, 12)}…</span></button>{run.status === 'failed' && <button className="app-icon-button" type="button" aria-label="重试评价" disabled={busy} onClick={() => { void retryRun(run.evaluationRunId); }}><RotateCcw size={14} /></button>}</div>)}
          {runs.length === 0 && <div className="experiment-empty">尚未运行评价。</div>}
        </div>
      </div>
      {selectedRun?.error && <div className="experiment-alert is-error" role="alert">{selectedRun.error.message}（{selectedRun.error.code}）</div>}
      {selectedRun && <EvaluationResult detail={selectedRun} catalogs={catalogs} symbol={symbol} timeframe={timeframe} onOpenRun={onOpenRun} />}
    </section>
  );
}
