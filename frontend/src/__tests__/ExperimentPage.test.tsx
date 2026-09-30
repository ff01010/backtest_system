import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExperimentPage from '../features/experiments/ExperimentPage';
import {
  cancelExperiment,
  createExperiment,
  fetchExperimentDetail,
  fetchExperiments,
  fetchExperimentSubject,
  fetchExperimentTrials,
  findCreatedExperiment,
  previewExperiment,
  resumeExperiment,
} from '../features/experiments/api';
import type {
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentPreview,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
} from '../features/experiments/contracts';

vi.mock('../features/experiments/api', () => ({
  cancelExperiment: vi.fn(),
  createExperiment: vi.fn(),
  fetchExperimentDetail: vi.fn(),
  fetchExperiments: vi.fn(),
  fetchExperimentSubject: vi.fn(),
  fetchExperimentTrials: vi.fn(),
  findCreatedExperiment: vi.fn(),
  previewExperiment: vi.fn(),
  resumeExperiment: vi.fn(),
}));

const SUBJECT_REF: ExperimentSubjectRef = {
  subjectType: 'legacy_single_strategy',
  subjectId: 'config-1',
  subjectVersion: 'version-1',
};

const SUBJECT: ExperimentSubject = {
  subjectRef: SUBJECT_REF,
  displayName: '均值回归',
  templateType: 'legacy_single_strategy_v1',
  runTemplateDigest: 'a'.repeat(64),
  fixedStructure: {
    symbol: '688981.SH',
    timeframe: '5m',
    fillModel: 'bar_next_open',
    scriptCodeDigest: 'b'.repeat(64),
    feeProfile: {
      profileId: 'default_a_share',
      version: 'fee-v1',
      rates: {
        commissionRate: '0.00008',
        minimumCommission: '5',
        transferFeeRate: '0.00001',
        stampDutyRate: '0.0005',
      },
    },
  },
  tunableInputCatalog: {
    schemaVersion: 'tunable_input_catalog_v1',
    inputs: [{
      inputId: 'strategyParameters.window',
      sourceName: 'window',
      type: 'integer',
      group: 'strategyParameters',
      defaultValue: 20,
      overridePolicy: 'finite_candidates',
    }],
    scenarioEditors: [],
  },
  assetManifestSummary: {},
  contracts: {},
  defaultExperimentInputs: {
    strategyParameters: { window: 20 },
    dateScenario: {
      scenarioId: 'development-1',
      name: '默认区间',
      role: 'development',
      startDate: '2026-07-01',
      endDate: '2026-07-02',
    },
    initialCapital: '100000',
    slippage: '0',
  },
  capability: {},
};

const PREVIEW: ExperimentPreview = {
  previewToken: 'preview-token',
  expiresAt: '2026-09-29T00:15:00Z',
  specHash: 'c'.repeat(64),
  canonicalizationVersion: 'canonical_json_v1',
  runTemplateDigest: 'd'.repeat(64),
  datasetSnapshotDigest: 'e'.repeat(64),
  capabilityDigest: 'f'.repeat(64),
  budgetDigest: '1'.repeat(64),
  budgetVersion: 'experiment_budget_v1',
  normalizedSpec: {},
  plannedTrialCount: 2,
  dimensionCounts: { 'strategyParameters.window': 2 },
  dimensionNormalization: {
    'strategyParameters.window': { submitted: 2, normalized: 2 },
  },
  resourceEstimate: {
    estimatedBarCount: 96,
    maxBarsPerTrial: 48,
    resourceClass: 'light',
    confidence: 'coverage_metadata',
  },
  limitDecision: { allowed: true, warnings: [], blockingReasons: [] },
  examples: [{
    trialIndex: 0,
    trialFingerprint: '2'.repeat(64),
    seedBundle: { derivationVersion: 'trial-seed-v1', baseSeed: 1 },
    dimensionAssignment: { strategyParameters: { window: 10 } },
    compiledRequestDigest: '3'.repeat(64),
  }],
};

const CREATED: ExperimentCreateResult = {
  experimentId: 'experiment-1',
  revisionId: 'revision-1',
  stageId: 'stage-1',
  status: 'preparing',
  plannedTrialCount: 2,
  generatedTrialCount: 0,
  specHash: PREVIEW.specHash,
  replayed: false,
  createdAt: '2026-09-29T00:00:00Z',
};

