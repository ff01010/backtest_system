import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import EvaluationBuilder from '../features/experiments/EvaluationBuilder';
import {
  cancelEvaluationExport,
  createEvaluationExport,
  createEvaluationRun,
  fetchEvaluationCatalogs,
  fetchEvaluationExports,
  fetchEvaluationRun,
  fetchEvaluationRuns,
  fetchEvaluationSpecs,
  retryEvaluationExport,
  retryEvaluationRun,
  saveEvaluationSpec,
} from '../features/experiments/api';
import type {
  EvaluationCatalogs,
  EvaluationCompletenessSnapshot,
  EvaluationExport,
  EvaluationRun,
  EvaluationRunDetail,
  SavedEvaluationSpec,
} from '../features/experiments/contracts';


vi.mock('../features/experiments/api', () => ({
  cancelEvaluationExport: vi.fn(),
  createEvaluationExport: vi.fn(),
  createEvaluationRun: vi.fn(),
  evaluationExportDownloadUrl: vi.fn((exportId: string) => `/download/${exportId}`),
  fetchEvaluationCatalogs: vi.fn(),
  fetchEvaluationExports: vi.fn(),
  fetchEvaluationRun: vi.fn(),
  fetchEvaluationRuns: vi.fn(),
  fetchEvaluationSpecs: vi.fn(),
  retryEvaluationExport: vi.fn(),
  retryEvaluationRun: vi.fn(),
  saveEvaluationSpec: vi.fn(),
}));

const CATALOGS: EvaluationCatalogs = {
  experimentId: 'experiment-1',
  experimentRevisionId: 'revision-1',
  stages: [{
    stageId: 'stage-1', stageIndex: 0, stageType: 'finite_grid_v1',
    status: 'completed', plannedTrialCount: 1,
  }],
  metricCatalog: {
    version: 'metric_catalog_v1',
    metrics: [
      {
        metricId: 'totalReturn', displayName: '总收益率', dataType: 'decimal',
        unit: 'ratio', direction: 'higher', grain: 'trial_execution', currency: null,
        windowSemantics: 'frozen_date_scenario', annualization: 'none',
        benchmarkSemantics: 'none', comparabilityKeyVersion: 'metric_comparability_v1',
        nullSemantics: 'unsupported_or_missing_is_null',
      },
      {
        metricId: 'annualSharpe', displayName: '年化夏普', dataType: 'decimal',
        unit: 'ratio', direction: 'higher', grain: 'trial_execution', currency: null,
        windowSemantics: 'frozen_date_scenario', annualization: 'trading_days_252',
        benchmarkSemantics: 'none', comparabilityKeyVersion: 'metric_comparability_v1',
        nullSemantics: 'unsupported_or_missing_is_null',
      },
      {
        metricId: 'maxDrawdownRate', displayName: '最大回撤率', dataType: 'decimal',
        unit: 'ratio', direction: 'lower', grain: 'trial_execution', currency: null,
        windowSemantics: 'frozen_date_scenario', annualization: 'none',
        benchmarkSemantics: 'none', comparabilityKeyVersion: 'metric_comparability_v1',
        nullSemantics: 'unsupported_or_missing_is_null',
      },
    ],
  },
  dimensionCatalog: {
    version: 'dimension_catalog_v1',
    dimensions: [
      {
        dimensionId: 'dateScenarioId', displayName: '日期场景', dataType: 'string',
        unit: null, nullSemantics: 'missing_is_null', orderedValues: ['development'],
      },
      {
        dimensionId: 'parameter.strategyParameters.lookback', displayName: 'lookback',
        dataType: 'integer', unit: null, nullSemantics: 'missing_is_null',
        orderedValues: [10, 20],
      },
      {
        dimensionId: 'costScenarioId', displayName: '成本场景',
        dataType: 'string', unit: null, nullSemantics: 'missing_is_null',
        orderedValues: ['base', 'stress'],
      },
    ],
  },
  pipelineVersion: 'basic_evaluation_pipeline_v1',
  adapter: { type: 'legacy_single_bar_result', version: '1' },
};

