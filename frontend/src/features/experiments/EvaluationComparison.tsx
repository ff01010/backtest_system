import { useState } from 'react';
import type { EvaluationCatalogs } from './contracts';
import {
  cellsFor, conditionKey, conditionsFor, curveAvailability, decimal, dimensionIds, dimensionValue,
  displayExtent, heatColor, orderedCandidates, readComparisons, scenarioKey, scenarioName,
} from './comparisonData';
import type { ComparisonCandidate, ComparisonMetric, ScenarioName, ScenarioValue } from './comparisonData';
import { scenariosFor } from './comparisonData';
import { dateRoleLabel, dimensionDisplayLabel } from './displayLabels';
import ParameterComparisonChart from './ParameterComparisonChart';
import './comparison.css';

type Props = {
  visualizations: Record<string, unknown>[];
  catalogs: EvaluationCatalogs;
  labels: Map<string, string>;
  names?: ScenarioName[];
  onOpenRun?: (runId: string, date: string) => void;
};
const DIMENSION_LABELS: Record<string, string> = {
  initialCapital: '初始资金', slippage: '滑点', costScenarioId: '成本场景', executionScenarioId: '执行场景',
};

function ValueCell({ values, extent, label, onOpenRun }: {
  values: ScenarioValue[];
  extent: [number, number] | null;
  label: string;
  onOpenRun?: Props['onOpenRun'];
}) {
  const value = values[0];
  const link = (item: ScenarioValue, index: number) => <button key={item.runId ?? index} type="button"
    disabled={!onOpenRun || !item.runId} aria-label={`${label} · ${decimal(item.value)} · 查看单次回测`}
    title={item.runId ? `原始值：${String(item.value ?? '无指标')}；点击查看单次回测` : '历史报告未记录单次回测入口'}
    onClick={() => item.runId && onOpenRun?.(item.runId, item.startDate ?? '')}>{decimal(item.value)}</button>;
  return <td className="comparison-value" style={{ background: values.length === 1 ? heatColor(value.value, extent) : undefined }}>
    {values.length === 0 ? <span title="本报告中没有对应结果">—</span>
      : values.length === 1 ? link(value, 0)
        : <details><summary>{values.length} 条结果</summary>{values.map(link)}</details>}
  </td>;
}

function Matrix({ candidates, scenarios, parameters, labelFor, format, extent, names, onOpenRun }: {
  candidates: ComparisonCandidate[];
  scenarios: ScenarioValue[];
  parameters: string[];
  labelFor: (id: string) => string;
  format: (id: string, value: unknown) => string;
  extent: [number, number] | null;
  names: ScenarioName[];
  onOpenRun?: Props['onOpenRun'];
}) {
  return <div className="comparison-table-scroll" tabIndex={0} aria-label="参数与日期比较矩阵滚动区域">
    <table className="evaluation-table comparison-table" aria-label="参数组合与日期场景比较矩阵">
      <thead><tr><th scope="col">候选</th>{parameters.map((id) => <th scope="col" key={id}>{labelFor(id)}</th>)}
        {scenarios.map((scenario) => <th scope="col" key={scenarioKey(scenario)}>
          <strong>{scenarioName(scenario, names)}</strong><span>{dateRoleLabel(scenario.role)}</span>
          <small>{scenario.startDate ?? '日期未记录'} ～ {scenario.endDate ?? '日期未记录'}</small>
        </th>)}
      </tr></thead>
      <tbody>{candidates.map((candidate, index) => <tr key={candidate.candidateKey}>
        <th scope="row">{index + 1}</th>{parameters.map((id) => <td key={id}>{format(id, candidate.dimensions?.[id])}</td>)}
        {scenarios.map((scenario) => <ValueCell key={scenarioKey(scenario)} values={cellsFor(candidate, scenario)}
          extent={extent} label={`候选 ${index + 1} · ${scenarioName(scenario, names)}`} onOpenRun={onOpenRun} />)}
      </tr>)}</tbody>
    </table>
  </div>;
}

