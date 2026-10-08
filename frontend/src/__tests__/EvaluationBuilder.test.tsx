import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  it('saves and runs once while preserving null metric semantics', async () => {
    const onOpenRun = vi.fn();
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" onOpenRun={onOpenRun} />);

    const button = await screen.findByRole('button', { name: '保存版本并运行' });
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

    fireEvent.click(await screen.findByRole('button', { name: '载入为副本' }));

    expect(screen.getByDisplayValue('基础评价 副本')).not.toBeNull();
    expect(createEvaluationRun).not.toHaveBeenCalled();
  });

  it('creates an asynchronous export with the frozen visible columns', async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({ items: [DETAIL], nextCursor: null });
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));
    fireEvent.click(await screen.findByRole('button', { name: '创建导出' }));

    await waitFor(() => expect(createEvaluationExport).toHaveBeenCalledTimes(1));
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

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));
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

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));
    const createButton = await screen.findByRole('button', { name: '创建导出' });
    fireEvent.click(createButton);
    expect(await screen.findByText('network failed')).not.toBeNull();
    fireEvent.click(createButton);

    await waitFor(() => expect(createEvaluationExport).toHaveBeenCalledTimes(2));
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

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));

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

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));

    expect(await screen.findByText('完整评价')).not.toBeNull();
    expect(screen.getByText(/历史基础评价按保存时/)).not.toBeNull();
  });

  it('saves partial policy, safe formula and an actual-point parameter curve', async () => {
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    fireEvent.click(await screen.findByLabelText('允许 provisional 部分评价'));
    fireEvent.change(screen.getByLabelText('最低完成率'), { target: { value: '0.7500' } });
    fireEvent.click(screen.getByRole('button', { name: '添加维度覆盖' }));
    fireEvent.change(screen.getByLabelText('最低覆盖率 dateScenarioId'), { target: { value: '0.5000' } });
    fireEvent.click(screen.getByRole('button', { name: '添加安全公式' }));
    fireEvent.click(screen.getByRole('button', { name: '参数曲线' }));
    fireEvent.click(screen.getByRole('button', { name: '成本压力' }));
    fireEvent.change(screen.getByLabelText('成本退化阈值'), { target: { value: '0.2500' } });
    fireEvent.click(screen.getByRole('button', { name: '保存版本并运行' }));

    await waitFor(() => expect(saveEvaluationSpec).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(saveEvaluationSpec).mock.calls[0][1];
    expect(payload.completeness).toMatchObject({
      allowPartial: true,
      minimumCompletionRate: '0.7500',
      requiredDimensionCoverage: [{
        dimensionId: 'dateScenarioId',
        minimumCoverageRate: '0.5000',
      }],
    });
    expect(payload.derivedMetrics[0]).toMatchObject({
      type: 'row_formula_v1',
      formula: 'coalesce(totalReturn, 0)',
    });
    expect(payload.visualizations[0]).toMatchObject({
      type: 'parameter_curve_v1',
      parameterDimensionId: 'parameter.strategyParameters.lookback',
    });
    expect(payload.visualizations[1]).toMatchObject({
      type: 'cost_stress_v1',
      baselineScenarioId: 'base',
      degradationThreshold: '0.2500',
    });
  });

  it('shows provisional coverage, exceptions and the final-test disclaimer', async () => {
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
    render(<EvaluationBuilder experimentId="experiment-1" symbol="688981.SH" timeframe="5m" />);

    fireEvent.click(await screen.findByRole('button', { name: /基础评价 · succeeded/ }));

    expect(await screen.findByText('部分评价（provisional）')).not.toBeNull();
    expect(screen.getByText('失败 1')).not.toBeNull();
    expect(screen.getByText(/不是最终 test/)).not.toBeNull();
    expect(screen.getByText('关键维度覆盖（1）')).not.toBeNull();
    expect(screen.getByText(/development：1\/2/)).not.toBeNull();
    expect(screen.getByText(/失败、取消与缺失项/)).not.toBeNull();
  });
});
