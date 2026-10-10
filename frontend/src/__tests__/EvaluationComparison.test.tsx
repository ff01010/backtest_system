import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EvaluationComparison from '../features/experiments/EvaluationComparison';
import type { EvaluationCatalogs } from '../features/experiments/contracts';
import type { ComparisonCandidate } from '../features/experiments/comparisonData';

const WINDOW = 'parameter.strategyParameters.window';
const THRESHOLD = 'parameter.strategyParameters.threshold';
const NOTE = 'parameter.strategyParameters.note';
const ENABLED = 'parameter.strategyParameters.enabled';
const catalogs: EvaluationCatalogs = {
  experimentId: 'e', experimentRevisionId: 'r', stages: [],
  metricCatalog: { version: 'metric_catalog_v1', metrics: [] },
  dimensionCatalog: { version: 'dimension_catalog_v1', dimensions: [
    { dimensionId: WINDOW, displayName: 'window', dataType: 'integer', unit: null, nullSemantics: 'missing_is_null' },
    { dimensionId: THRESHOLD, displayName: 'threshold', dataType: 'decimal', unit: null, nullSemantics: 'missing_is_null' },
    { dimensionId: NOTE, displayName: 'note', dataType: 'string', unit: null, nullSemantics: 'missing_is_null' },
    { dimensionId: ENABLED, displayName: 'enabled', dataType: 'boolean', unit: null, nullSemantics: 'missing_is_null' },
    { dimensionId: 'initialCapital', displayName: '初始资金', dataType: 'decimal', unit: null, nullSemantics: 'missing_is_null' },
  ] },
  pipelineVersion: 'v2', adapter: { type: 'test', version: '1' },
};
const labels = new Map([
  [WINDOW, 'window'], [THRESHOLD, 'threshold'], ['initialCapital', '初始资金'],
  ['annualSharpe', '年化夏普'], ['totalReturn', '总收益率'],
]);
const names = [
  { scenarioId: 'dev', name: '开发区间', startDate: '2024-01-01', endDate: '2024-12-31' },
  { scenarioId: 'val', name: '验证区间', startDate: '2025-01-01', endDate: '2025-12-31' },
];
function candidate(window: number, threshold: number, capital = 100000): ComparisonCandidate {
  const key = `${window}-${threshold}-${capital}`;
  return {
    candidateKey: key, dimensions: { [WINDOW]: window, [THRESHOLD]: threshold, initialCapital: capital },
    values: names.map((scenario, index) => ({
      dateScenarioId: scenario.scenarioId, dateScenarioOrder: index,
      startDate: scenario.startDate, endDate: scenario.endDate,
      role: index ? 'validation' : 'development', value: index ? '-1.2369' : '-1.1300', runId: `${key}-${index}`,
    })),
  };
}
const candidates = [0.01, 0.02].flatMap((threshold) => [10, 50, 60].map((window) => candidate(window, threshold)));
function renderComparison(rows = candidates) {
  const onOpenRun = vi.fn();
  const metric = { type: 'time_stability_v1', metricId: 'annualSharpe', candidates: rows };
  const rendered = render(<EvaluationComparison catalogs={catalogs} labels={labels} names={names} onOpenRun={onOpenRun}
    visualizations={[metric, { ...metric, metricId: 'totalReturn', candidates: rows.map((row) => ({
      ...row, values: row.values.map((value) => ({ ...value, value: '0.123456' })),
    })) }]} />);
  return { ...rendered, onOpenRun };
}
afterEach(cleanup);