const DETAIL: ExperimentDetail = {
  experimentId: 'experiment-1',
  name: '均值回归 · 有限网格',
  description: '',
  status: 'queued',
  snapshotVersion: 2,
  revision: { revisionId: 'revision-1', revisionNo: 1, specHash: PREVIEW.specHash },
  stage: {
    stageId: 'stage-1',
    stageType: 'finite_grid_v1',
    generatorVersion: '1',
    status: 'queued',
    plannedTrialCount: 2,
    generatedTrialCount: 2,
    generationCursor: 2,
    trialCollectionDigest: '4'.repeat(64),
    inFlightTrialCount: 0,
    terminalTrialCount: 0,
    succeededTrialCount: 0,
    failedTrialCount: 0,
    cancelledTrialCount: 0,
    queuedTrialCount: 2,
  },
  resourceBudget: {},
  failurePolicy: {},
  errorAggregates: [],
  consecutiveFailureCount: 0,
  frozenDefinition: {
    subjectRef: SUBJECT_REF,
    fixedStructure: SUBJECT.fixedStructure,
    plan: {},
    datasetSnapshots: [{}],
    determinism: {},
  },
  blockedReason: null,
  createdAt: '2026-09-29T00:00:00Z',
  updatedAt: '2026-09-29T00:00:01Z',
};

const TRIAL: ExperimentTrial = {
  trialId: 'trial-1',
  trialIndex: 0,
  stageId: 'stage-1',
  candidateKey: 'candidate-1',
  dimensionAssignment: {
    dateScenario: {
      scenarioId: 'development-1',
      name: '开发集',
      startDate: '2026-07-01',
    },
  },
  materializationStatus: 'materialized',
  summaryStatus: 'succeeded',
  automaticRetryCount: 0,
  retryNotBefore: null,
  activeExecution: null,
  selectedExecution: {
    executionId: 'execution-1',
    runId: 'run-1',
    status: 'succeeded',
    resultHash: 'a'.repeat(64),
    resultSchemaVersion: '3',
    actualDataHash: 'b'.repeat(64),
  },
  errorSummary: null,
  runId: 'run-1',
  runUrl: '/?workspace=backtest&runId=run-1',
  executionId: 'execution-1',
};

beforeEach(() => {
  localStorage.clear();
  vi.mocked(fetchExperimentSubject).mockReset().mockResolvedValue(SUBJECT);
  vi.mocked(previewExperiment).mockReset().mockResolvedValue(PREVIEW);
  vi.mocked(createExperiment).mockReset().mockResolvedValue(CREATED);
  vi.mocked(findCreatedExperiment).mockReset().mockResolvedValue({ ...CREATED, replayed: true });
  vi.mocked(fetchExperimentDetail).mockReset().mockResolvedValue(DETAIL);
  vi.mocked(fetchExperiments).mockReset().mockResolvedValue({ items: [], nextCursor: null });
  vi.mocked(fetchExperimentTrials).mockReset().mockResolvedValue({
    items: [TRIAL],
    nextCursor: null,
  });
  vi.mocked(cancelExperiment).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    status: 'cancelled',
  });
  vi.mocked(resumeExperiment).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    revisionId: 'revision-1',
    status: 'queued',
  });
});