const SAVED: SavedEvaluationSpec = {
  evaluationSpecId: 'spec-1',
  experimentId: 'experiment-1',
  revisionId: 'revision-1',
  versionNo: 1,
  name: '基础评价',
  specHash: 'a'.repeat(64),
  metricCatalogVersion: 'metric_catalog_v1',
  dimensionCatalogVersion: 'dimension_catalog_v1',
  normalizerVersion: null,
  createdAt: '2026-09-30T00:00:00Z',
  replayed: false,
  spec: {
    schemaVersion: 'evaluation_spec_v1',
    name: '基础评价',
    source: {
      experimentRevisionId: 'revision-1',
      stageIds: ['stage-1'],
      executionSelection: 'selected_execution_at_run_creation',
    },
    catalogs: {
      metricCatalogVersion: 'metric_catalog_v1',
      dimensionCatalogVersion: 'dimension_catalog_v1',
      normalizerVersion: null,
    },
    completeness: {
      allowPartial: false, minimumCompletionRate: '1.0000', requiredDimensionCoverage: [],
    },
    columns: ['totalReturn', 'annualSharpe', 'maxDrawdownRate'],
    derivedMetrics: [], filters: [], groupBy: [], aggregates: [], groupFilters: [],
    sort: [{ type: 'metric_sort_v1', metricId: 'annualSharpe', direction: 'desc' }],
    visualizations: [], reportSections: [],
  },
};

const RUN: EvaluationRun = {
  evaluationRunId: 'evaluation-run-1', evaluationSpecId: 'spec-1',
  experimentId: 'experiment-1', revisionId: 'revision-1', specVersionNo: 1,
  specName: '基础评价', status: 'queued', pipelineVersion: 'basic_evaluation_pipeline_v1',
  adapterType: 'legacy_single_bar_result', adapterVersion: '1',
  metricCatalogVersion: 'metric_catalog_v1', dimensionCatalogVersion: 'dimension_catalog_v1',
  executionCount: 1, resultSetHash: 'b'.repeat(64), outputHash: null, error: null,
  completeness: {
    plannedTrialCount: 1, generatedTrialCount: 1, succeededCount: 1,
    failedCount: 0, cancelledCount: 0, missingCount: 0,
    completionRate: '1.0000', isPartial: false, dimensionCoverage: [], exceptions: [],
  },
  attemptCount: 0, createdAt: '2026-09-30T00:00:00Z', finishedAt: null,
};

const DETAIL: EvaluationRunDetail = {
  ...RUN,
  status: 'succeeded',
  outputHash: 'c'.repeat(64),
  finishedAt: '2026-09-30T00:00:01Z',
  executionSet: [{
    stageId: 'stage-1', stageIndex: 0, stageType: 'finite_grid_v1', trialId: 'trial-1',
    trialIndex: 0, trialExecutionId: 'execution-1', runId: 'run-1',
    resultHash: 'd'.repeat(64), resultSchemaVersion: '3', actualDataHash: 'e'.repeat(64),
  }],
  output: {
    schemaVersion: 'basic_evaluation_output_v1',
    pipelineVersion: 'basic_evaluation_pipeline_v1',
    resultSetHash: 'b'.repeat(64),
    sourceRowCount: 1,
    matchedRowCount: 1,
    isPartial: false,
    reportStatus: 'complete',
    candidateSelectionEligible: true,
    finalTestEligible: false,
    datasetNotice: 'reference 不是最终 test。',
    completeness: RUN.completeness,
    columns: ['totalReturn', 'annualSharpe', 'maxDrawdownRate'],
    sort: [{ type: 'metric_sort_v1', metricId: 'annualSharpe', direction: 'desc' }],
    groupBy: [],
    rows: [{
      trialId: 'trial-1', trialExecutionId: 'execution-1', runId: 'run-1',
      stageId: 'stage-1', stageIndex: 0, trialIndex: 0, resultHash: 'd'.repeat(64),
      startDate: '2026-07-01', values: {
        totalReturn: '0.12', annualSharpe: null, maxDrawdownRate: '0.03',
      }, unsupportedReasons: { annualSharpe: 'metric_missing' },
    }],
    visualizations: [],
  },
  spec: SAVED.spec,
};

const EXPORT: EvaluationExport = {
  exportId: 'export-1', evaluationRunId: 'evaluation-run-1',
  evaluationSpecId: 'spec-1', experimentId: 'experiment-1',
  evaluationName: '基础评价', format: 'xlsx',
  columns: ['totalReturn', 'annualSharpe', 'maxDrawdownRate'], locale: 'zh-CN',
  outputHash: 'c'.repeat(64), status: 'queued',
  progress: { completed: 0, total: 1 }, attemptCount: 0,
  artifact: null, error: null, retryOfExportId: null,
  createdAt: '2026-09-30T00:00:02Z', startedAt: null,
  finishedAt: null, expiresAt: null,
};

