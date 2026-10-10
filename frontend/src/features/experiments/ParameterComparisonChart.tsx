import { cellsFor, decimal, numeric, scenarioKey, scenarioName } from './comparisonData';
import type { ComparisonCandidate, ScenarioName, ScenarioValue } from './comparisonData';
import { dateRoleLabel } from './displayLabels';

type Props = {
  candidates: ComparisonCandidate[];
  scenarios: ScenarioValue[];
  axis: string;
  axisLabel: string;
  metricLabel: string;
  names: ScenarioName[];
};
const COLORS = ['#167d8d', '#b16335', '#7360a8', '#597e37', '#b24678', '#5171ab'];
const LEFT = 100;
const RIGHT = 820;
const TOP = 35;
const BOTTOM = 275;

function chartPoint(candidate: ComparisonCandidate, scenario: ScenarioValue): number | null {
  const cells = cellsFor(candidate, scenario);
  // 多条结果不隐式平均；没有唯一观测时曲线断开。
  return cells.length === 1 ? numeric(cells[0].value) : null;
}

function segments(points: Array<string | null>): string[] {
  const result: string[] = [];
  let current: string[] = [];
  points.forEach((point) => {
    if (point === null) {
      if (current.length) result.push(current.join(' '));
      current = [];
    } else current.push(point);
  });
  if (current.length) result.push(current.join(' '));
  return result;
}

export default function ParameterComparisonChart(props: Props) {
  const { candidates, scenarios, axis, axisLabel, metricLabel, names } = props;
  const ordered = candidates.filter((item) => numeric(item.dimensions?.[axis]) !== null)
    .sort((a, b) => numeric(a.dimensions?.[axis])! - numeric(b.dimensions?.[axis])!);
  const xs = ordered.map((item) => numeric(item.dimensions?.[axis])!);
  const ys = ordered.flatMap((item) => scenarios.map((scenario) => chartPoint(item, scenario)))
    .filter((value): value is number => value !== null);
  if (!xs.length || !ys.length) return <p className="comparison-empty">当前条件没有可绘制的单次指标值。</p>;
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const padding = Math.max((yMax - yMin) * 0.1, Math.abs(yMin) * 0.02, 0.0001);
  const low = yMin - padding;
  const high = yMax + padding;
  const x = (value: number) => xMax === xMin ? (LEFT + RIGHT) / 2 : LEFT + (value - xMin) / (xMax - xMin) * (RIGHT - LEFT);
  const y = (value: number) => BOTTOM - (value - low) / (high - low) * (BOTTOM - TOP);
  return <div className="comparison-chart">
    <svg viewBox="0 0 850 335" role="img" aria-label={`${axisLabel} 与${metricLabel}参数曲线`}>
      <text x={LEFT} y={18}>{metricLabel}</text>
      {Array.from({ length: 5 }, (_, index) => {
        const tick = low + (high - low) * index / 4;
        return <g key={index}><line className="comparison-gridline" x1={LEFT} x2={RIGHT} y1={y(tick)} y2={y(tick)} />
          <text x={LEFT - 10} y={y(tick) + 4} textAnchor="end">{decimal(tick)}</text></g>;
      })}
      {[...new Set(xs)].filter((_, index, all) => index % Math.max(1, Math.ceil(all.length / 8)) === 0 || index === all.length - 1)
        .map((value) => <text key={value} x={x(value)} y={BOTTOM + 22} textAnchor="middle">{Number.isInteger(value) ? value : decimal(value)}</text>)}
      <text x={(LEFT + RIGHT) / 2} y={325} textAnchor="middle">{axisLabel}</text>
      {scenarios.map((scenario, index) => <g key={scenarioKey(scenario)} stroke={COLORS[index % COLORS.length]}>
        {segments(ordered.map((item) => {
          const value = chartPoint(item, scenario);
          return value === null ? null : `${x(numeric(item.dimensions?.[axis])!)},${y(value)}`;
        })).map((points, part) => <polyline key={part} points={points} fill="none" strokeWidth={2} strokeDasharray={index % 2 ? '6 4' : undefined} />)}
        {ordered.map((item) => {
          const value = chartPoint(item, scenario);
          if (value === null) return null;
          return <circle key={item.candidateKey} cx={x(numeric(item.dimensions?.[axis])!)} cy={y(value)} r={4}
            fill={COLORS[index % COLORS.length]}><title>{`${scenarioName(scenario, names)} · ${axisLabel}=${String(item.dimensions?.[axis])} · ${metricLabel} ${decimal(value)}`}</title></circle>;
        })}
      </g>)}
    </svg>
    <div className="comparison-series-legend">{scenarios.map((scenario, index) => <span key={scenarioKey(scenario)}>
      <i style={{ background: COLORS[index % COLORS.length] }} />{scenarioName(scenario, names)} · {dateRoleLabel(scenario.role)}
      <small>{scenario.startDate ?? '日期未记录'} ～ {scenario.endDate ?? '日期未记录'}</small>
    </span>)}</div>
    <p className="comparison-caption">横轴为实际参数取值，所有日期场景共用纵轴；连线只连接相邻实测点，缺失值处断开。精确数值及单次回测入口见比较矩阵。</p>
  </div>;
}