function MetricComparison({ metric, catalogs, labels, names = [], onOpenRun }: Omit<Props, 'visualizations'> & { metric: ComparisonMetric }) {
  const [view, setView] = useState('matrix');
  const [selectedCondition, setSelectedCondition] = useState('');
  const [selectedAxis, setSelectedAxis] = useState('');
  const [selectedParameters, setSelectedParameters] = useState('');
  const definitions = new Map(catalogs.dimensionCatalog.dimensions.map((item) => [item.dimensionId, item]));
  const labelFor = (id: string) => dimensionDisplayLabel(
    id,
    labels.get(id) ?? DIMENSION_LABELS[id] ?? id.replace('parameter.strategyParameters.', ''),
  );
  const format = (id: string, value: unknown) => dimensionValue(value, definitions.get(id), names);
  const describe = (values: Record<string, unknown>) => Object.entries(values)
    .map(([id, value]) => `${labelFor(id)} ${format(id, value)}`).join(' · ');
  const knownIds = dimensionIds(metric.candidates);
  const catalogIds = [...definitions.keys()].filter((id) => knownIds.includes(id));
  const ids = [...catalogIds, ...knownIds.filter((id) => !definitions.has(id))];
  const runtimeIds = ids.filter((id) => !id.startsWith('parameter.'));
  const conditions = conditionsFor(metric.candidates, runtimeIds);
  const condition = conditions.find((item) => item.key === selectedCondition) ?? conditions[0];
  const parameterIds = ids.filter((id) => id.startsWith('parameter.'));
  const candidates = orderedCandidates(metric.candidates.filter((item) => conditionKey(item, runtimeIds) === condition?.key), parameterIds, definitions);
  const varying = parameterIds.filter((id) => conditionsFor(candidates, [id]).length > 1);
  const { axes, reason: curveReason } = curveAvailability(candidates, parameterIds, definitions);
  const axis = axes.includes(selectedAxis) ? selectedAxis : axes[0];
  const otherIds = parameterIds.filter((id) => id !== axis);
  const parameterConditions = conditionsFor(candidates, otherIds);
  const fixed = parameterConditions.find((item) => item.key === selectedParameters) ?? parameterConditions[0];
  const curveCandidates = candidates.filter((item) => conditionKey(item, otherIds) === fixed?.key);
  // 日期列取自整份报告，切换条件后仍能看到该条件缺少的场景。
  const scenarios = scenariosFor(metric.candidates);
  const extent = displayExtent(candidates);
  const metricLabel = labels.get(metric.metricId) ?? metric.metricId;
  const curveVisible = view === 'curve' && Boolean(axis);
  return <>
    <div className="comparison-controls">
      <div className="comparison-view-switch" role="group" aria-label="比较视图">
        <button type="button" aria-pressed={!curveVisible} onClick={() => setView('matrix')}>比较矩阵</button>
        <button type="button" aria-pressed={curveVisible} disabled={!axis}
          title={curveReason ?? undefined} onClick={() => setView('curve')}>参数曲线</button>
      </div>
      {conditions.length > 1 ? <label>比较条件<select aria-label="比较条件" value={condition?.key ?? ''}
        onChange={(event) => setSelectedCondition(event.target.value)}>{conditions.map((item) => <option key={item.key} value={item.key}>{describe(item.dimensions)}</option>)}</select></label>
        : runtimeIds.length > 0 && <span className="comparison-fixed">{describe(condition?.dimensions ?? {})}</span>}
    </div>
    {!curveVisible && <>
      <div className="comparison-legend"><span>{metricLabel} · 数值由低到高</span><i style={{ background: extent && extent[0] === extent[1] ? heatColor(extent[0], extent) : undefined }} />
        <span>{extent ? `${decimal(extent[0])} ～ ${decimal(extent[1])}` : '无有效数值'}</span></div>
      <Matrix candidates={candidates} scenarios={scenarios} parameters={varying} labelFor={labelFor} format={format}
        extent={extent} names={names} onOpenRun={onOpenRun} />
    </>}
    {curveVisible && <>
      <div className="comparison-controls"><label>横轴参数<select aria-label="横轴参数" value={axis} onChange={(event) => {
        setSelectedAxis(event.target.value); setSelectedParameters('');
      }}>{axes.map((id) => <option key={id} value={id}>{labelFor(id)}</option>)}</select></label>
        {parameterConditions.length > 1 ? <label>固定其他参数<select aria-label="固定其他参数" value={fixed?.key ?? ''}
          onChange={(event) => setSelectedParameters(event.target.value)}>{parameterConditions.map((item) => <option key={item.key} value={item.key}>{describe(item.dimensions)}</option>)}</select></label>
          : otherIds.length > 0 && <span className="comparison-fixed">固定参数：{describe(fixed?.dimensions ?? {})}</span>}
      </div>
      <ParameterComparisonChart candidates={curveCandidates} scenarios={scenarios} axis={axis} axisLabel={labelFor(axis)} metricLabel={metricLabel} names={names} />
    </>}
    {!metric.candidates.length && <p className="comparison-empty">当前报告没有可比较的候选结果。</p>}
  </>;
}

export default function EvaluationComparison(props: Props) {
  const metrics = readComparisons(props.visualizations);
  const [selectedMetric, setSelectedMetric] = useState('annualSharpe');
  const metric = metrics.find((item) => item.metricId === selectedMetric) ?? metrics[0];
  if (!metric) return null;
  return <section className="evaluation-comparison" aria-label="参数与日期对比">
    <header><div><span>结果对比</span><h3>参数组合 × 日期场景</h3></div>
      <label>查看指标<select aria-label="比较指标" value={metric.metricId} onChange={(event) => setSelectedMetric(event.target.value)}>
        {metrics.map((item) => <option key={item.metricId} value={item.metricId}>{props.labels.get(item.metricId) ?? item.metricId}</option>)}
      </select></label>
    </header>
    <MetricComparison key={metric.metricId} {...props} metric={metric} />
  </section>;
}