describe('ExperimentPage', () => {
  it('shows a clear gate when automation is not enabled', () => {
    render(
      <ExperimentPage
        userId="user-1"
        enabled={false}
        subject={null}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    expect(screen.getByRole('alert').textContent).toContain('自动化回测能力尚未启用');
    expect(fetchExperiments).not.toHaveBeenCalled();
  });

  it('invalidates the preview immediately after any form change', async () => {
    render(
      <ExperimentPage
        userId="user-1"
        subject={SUBJECT_REF}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );
    await screen.findByRole('heading', { name: '均值回归' });

    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));
    await screen.findByText('Trial 总数');
    expect(previewExperiment).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('实验名称'), { target: { value: '变更后的实验' } });

    await waitFor(() => expect(screen.queryByText('Trial 总数')).toBeNull());
    expect(screen.getByText('配置已变化，之前的预览令牌已失效。')).not.toBeNull();
  });

  it('discards an in-flight preview response after the draft changes', async () => {
    let resolvePreview: (preview: ExperimentPreview) => void = () => {};
    vi.mocked(previewExperiment).mockReturnValue(new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    render(
      <ExperimentPage
        userId="user-1"
        subject={SUBJECT_REF}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );
    await screen.findByRole('heading', { name: '均值回归' });

    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));
    fireEvent.change(screen.getByLabelText('实验名称'), { target: { value: '在途修改' } });
    await act(async () => resolvePreview(PREVIEW));

    expect(screen.queryByText('Trial 总数')).toBeNull();
    expect(screen.getByText('配置已变化，之前的预览令牌已失效。')).not.toBeNull();
  });

  it('creates once, clears the matching draft, and opens aggregate progress', async () => {
    const onExperimentChange = vi.fn();
    render(
      <ExperimentPage
        userId="user-1"
        subject={SUBJECT_REF}
        onExperimentChange={onExperimentChange}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );
    await screen.findByRole('heading', { name: '均值回归' });
    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));
    await screen.findByText('Trial 总数');

    fireEvent.click(screen.getByRole('button', { name: '确认创建并开始' }));

    await waitFor(() => expect(createExperiment).toHaveBeenCalledTimes(1));
    expect(onExperimentChange).toHaveBeenCalledWith('experiment-1');
    expect(localStorage.length).toBe(0);
  });

  it('recovers an uncertain create response with the original idempotency key', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(createExperiment).mockRejectedValueOnce(new TypeError('network failed'));
    render(
      <ExperimentPage
        userId="user-1"
        subject={SUBJECT_REF}
        onExperimentChange={onExperimentChange}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );
    await screen.findByRole('heading', { name: '均值回归' });
    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));
    await screen.findByText('Trial 总数');
    fireEvent.click(screen.getByRole('button', { name: '确认创建并开始' }));

    await waitFor(() => expect(findCreatedExperiment).toHaveBeenCalledTimes(1));
    expect(findCreatedExperiment).toHaveBeenCalledWith(
      vi.mocked(createExperiment).mock.calls[0][1],
    );
    expect(onExperimentChange).toHaveBeenCalledWith('experiment-1');
  });

  it('shows aggregate progress and drills down through the selected ordinary run', async () => {
    const onOpenRun = vi.fn();
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
        onOpenRun={onOpenRun}
      />,
    );

    await screen.findByText('2 / 2');
    expect(screen.getByText('冻结定义')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看单次结果' }));
    expect(onOpenRun).toHaveBeenCalledWith('run-1', '688981.SH', '5m', '20260701');
    expect(fetchExperimentDetail).toHaveBeenCalledTimes(1);
    expect(fetchExperimentTrials).toHaveBeenCalledWith('experiment-1');
  });

  it('explains that capacity blocks resume the same revision', async () => {
    vi.mocked(fetchExperimentDetail).mockResolvedValueOnce({
      ...DETAIL,
      status: 'queued',
      blockedReason: {
        code: 'experiment.capacity_blocked',
        message: '系统容量低于该 Revision 创建时预算',
        scope: null,
        blockedAt: null,
      },
    });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('条件恢复后会从当前 Revision 继续');
  });

  it('resumes an execution block with one idempotent command', async () => {
    vi.mocked(fetchExperimentDetail).mockResolvedValueOnce({
      ...DETAIL,
      status: 'queued',
      blockedReason: {
        code: 'experiment.execution_blocked',
        message: '连续系统性错误已触发实验熔断',
        scope: 'stage',
        blockedAt: '2026-09-30T00:00:00Z',
      },
      errorAggregates: [{
        fingerprint: 'f'.repeat(64),
        category: 'system_error',
        code: 'runtime.version_mismatch',
        message: '平台运行时版本不一致',
        failureStage: 'bar.initialize',
        component: 'bar_runner',
        retryable: true,
        count: 3,
        lastTerminalSequence: 3,
      }],
      consecutiveFailureCount: 3,
    });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    await screen.findByText('runtime.version_mismatch');
    const button = screen.getByRole('button', { name: '继续未完成 Trial' });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(resumeExperiment).toHaveBeenCalledTimes(1));
    expect(resumeExperiment).toHaveBeenCalledWith(
      'experiment-1',
      'revision-1',
      '用户在实验详情页确认继续未完成 Trial',
      expect.any(String),
    );
  });
});