async function selectFirstReport(): Promise<void> {
  fireEvent.change(
    await screen.findByRole('combobox', { name: '选择评价报告' }),
    { target: { value: DETAIL.evaluationRunId } },
  );
  await waitFor(() => expect(fetchEvaluationRun).toHaveBeenCalledWith(
    'experiment-1', DETAIL.evaluationRunId,
  ));
  await waitFor(() => expect(fetchEvaluationExports).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  vi.mocked(fetchEvaluationCatalogs).mockReset().mockResolvedValue(CATALOGS);
  vi.mocked(fetchEvaluationSpecs).mockReset().mockResolvedValue({ items: [], nextCursor: null });
  vi.mocked(fetchEvaluationRuns).mockReset().mockResolvedValue({ items: [], nextCursor: null });
  vi.mocked(saveEvaluationSpec).mockReset().mockResolvedValue(SAVED);
  vi.mocked(createEvaluationRun).mockReset().mockResolvedValue(RUN);
  vi.mocked(fetchEvaluationRun).mockReset().mockResolvedValue(DETAIL);
  vi.mocked(fetchEvaluationExports).mockReset().mockResolvedValue({ items: [], nextCursor: null });
  vi.mocked(createEvaluationExport).mockReset().mockResolvedValue(EXPORT);
  vi.mocked(cancelEvaluationExport).mockReset().mockResolvedValue({
    exportId: EXPORT.exportId, status: 'cancel_requested', replayed: false,
  });
  vi.mocked(retryEvaluationExport).mockReset().mockResolvedValue({
    ...EXPORT, exportId: 'export-2', retryOfExportId: EXPORT.exportId,
  });
  vi.mocked(retryEvaluationRun).mockReset().mockResolvedValue({
    evaluationRunId: RUN.evaluationRunId,
    status: 'queued',
    replayed: false,
  });
});