describe('EvaluationComparison', () => {
  it('shows six candidates in one matrix, common conditions once and opens the exact run', () => {
    const { onOpenRun, container } = renderComparison();
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(7);
    expect(within(table).getAllByText('-1.1300')).toHaveLength(6);
    expect(screen.getByText('开发区间')).not.toBeNull();
    expect(screen.getByText('2024-01-01 ～ 2024-12-31')).not.toBeNull();
    expect(screen.getAllByText('初始资金 100000.0000')).toHaveLength(1);
    expect(screen.queryByText(/当前结论|典型表现|最稳定|相对较优/)).toBeNull();
    const cellColors = Array.from(container.querySelectorAll('td.comparison-value')).map((cell) => cell.getAttribute('style'));
    expect(cellColors[0]).toBe(cellColors[2]);
    expect(cellColors[0]).not.toBe(cellColors[1]);
    fireEvent.click(screen.getByRole('button', { name: '候选 1 · 开发区间 · -1.1300 · 查看单次回测' }));
    expect(onOpenRun).toHaveBeenCalledWith('10-0.01-100000-0', '2024-01-01');
    fireEvent.change(screen.getByLabelText('比较指标'), { target: { value: 'totalReturn' } });
    expect(within(screen.getByRole('table')).getAllByText('0.1235')).toHaveLength(12);
  });

  it('plots one varying parameter with every other parameter and runtime condition fixed', () => {
    const changed = candidates.map((row) => ({ ...row, values: row.values.map((value) => ({
      ...value, value: row.dimensions?.[THRESHOLD] === 0.02 ? '9' : value.value,
    })) }));
    const { container } = renderComparison([...changed, candidate(10, 0.01, 200000)]);
    fireEvent.click(screen.getByRole('button', { name: '参数曲线' }));
    fireEvent.change(screen.getByLabelText('横轴参数'), { target: { value: WINDOW } });
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.getByRole('img', { name: 'window 与年化夏普参数曲线' })).not.toBeNull();
    expect(container.querySelectorAll('circle')).toHaveLength(6);
    expect(container.querySelector('svg')?.textContent).not.toContain('9.0000');
    const fixedSelect = screen.getByLabelText('固定其他参数') as HTMLSelectElement;
    fireEvent.change(fixedSelect, { target: { value: fixedSelect.options[1].value } });
    expect(container.querySelector('svg')?.textContent).toContain('9.0000');
    expect(container.querySelector('svg')?.textContent).not.toContain('-1.1300');
    fireEvent.click(screen.getByRole('button', { name: '比较矩阵' }));
    const runtime = screen.getByLabelText('比较条件') as HTMLSelectElement;
    fireEvent.change(runtime, { target: { value: runtime.options[1].value } });
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2);
  });

  it('keeps missing values, zero and duplicate observations distinct without averaging', () => {
    const rows = [candidate(10, 0.01), candidate(50, 0.01), candidate(60, 0.01)];
    rows[0].values[0].value = null;
    rows[1].values[0].value = '0';
    rows[1].values = rows[1].values.slice(0, 1);
    rows[2].values.push({ ...rows[2].values[0], runId: 'duplicate', value: '10' });
    renderComparison(rows);
    const table = screen.getByRole('table');
    expect(within(table).getAllByText('—')).toHaveLength(2);
    expect(within(table).getByText('0.0000')).not.toBeNull();
    expect(within(table).getByText('2 条结果')).not.toBeNull();
    expect(within(table).queryByText('4.4350')).toBeNull();
  });

  it('shows old reports without parameter dimensions as numbered observations and disables curves', () => {
    renderComparison(candidates.slice(0, 2).map((item) => ({ ...item, dimensions: undefined })));
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(3);
    const button = screen.getByRole('button', { name: '参数曲线' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe('该报告未保存候选参数取值，无法绘制参数曲线。');
    expect(screen.queryByText('该报告未保存候选参数取值，无法绘制参数曲线。')).toBeNull();
  });

  it('opens curves when non-axis note and enabled values are null', () => {
    const { container } = renderComparison(candidates.map((row) => ({
      ...row, dimensions: { ...row.dimensions, [NOTE]: null, [ENABLED]: null },
    })));
    const button = screen.getByRole('button', { name: '参数曲线' });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(screen.getByRole('img', { name: 'window 与年化夏普参数曲线' })).not.toBeNull();
    expect(container.querySelectorAll('circle')).toHaveLength(6);
    expect(screen.getByLabelText('固定其他参数').textContent).toContain('note 未记录');
    expect(screen.getByLabelText('固定其他参数').textContent).toContain('enabled 未记录');
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('keeps unknown non-axis conditions separate from known conditions', () => {
    const rows = [null, 'known'].flatMap((note) => [0, 50].map((value) => {
      const row = candidate(value, 0.01);
      return { ...row, candidateKey: `${row.candidateKey}-${note}`,
        dimensions: { ...row.dimensions, [NOTE]: note, [ENABLED]: false },
        values: row.values.map((item) => ({ ...item, value: note === null ? '1' : '9' })),
      };
    }));
    const { container } = renderComparison(rows);
    fireEvent.click(screen.getByRole('button', { name: '参数曲线' }));
    const conditions = screen.getByLabelText('固定其他参数') as HTMLSelectElement;
    const unknown = Array.from(conditions.options).find((item) => item.text.includes('note 未记录'))!;
    fireEvent.change(conditions, { target: { value: unknown.value } });
    expect(container.querySelectorAll('circle')).toHaveLength(4);
    expect(container.querySelector('svg')?.textContent).toContain('1.0000');
    expect(container.querySelector('svg')?.textContent).not.toContain('9.0000');
  });

  it('explains why a constant numeric grid cannot open curves', () => {
    renderComparison([candidate(10, 0.01)]);
    const button = screen.getByRole('button', { name: '参数曲线' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe('当前比较条件下，没有包含至少两个不同取值的数值参数。');
    expect(screen.queryByText('当前比较条件下，没有包含至少两个不同取值的数值参数。')).toBeNull();
    expect(screen.queryByText(/共同参数：/)).toBeNull();
  });

  it('does not turn a missing axis value into zero or silently omit its candidate', () => {
    const missing = candidate(50, 0.01);
    missing.dimensions = { ...missing.dimensions, [WINDOW]: null };
    renderComparison([candidate(10, 0.01), candidate(60, 0.01), missing]);
    const button = screen.getByRole('button', { name: '参数曲线' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe('数值参数取值缺失或无效，当前报告无法绘制参数曲线。');
    expect(screen.queryByText('数值参数取值缺失或无效，当前报告无法绘制参数曲线。')).toBeNull();
  });

  it('orders parameter values numerically instead of by candidate hashes', () => {
    renderComparison([candidate(100, 0.02), candidate(2, 0.02), candidate(10, 0.01)]);
    const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[0].textContent)).toEqual(['2', '10', '100']);
  });

  it('uses equal colors for equal displayed values and breaks curves at missing observations', () => {
    const rows = [candidate(10, 0.01), candidate(50, 0.01), candidate(60, 0.01)];
    rows[0].values[0].value = '0.123451';
    rows[1].values[0].value = '0.123449';
    rows[2].values[0].value = '0.123452';
    rows[1].values[1].value = null;
    const { container } = renderComparison(rows);
    const cells = container.querySelectorAll('td.comparison-value');
    expect(cells[0].getAttribute('style')).toBe(cells[4].getAttribute('style'));
    fireEvent.click(screen.getByRole('button', { name: '参数曲线' }));
    expect(container.querySelectorAll('circle')).toHaveLength(5);
    expect(container.querySelectorAll('polyline')).toHaveLength(3);
  });
});