describe('EvaluationBuilder', () => {
  it('uses a full-width report layout until advanced evaluation is expanded', async () => {
    const { container } = render(
      <EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />,
    );

    await screen.findByRole('button', { name: '生成基础评价' });
    const layout = container.querySelector('.evaluation-layout');
    const basic = container.querySelector('.evaluation-basic-card');
    const reportBrowser = container.querySelector('.evaluation-report-browser');
    expect(layout?.classList.contains('is-advanced-open')).toBe(false);
    expect(basic).not.toBeNull();
    expect(reportBrowser).not.toBeNull();
    expect(basic!.compareDocumentPosition(reportBrowser!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/评价数据来自冻结 Trial/)).toBeNull();
    expect(screen.queryByText(/按完整策略参数组合汇总/)).toBeNull();
    expect(screen.queryByText(/选择报告后/)).toBeNull();
    expect(screen.queryByText(/完整评价规则编辑器/)).toBeNull();
    expect(screen.queryByText(/需要复用或重新计算/)).toBeNull();

    fireEvent.click(screen.getByText('自定义评价（高级）'));
    await waitFor(() => expect(layout?.classList.contains('is-advanced-open')).toBe(true));
  });

  it('saves and runs once while preserving null metric semantics', async () => {
    const onOpenRun = vi.fn();
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" onOpenRun={onOpenRun} />);

    const button = await screen.findByRole('button', { name: '生成基础评价' });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(saveEvaluationSpec).toHaveBeenCalledTimes(1));
    expect(createEvaluationRun).toHaveBeenCalledTimes(1);
    expect(vi.mocked(saveEvaluationSpec).mock.calls[0][2]).toMatch(/^[0-9a-f-]{36}$/);
    expect(vi.mocked(createEvaluationRun).mock.calls[0][2]).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByText('—')).not.toBeNull();
    expect(screen.queryByText('0', { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    expect(onOpenRun).toHaveBeenCalledWith('run-1', '688981.SH', '5m', '2026-07-01');
  });

  it('loads a saved version as a copy without running a backtest', async () => {
    vi.mocked(fetchEvaluationSpecs).mockResolvedValue({ items: [SAVED], nextCursor: null });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    fireEvent.click(await screen.findByText('自定义规则版本（1）'));
    fireEvent.click(await screen.findByRole('button', { name: '载入为副本' }));

    expect(screen.getByDisplayValue('基础评价 副本')).not.toBeNull();
    expect(createEvaluationRun).not.toHaveBeenCalled();
  });

  it('creates an asynchronous export with the frozen visible columns', async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [DETAIL], nextCursor: null });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();
    expect(await screen.findByText('完整评价')).not.toBeNull();
    expect(screen.getByText('报告列表')).not.toBeNull();
    expect(screen.getByText('共 1 份')).not.toBeNull();
    expect(screen.getByText('当前报告')).not.toBeNull();
    expect(document.querySelectorAll('.evaluation-report-timestamps time')).toHaveLength(2);
    expect(screen.queryByText(/冻结的展示与分组标签/)).toBeNull();
    expect((screen.getByRole('combobox', { name: '选择评价报告' }) as HTMLSelectElement).value).toBe(DETAIL.evaluationRunId);
    expect(((await screen.findByText('技术标识与冻结摘要')).closest('details') as HTMLDetailsElement).open).toBe(false);
    expect(within(screen.getByLabelText('评价导出')).queryByText(DETAIL.outputHash!)).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: '创建导出' }));

    await waitFor(() => expect(createEvaluationExport).toHaveBeenCalledTimes(1));
    expect((await screen.findByRole(
      'button', { name: '创建导出' },
    ) as HTMLButtonElement).disabled).toBe(false);
    expect(createEvaluationExport).toHaveBeenCalledWith(
      'evaluation-run-1',
      'xlsx',
      ['totalReturn', 'annualSharpe', 'maxDrawdownRate'],
      'zh-CN',
      expect.stringMatching(/^[0-9a-f-]{36}$/),
    );
  });

  it('starts polling when a newly created export enters the queue', async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [DETAIL], nextCursor: null });
    vi.mocked(fetchEvaluationExports)
      .mockResolvedValueOnce({ items: [], nextCursor: null })
      .mockResolvedValueOnce({ items: [EXPORT], nextCursor: null })
      .mockResolvedValueOnce({
        items: [{
          ...EXPORT,
          status: 'succeeded',
          artifact: {
            filename: 'result.xlsx', mediaType: 'application/xlsx',
            byteSize: 128, sha256: 'f'.repeat(64),
          },
        }],
        nextCursor: null,
      });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();
    const createButton = await screen.findByRole('button', { name: '创建导出' });
    await waitFor(() => expect(fetchEvaluationExports).toHaveBeenCalledTimes(1));
    fireEvent.click(createButton);

    await waitFor(
      () => expect(fetchEvaluationExports).toHaveBeenCalledTimes(3),
      { timeout: 2500 },
    );
    expect(await screen.findByText('XLSX · 已完成')).not.toBeNull();
  });

  it('reuses the export idempotency key after an uncertain network result', async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [DETAIL], nextCursor: null });
    vi.mocked(createEvaluationExport)
      .mockRejectedValueOnce(new Error('network failed'))
      .mockResolvedValueOnce(EXPORT);
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();
    const createButton = await screen.findByRole('button', { name: '创建导出' });
    fireEvent.click(createButton);
    expect(await screen.findByText('network failed')).not.toBeNull();
    fireEvent.click(createButton);

    await waitFor(() => expect(createEvaluationExport).toHaveBeenCalledTimes(2));
    expect((await screen.findByRole(
      'button', { name: '创建导出' },
    ) as HTMLButtonElement).disabled).toBe(false);
    expect(vi.mocked(createEvaluationExport).mock.calls[0][4]).toBe(
      vi.mocked(createEvaluationExport).mock.calls[1][4],
    );
  });

  it('shows completed, failed, cancelled and expired export actions', async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [DETAIL], nextCursor: null });
    vi.mocked(fetchEvaluationExports).mockResolvedValue({
      items: [
        {
          ...EXPORT,
          status: 'succeeded',
          artifact: {
            filename: 'report.xlsx', mediaType: 'application/xlsx',
            byteSize: 1024, sha256: 'f'.repeat(64),
          },
          expiresAt: '2026-10-07T00:00:00Z',
        },
        { ...EXPORT, exportId: 'export-failed', status: 'failed', error: { code: 'artifact_failed', message: '生成失败' } },
        { ...EXPORT, exportId: 'export-cancelled', status: 'cancelled' },
        { ...EXPORT, exportId: 'export-expired', status: 'expired' },
      ],
      nextCursor: null,
    });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();

    expect(await screen.findByRole('link', { name: '下载' })).not.toBeNull();
    expect(screen.getAllByRole('button', { name: '重新创建评价导出' })).toHaveLength(3);
    expect(screen.getByText(/SHA-256/)).not.toBeNull();
  });

  it('renders legacy basic evaluation output with run-level completeness fallback', async () => {
    vi.mocked(fetchEvaluationRun).mockResolvedValue({
      ...DETAIL,
      output: {
        schemaVersion: 'basic_evaluation_output_v1',
        pipelineVersion: 'basic_evaluation_pipeline_v1',
        resultSetHash: 'b'.repeat(64),
        sourceRowCount: 1,
        matchedRowCount: 1,
        columns: ['totalReturn'],
        sort: [],
        rows: DETAIL.output!.rows,
      },
    });
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      items: [{ ...RUN, status: 'succeeded' }], nextCursor: null,
    });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();

    expect(await screen.findByText('完整评价')).not.toBeNull();
    expect(screen.queryByText(/历史基础评价按保存时/)).toBeNull();
  });

  it('formats decimals and replaces automatic conclusions with the comparison matrix', async () => {
    const analysisDetail: EvaluationRunDetail = {
      ...DETAIL,
      output: {
        ...DETAIL.output!,
        rows: [{
          ...DETAIL.output!.rows[0],
          values: {
            totalReturn: '0.123456', annualSharpe: '-1.18345',
            maxDrawdownRate: '0.07558971490884193035845026230900836',
          },
          unsupportedReasons: {},
        }],
        visualizations: [
          {
            type: 'parameter_curve_v1',
            metricId: 'annualSharpe',
            parameterDimensionId: 'parameter.strategyParameters.lookback',
            series: [
              { seriesKey: 'series-1', points: [{ x: 10, median: '-1.18345', count: 1 }, { x: 20, median: '-1.18345', count: 1 }] },
              { seriesKey: 'series-2', points: [{ x: 10, median: '-1.1000', count: 1 }, { x: 20, median: '-1.1000', count: 1 }] },
            ],
          },
          {
            type: 'time_stability_v1',
            metricId: 'annualSharpe',
            candidates: [{
              candidateKey: 'candidate-1',
              dimensions: { 'parameter.strategyParameters.lookback': 10 },
              values: [
                { dateScenarioId: 'development-1', dateScenarioOrder: 0, role: 'development', value: '-1.18345' },
                { dateScenarioId: 'validation-1', dateScenarioOrder: 1, role: 'validation', value: '-1.2369' },
              ],
            }],
          },
        ],
      },
    };
    vi.mocked(fetchEvaluationRun).mockResolvedValue(analysisDetail);
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [analysisDetail], nextCursor: null });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    await selectFirstReport();

    expect(await screen.findByText('0.1235')).not.toBeNull();
    expect(screen.getByText('0.0756')).not.toBeNull();
    expect(screen.queryByText('0.123456')).toBeNull();
    expect(screen.getByRole('table', { name: '参数组合与日期场景比较矩阵' })).not.toBeNull();
    expect(screen.queryByText('各参数值的典型表现相同')).toBeNull();
    expect(screen.queryByText('当前只有一个候选')).toBeNull();
    expect(screen.getAllByText('-1.1835').length).toBeGreaterThan(0);
    expect(screen.queryByText(/共同参数：/)).toBeNull();
    expect(screen.getByText('其他已保存分析（1）').closest('details')?.open).toBe(false);
    expect(screen.queryByText(/纵向比较参数组合/)).toBeNull();
    expect(screen.queryByText(/全表共用色标/)).toBeNull();
    expect(screen.queryByText(/按报告保存的分组展示中位数/)).toBeNull();
  });

  it('only shows provisional thresholds after the user chooses a temporary evaluation', async () => {
    const onRetryFailed = vi.fn();
    render(
      <EvaluationBuilder
        experimentId="experiment-1"
        symbol="688981.SH"
        timeframe="5m"
        trialSummary={{ planned: 3, generated: 3, succeeded: 1, failed: 1, cancelled: 0 }}
        onRetryFailed={onRetryFailed}
      />,
    );

    expect(screen.queryByLabelText('最低完成率')).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));
    expect(onRetryFailed).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '使用成功结果生成临时评价' }));
    expect((screen.getByLabelText('最低完成率') as HTMLInputElement).value).toBe('0.3333');
    fireEvent.change(screen.getByLabelText('最低完成率'), { target: { value: '0.3000' } });
    fireEvent.click(screen.getByRole('button', { name: '添加维度覆盖' }));
    fireEvent.change(screen.getByLabelText('最低覆盖率 dateScenarioId'), { target: { value: '0.5000' } });
    fireEvent.click(screen.getByRole('button', { name: '确认生成临时评价' }));

    await waitFor(() => expect(saveEvaluationSpec).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(saveEvaluationSpec).mock.calls[0][1];
    expect(payload.completeness).toMatchObject({
      allowPartial: true,
      minimumCompletionRate: '0.3000',
      requiredDimensionCoverage: [{
        dimensionId: 'dateScenarioId',
        minimumCoverageRate: '0.5000',
      }],
    });
    expect(payload.groupBy).toEqual(['parameter.strategyParameters.lookback']);
    expect(payload.catalogs.normalizerVersion).toBe('candidate_rate_distribution_v1');
    expect(payload.aggregates.map((item) => item.aggregateId)).toEqual([
      'medianAnnualSharpe',
      'meanTotalReturn',
      'worstMaxDrawdownRate',
      'annualSharpeDispersion',
      'successfulTrialCount',
    ]);
    expect(payload.visualizations.slice(0, 3)).toEqual([
      { type: 'time_stability_v1', metricId: 'annualSharpe' },
      { type: 'time_stability_v1', metricId: 'totalReturn' },
      { type: 'time_stability_v1', metricId: 'maxDrawdownRate' },
    ]);
    expect(payload.visualizations[3]).toMatchObject({
      type: 'cost_stress_v1',
      baselineScenarioId: 'base',
    });
  });

  it('shows formula metric ids and validates unknown metrics and literal division by zero', async () => {
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    fireEvent.click(await screen.findByText('自定义评价（高级）'));
    fireEvent.click(screen.getByRole('button', { name: '添加安全公式' }));
    expect(screen.getByText('totalReturn')).not.toBeNull();

    const formula = screen.getByLabelText('安全公式');
    fireEvent.change(formula, { target: { value: 'unknownMetric + 1' } });
    expect(screen.getAllByText('未知指标：unknownMetric').length).toBeGreaterThan(0);
    fireEvent.change(formula, { target: { value: 'totalReturn / 0' } });
    expect(screen.getAllByText('公式存在明确的除零').length).toBeGreaterThan(0);
  });

  it('shows provisional coverage and exceptions without redundant dataset hints', async () => {
    const partialCompleteness: EvaluationCompletenessSnapshot = {
      ...RUN.completeness,
      plannedTrialCount: 3,
      succeededCount: 1,
      failedCount: 1,
      missingCount: 1,
      completionRate: '0.3333',
      isPartial: true,
      dimensionCoverage: [{
        dimensionId: 'dateScenarioId', requiredRate: '0.5000',
        minimumObservedRate: '0.5000',
        values: [{
          value: 'development', expectedCount: 2, succeededCount: 1,
          coverageRate: '0.5000',
        }],
      }],
      exceptions: [
        {
          stageId: 'stage-1', stageIndex: 0, trialId: 'trial-2', trialIndex: 1,
          status: 'failed', materializationStatus: 'materialized', error: { message: '运行失败' },
        },
        {
          stageId: 'stage-1', stageIndex: 0, trialId: null, trialIndex: 2,
          status: 'missing', materializationStatus: 'not_generated', error: null,
        },
      ],
    };
    const partialDetail: EvaluationRunDetail = {
      ...DETAIL,
      completeness: partialCompleteness,
      output: {
        ...DETAIL.output!,
        isPartial: true,
        reportStatus: 'provisional',
        candidateSelectionEligible: false,
        completeness: partialCompleteness,
      },
    };
    vi.mocked(fetchEvaluationRun).mockResolvedValue(partialDetail);
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      items: [{ ...RUN, status: 'succeeded', completeness: partialDetail.completeness }],
      nextCursor: null,
    });
    render(<EvaluationBuilder
      experimentId="experiment-1"
      symbol="688981.SH"
      timeframe="5m"
      scenarioNames={[{
        scenarioId: 'development', name: '开发区间',
        startDate: '2026-07-01', endDate: '2026-07-02',
      }]}
    />);

    await selectFirstReport();

    expect(await screen.findByText('临时评价')).not.toBeNull();
    expect(screen.getByText('失败 1')).not.toBeNull();
    expect(screen.queryByText(/不代表最终测试/)).toBeNull();
    expect(screen.getByText('关键维度覆盖（1）')).not.toBeNull();
    expect(screen.getByText(/开发区间：1\/2/)).not.toBeNull();
    expect(screen.queryByText(/development：1\/2/)).toBeNull();
    expect(screen.getByText(/失败、取消与缺失项/)).not.toBeNull();
  });
});
