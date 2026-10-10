import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExperimentPage, { isStandardBarConfig } from '../features/experiments/ExperimentPage';
import { fetchStrategyConfigs } from '../api/client';
import {
  archiveExperiment,
  cancelExperiment,
  createExperiment,
  fetchAdminArchivedExperiments,
  fetchExperimentBacktestRuns,
  fetchExperimentDetail,
  fetchExperimentRevisions,
  fetchExperiments,
  fetchExperimentSubject,
  fetchExperimentTrials,
  findCreatedExperiment,
  previewExperiment,
  previewRetryFailedTrials,
  retryFailedTrials,
  resumeExperiment,
  restoreArchivedExperiment,
  requestExperimentPurge,
} from '../features/experiments/api';
import type {
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentListItem,
  ExperimentPreview,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
} from '../features/experiments/contracts';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, fetchStrategyConfigs: vi.fn() };
});

vi.mock('../features/experiments/api', () => ({
  archiveExperiment: vi.fn(),
  cancelExperiment: vi.fn(),
  createExperiment: vi.fn(),
  fetchAdminArchivedExperiments: vi.fn(),
  fetchExperimentBacktestRuns: vi.fn(),
  fetchExperimentDetail: vi.fn(),
  fetchExperimentRevisions: vi.fn(),
  fetchExperiments: vi.fn(),
  fetchExperimentSubject: vi.fn(),
  fetchExperimentTrials: vi.fn(),
  findCreatedExperiment: vi.fn(),
  previewExperiment: vi.fn(),
  previewRetryFailedTrials: vi.fn(),
  retryFailedTrials: vi.fn(),
  resumeExperiment: vi.fn(),
  restoreArchivedExperiment: vi.fn(),
  requestExperimentPurge: vi.fn(),
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
  source: null,
  status: 'queued',
  snapshotVersion: 2,
  relatedBacktestRuns: {
    totalCount: 2,
    initialCount: 1,
    retryCount: 1,
  },
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
    plan: {
      planVersion: 'experiment_plan_v1',
      stages: [{
        stageType: 'finite_grid_v1',
        generatorVersion: '1',
        parameterGrid: [{ inputId: 'strategyParameters.window', values: [10, 20] }],
        dateScenarios: [{ ...SUBJECT.defaultExperimentInputs.dateScenario }],
        capitalScenarios: ['100000'],
        costScenarios: [{ scenarioId: 'cost-1', slippage: '0' }],
        executionScenarios: [{ scenarioId: 'immediate-1', algorithm: 'immediate', parameters: {} }],
      }],
    },
    datasetSnapshots: [{}],
    determinism: {},
  },
  blockedReason: null,
  archivedAt: null,
  retentionUntil: null,
  purgeJobId: null,
  createdAt: '2026-09-29T00:00:00Z',
  updatedAt: '2026-09-29T00:00:01Z',
};

function experimentListItem(experimentId: string, name: string): ExperimentListItem {
  return {
    experimentId,
    name,
    source: null,
    subject: { ...SUBJECT_REF, name: '均值回归' },
    currentRevisionId: `revision-${experimentId}`,
    revisionNo: 1,
    status: 'completed',
    reason: null,
    runPath: 'standard_bar',
    scopeType: 'single_symbol',
    symbol: '688981.SH',
    timeframe: '5m',
    fillModel: 'bar_next_open',
    plannedTrialCount: 2,
    generatedTrialCount: 2,
    materializedTrialCount: 2,
    queuedTrialCount: 0,
    runningTrialCount: 0,
    succeededTrialCount: 2,
    failedTrialCount: 0,
    cancelledTrialCount: 0,
    createdAt: '2026-08-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    archivedAt: '2026-09-01T00:00:00Z',
    retentionUntil: '2026-11-01T00:00:00Z',
    purgeJobId: null,
  };
}

const TRIAL: ExperimentTrial = {
  trialId: 'trial-1',
  trialIndex: 0,
  stageId: 'stage-1',
  candidateKey: 'candidate-1',
  dimensionAssignment: {
    strategyParameters: {
      window: 10,
      threshold: '0.5',
      enabled: true,
      note: '',
    },
    dateScenario: {
      scenarioId: 'development-1',
      name: '开发集',
      role: 'development',
      startDate: '2026-07-01',
      endDate: '2026-07-02',
    },
    initialCapital: '100000',
    costScenario: {
      scenarioId: 'cost-1',
      name: '基础成本',
      slippage: '0',
    },
    executionScenario: {
      scenarioId: 'immediate-1',
      algorithm: 'immediate',
      parameters: {},
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
  executionAttempts: [{
    executionId: 'execution-1',
    executionNo: 1,
    runId: 'run-1',
    status: 'succeeded',
    terminalStatus: 'succeeded',
    createdReason: 'initial',
    selected: true,
    active: false,
    resultHash: 'a'.repeat(64),
    errorSummary: null,
    queuedAt: '2026-09-29T00:00:00Z',
    startedAt: '2026-09-29T00:00:01Z',
    finishedAt: '2026-09-29T00:00:02Z',
  }],
  runId: 'run-1',
  runUrl: '/?workspace=backtest&runId=run-1',
  executionId: 'execution-1',
};

beforeEach(() => {
  localStorage.clear();
  vi.mocked(fetchStrategyConfigs).mockReset().mockResolvedValue([]);
  vi.mocked(fetchExperimentSubject).mockReset().mockResolvedValue(SUBJECT);
  vi.mocked(previewExperiment).mockReset().mockResolvedValue(PREVIEW);
  vi.mocked(createExperiment).mockReset().mockResolvedValue(CREATED);
  vi.mocked(findCreatedExperiment).mockReset().mockResolvedValue({ ...CREATED, replayed: true });
  vi.mocked(fetchExperimentDetail).mockReset().mockResolvedValue(DETAIL);
  vi.mocked(fetchExperimentRevisions).mockReset().mockResolvedValue({
    items: [{
      revisionId: 'revision-1',
      revisionNo: 1,
      current: true,
      status: 'queued',
      specHash: PREVIEW.specHash,
      stage: {
        stageId: 'stage-1',
        status: 'queued',
        plannedTrialCount: 2,
        generatedTrialCount: 2,
        inFlightTrialCount: 0,
        terminalTrialCount: 0,
        succeededTrialCount: 0,
        failedTrialCount: 0,
        cancelledTrialCount: 0,
      },
      createdAt: '2026-09-29T00:00:00Z',
      updatedAt: '2026-09-29T00:00:01Z',
      terminalAt: null,
    }],
    nextCursor: null,
  });
  vi.mocked(fetchExperiments).mockReset().mockResolvedValue({ items: [], nextCursor: null });
  vi.mocked(fetchAdminArchivedExperiments).mockReset().mockResolvedValue({
    items: [], nextCursor: null,
  });
  vi.mocked(fetchExperimentBacktestRuns).mockReset().mockResolvedValue({
    totalCount: 2,
    initialCount: 1,
    retryCount: 1,
    items: [{
      revisionId: 'revision-1',
      revisionNo: 1,
      currentRevision: true,
      stageId: 'stage-1',
      stageIndex: 0,
      trialId: 'trial-1',
      trialIndex: 0,
      executionId: 'execution-1',
      executionNo: 1,
      createdReason: 'initial',
      selected: false,
      runId: 'run-1',
      displayNo: 187,
      status: 'failed',
    }, {
      revisionId: 'revision-1',
      revisionNo: 1,
      currentRevision: true,
      stageId: 'stage-1',
      stageIndex: 0,
      trialId: 'trial-1',
      trialIndex: 0,
      executionId: 'execution-2',
      executionNo: 2,
      createdReason: 'manual_retry',
      selected: true,
      runId: 'run-2',
      displayNo: 193,
      status: 'succeeded',
    }],
    nextCursor: null,
  });
  vi.mocked(fetchExperimentTrials).mockReset().mockResolvedValue({
    items: [TRIAL],
    nextCursor: null,
  });
  vi.mocked(archiveExperiment).mockReset();
  vi.mocked(restoreArchivedExperiment).mockReset();
  vi.mocked(requestExperimentPurge).mockReset();
  vi.mocked(cancelExperiment).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    status: 'cancelled',
  });
  vi.mocked(resumeExperiment).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    revisionId: 'revision-1',
    status: 'queued',
  });
  vi.mocked(previewRetryFailedTrials).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    revisionId: 'revision-1',
    selectedTrialCount: 1,
    selectionDigest: '9'.repeat(64),
    trials: [{
      trialId: 'trial-1',
      trialIndex: 0,
      previousExecutionId: 'execution-1',
      previousExecutionNo: 1,
      errorSummary: { code: 'runtime.failed' },
    }],
  });
  vi.mocked(retryFailedTrials).mockReset().mockResolvedValue({
    experimentId: 'experiment-1',
    revisionId: 'revision-1',
    status: 'running',
    operationId: 'operation-1',
    selectedTrialCount: 1,
    selectionDigest: '9'.repeat(64),
    executions: [{
      executionId: 'execution-2',
      executionNo: 2,
      runId: 'run-2',
      dispatchKey: 'experiment:execution-2',
      createdReason: 'manual_retry',
    }],
    replayed: false,
    acceptedAt: '2026-09-30T00:00:00Z',
  });
});

describe('ExperimentPage', () => {
  it('只把明确使用标准 Bar 成交模型的发布策略列入自动化入口', () => {
    const base = {
      id: 'config-1', name: '标准 Bar', scriptId: 'script-1', strategyVersionId: 'strategy-v1',
      scriptVersionNo: 1, description: '', code: 'def on_bar(bar, state): return []',
      entryType: 'on_bar', signalRequired: false, strategy: 'user_script', mode: 'backtest',
      symbol: '688981.SH', timeframe: '5m', benchmarkSymbol: null, accountId: null,
      enabled: true, published: true, publishedVersionId: 'published-1', revision: 1,
      createdAt: '2026-10-08', updatedAt: '2026-10-08',
    } as const;

    expect(isStandardBarConfig({
      ...base,
      paramsJson: { params: { fillModel: 'bar_next_open' } },
    })).toBe(true);
    expect(isStandardBarConfig({
      ...base,
      paramsJson: { params: { fillModel: 'l2_quote' } },
    })).toBe(false);
    expect(isStandardBarConfig({
      ...base,
      paramsJson: { params: { fillModel: 'unknown_model' } },
    })).toBe(false);
    expect(isStandardBarConfig({
      ...base,
      paramsJson: { params: {} },
    })).toBe(false);
  });
  it('keeps an admin owner archived experiment reachable for restore', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(fetchExperiments)
      .mockResolvedValueOnce({ items: [], nextCursor: null })
      .mockResolvedValueOnce({
        items: [experimentListItem('owned-archive', '我的归档实验')],
        nextCursor: null,
      });
    render(
      <ExperimentPage
        userId="admin-1"
        isAdmin
        subject={null}
        onExperimentChange={onExperimentChange}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看已归档实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /我的归档实验/ }));

    expect(onExperimentChange).toHaveBeenCalledWith('owned-archive');
  });

  it('uses the admin archive inventory to purge another user experiment', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('管理员按保留策略清理');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(fetchAdminArchivedExperiments).mockResolvedValue({
      items: [{
        experimentId: 'other-experiment',
        name: '其他用户实验',
        currentRevisionId: 'other-revision',
        status: 'completed',
        snapshotVersion: 7,
        archivedAt: '2026-08-01T00:00:00Z',
        retentionUntil: '2026-09-01T00:00:00Z',
        purgeJobId: null,
        updatedAt: '2026-10-01T00:00:00Z',
      }],
      nextCursor: null,
    });
    vi.mocked(requestExperimentPurge).mockResolvedValue({
      purgeId: 'purge-other',
      experimentId: 'other-experiment',
      revisionId: 'other-revision',
      status: 'queued',
      phase: 'artifacts',
      scopeDigest: 'a'.repeat(64),
      scopeCounts: { experiment: 1 },
      snapshotVersion: 8,
      reason: '管理员按保留策略清理',
      operationId: 'purge-other',
      replayed: false,
      acceptedAt: '2026-10-08T00:00:00Z',
    });
    render(
      <ExperimentPage
        userId="admin-1"
        isAdmin
        subject={null}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看已归档实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '物理清理 其他用户实验' }));

    await waitFor(() => expect(requestExperimentPurge).toHaveBeenCalledWith(
      'other-experiment', 'other-revision', 7,
      '管理员按保留策略清理', expect.any(String),
    ));
    expect(fetchAdminArchivedExperiments).toHaveBeenCalledTimes(1);
  });

  it('loads later pages for owner archives and the admin purge inventory', async () => {
    vi.mocked(fetchExperiments)
      .mockResolvedValueOnce({ items: [], nextCursor: null })
      .mockResolvedValueOnce({
        items: [experimentListItem('owned-first', '我的归档第一页')],
        nextCursor: 'owner-next',
      })
      .mockResolvedValueOnce({
        items: [experimentListItem('owned-first', '我的归档第一页')],
        nextCursor: 'owner-next',
      })
      .mockResolvedValueOnce({
        items: [experimentListItem('owned-second', '我的归档第二页')],
        nextCursor: null,
      });
    vi.mocked(fetchAdminArchivedExperiments)
      .mockResolvedValueOnce({
        items: [{
          experimentId: 'admin-first',
          name: '待清理第一页',
          currentRevisionId: 'admin-revision-1',
          status: 'completed',
          snapshotVersion: 3,
          archivedAt: '2026-08-01T00:00:00Z',
          retentionUntil: '2026-09-01T00:00:00Z',
          purgeJobId: null,
          updatedAt: '2026-10-02T00:00:00Z',
        }],
        nextCursor: 'admin-next',
      })
      .mockResolvedValueOnce({
        items: [{
          experimentId: 'admin-second',
          name: '待清理第二页',
          currentRevisionId: 'admin-revision-2',
          status: 'completed',
          snapshotVersion: 4,
          archivedAt: '2026-08-02T00:00:00Z',
          retentionUntil: '2026-09-02T00:00:00Z',
          purgeJobId: null,
          updatedAt: '2026-10-01T00:00:00Z',
        }],
        nextCursor: null,
      });
    render(
      <ExperimentPage
        userId="admin-1"
        isAdmin
        subject={null}
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看已归档实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看全部归档实验' }));
    const drawer = await screen.findByRole('dialog', { name: '全部归档实验' });
    fireEvent.click(await within(drawer).findByRole('button', { name: '加载更多实验' }));
    expect(await screen.findByText('我的归档第二页')).not.toBeNull();
    expect(fetchExperiments).toHaveBeenLastCalledWith('owner-next', true, {
      limit: 20,
      query: '',
      status: '',
    });

    fireEvent.click(screen.getByRole('button', { name: '加载更多待清理实验' }));
    expect(await screen.findByText('待清理第二页')).not.toBeNull();
    expect(fetchAdminArchivedExperiments).toHaveBeenLastCalledWith('admin-next');
  });

  it('ignores a stale experiment list response after switching archive views', async () => {
    let rejectCurrent: (reason: Error) => void = () => {};
    vi.mocked(fetchExperiments)
      .mockReturnValueOnce(new Promise((_resolve, reject) => {
        rejectCurrent = reject;
      }))
      .mockResolvedValueOnce({ items: [], nextCursor: null });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看已归档实验' }));
    await waitFor(() => expect(fetchExperiments).toHaveBeenLastCalledWith(
      undefined, true, { limit: 5 },
    ));
    expect(screen.getByText('暂无可用的已发布标准 Bar 策略。请先在策略回测中完成保存与发布。')).not.toBeNull();

    await act(async () => rejectCurrent(new Error('stale list failed')));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(fetchExperiments).toHaveBeenCalledTimes(2);
  });

  it('keeps the landing list compact and filters the full experiment drawer on the server', async () => {
    vi.mocked(fetchExperiments)
      .mockResolvedValueOnce({
        items: [experimentListItem('recent-1', '最近实验一')],
        nextCursor: 'recent-next',
      })
      .mockResolvedValueOnce({
        items: [experimentListItem('all-1', '全部实验一')],
        nextCursor: null,
      })
      .mockResolvedValueOnce({
        items: [experimentListItem('search-1', 'Alpha 参数研究')],
        nextCursor: null,
      })
      .mockResolvedValueOnce({
        items: [experimentListItem('failed-1', 'Alpha 失败实验')],
        nextCursor: null,
      });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    expect(await screen.findByText('最近实验一')).not.toBeNull();
    expect(fetchExperiments).toHaveBeenNthCalledWith(
      1, undefined, false, { limit: 5 },
    );
    expect(screen.queryByRole('button', { name: '加载更多我的实验' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看全部实验' }));
    const drawer = await screen.findByRole('dialog', { name: '全部实验' });
    await within(drawer).findByText('全部实验一');

    fireEvent.change(within(drawer).getByLabelText('搜索实验'), {
      target: { value: '  Alpha  ' },
    });
    fireEvent.click(within(drawer).getByRole('button', { name: '搜索' }));
    await waitFor(() => expect(fetchExperiments).toHaveBeenLastCalledWith(
      undefined, false, { limit: 20, query: 'Alpha', status: '' },
    ));
    expect(await within(drawer).findByText('Alpha 参数研究')).not.toBeNull();

    fireEvent.change(within(drawer).getByLabelText('筛选实验状态'), {
      target: { value: 'failed' },
    });
    await waitFor(() => expect(fetchExperiments).toHaveBeenLastCalledWith(
      undefined, false, { limit: 20, query: 'Alpha', status: 'failed' },
    ));
    expect(await within(drawer).findByText('Alpha 失败实验')).not.toBeNull();
  });

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

  it('shows localized labels for built-in execution and resource values', async () => {
    render(
      <ExperimentPage
        userId="user-1"
        subject={SUBJECT_REF}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );
    await screen.findByRole('heading', { name: '均值回归' });

    expect(screen.getByText('立即成交')).not.toBeNull();
    expect(screen.queryByText('immediate')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));

    expect(await screen.findByText('轻量')).not.toBeNull();
    expect(screen.queryByText('light')).toBeNull();
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

  it('shows execution progress and the experiment-wide backtest task index', async () => {
    const onOpenRun = vi.fn();
    const onOpenBacktestDetail = vi.fn();
    const { container } = render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
        onOpenRun={onOpenRun}
        onOpenBacktestDetail={onOpenBacktestDetail}
      />,
    );

    await screen.findByText('2 / 2');
    expect(screen.getByText('冻结定义')).not.toBeNull();
    expect(screen.getAllByText('排队中').length).toBeGreaterThan(0);
    expect(screen.queryByText('queued')).toBeNull();
    expect(screen.getByText('Bar 下一根开盘价')).not.toBeNull();
    expect(screen.getByText('开发 · 2026-07-01 至 2026-07-02')).not.toBeNull();
    expect((screen.getByText('技术信息与编号').closest('details') as HTMLDetailsElement).open).toBe(false);
    expect((screen.getByText('执行记录（1）').closest('details') as HTMLDetailsElement).open).toBe(false);
    const parameters = screen.getByText('查看参数').closest('details') as HTMLDetailsElement;
    expect(parameters.open).toBe(false);
    fireEvent.click(within(parameters).getByText('查看参数'));
    expect(parameters.open).toBe(true);
    expect(within(parameters).getByText('window')).not.toBeNull();
    expect(within(parameters).getByText('10')).not.toBeNull();
    expect(within(parameters).getByText('threshold')).not.toBeNull();
    expect(within(parameters).getByText('0.5')).not.toBeNull();
    expect(within(parameters).getByText('基础成本')).not.toBeNull();
    expect(within(parameters).getByText('立即成交（默认）')).not.toBeNull();
    const kpis = container.querySelector('.experiment-detail-kpis') as HTMLElement;
    expect(kpis).not.toBeNull();
    expect(within(kpis).getByText('取消')).not.toBeNull();
    expect(within(kpis).queryByText('日期场景')).toBeNull();
    expect(container.querySelector('.experiment-detail-workspace')).not.toBeNull();
    expect(screen.queryByRole('button', { name: '查看单次结果' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /关联回测任务/ }));
    const drawer = await screen.findByRole('dialog', { name: '关联回测任务' });
    expect(within(drawer).getByText('#187')).not.toBeNull();
    expect(within(drawer).getByText('#193')).not.toBeNull();
    expect(within(drawer).getByText('人工重试')).not.toBeNull();
    expect(within(drawer).queryByText('选定结果')).toBeNull();
    expect(within(drawer).queryByText('查看信号图表')).toBeNull();
    fireEvent.click(within(drawer).getByRole('button', { name: '查看回测 #193 详情' }));
    expect(onOpenBacktestDetail).toHaveBeenCalledWith('run-2');
    expect(onOpenRun).not.toHaveBeenCalled();
    expect(fetchExperimentBacktestRuns).toHaveBeenCalledWith('experiment-1', undefined, '');
    const runSearch = within(drawer).getByRole('textbox', { name: '筛选回测任务编号' });
    expect(runSearch.getAttribute('placeholder')).toBe('输入回测任务编号，例如 187');
    fireEvent.change(runSearch, { target: { value: '#193' } });
    expect((runSearch as HTMLInputElement).value).toBe('193');
    fireEvent.click(within(drawer).getByRole('button', { name: '筛选' }));
    await waitFor(() => {
      expect(fetchExperimentBacktestRuns).toHaveBeenCalledWith('experiment-1', undefined, '193');
    });
    expect(fetchExperimentDetail).toHaveBeenCalledTimes(1);
    expect(fetchExperimentTrials).toHaveBeenCalledWith('experiment-1');
    expect(screen.getByText('执行 #1')).not.toBeNull();
    expect(screen.queryByText('选定结果')).toBeNull();
  });

  it('兼容缺少关联回测任务摘要的实验详情', async () => {
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      relatedBacktestRuns: undefined,
    } as unknown as ExperimentDetail);

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

    await screen.findByText('2 / 2');
    const relatedRunsButton = screen.getByRole('button', { name: /关联回测任务/ });
    expect((relatedRunsButton as HTMLButtonElement).disabled).toBe(true);
    expect(relatedRunsButton.textContent).toContain('0 个');
  });

  it('freezes and confirms failed Trial selection before one manual retry', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const failedTrial: ExperimentTrial = {
      ...TRIAL,
      summaryStatus: 'failed',
      selectedExecution: null,
      errorSummary: { code: 'runtime.failed', message: '执行失败' },
      executionAttempts: [{
        ...TRIAL.executionAttempts[0],
        status: 'failed',
        terminalStatus: 'failed',
        selected: false,
        resultHash: null,
        errorSummary: { code: 'runtime.failed', message: '执行失败' },
      }],
    };
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      status: 'failed',
      stage: {
        ...DETAIL.stage,
        terminalTrialCount: 2,
        failedTrialCount: 1,
        queuedTrialCount: 0,
        status: 'failed',
      },
    });
    vi.mocked(fetchExperimentTrials).mockResolvedValue({
      items: [failedTrial],
      nextCursor: null,
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

    const checkbox = await screen.findByRole('checkbox', { name: '选择 Trial #1' });
    fireEvent.click(checkbox);
    const retryButton = screen.getByRole('button', { name: '重试选中失败项（1）' });
    fireEvent.click(retryButton);
    fireEvent.click(retryButton);

    await waitFor(() => expect(retryFailedTrials).toHaveBeenCalledTimes(1));
    expect(previewRetryFailedTrials).toHaveBeenCalledWith(
      'experiment-1', 'revision-1', ['trial-1'],
    );
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('保留旧执行'));
  });

  it('copies the frozen inputs into an independent experiment and shows the change', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(createExperiment).mockResolvedValueOnce({
      ...CREATED,
      experimentId: 'experiment-copy-1',
    });
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      status: 'completed',
      stage: {
        ...DETAIL.stage,
        status: 'completed',
        terminalTrialCount: 2,
        succeededTrialCount: 2,
        queuedTrialCount: 0,
      },
    });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={onExperimentChange}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '复制实验并调整' }));
    await screen.findByRole('heading', { name: '均值回归' });
    expect(screen.getByDisplayValue('10,20')).not.toBeNull();
    expect(screen.getByText('运行配置尚未变化。修改参数候选、日期、资金或滑点后，差异会显示在这里。')).not.toBeNull();
    fireEvent.change(screen.getByDisplayValue('10,20'), { target: { value: '10,20,30' } });
    expect(screen.getByText('10,20,30')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '生成预览' }));
    await screen.findByText('Trial 总数');
    fireEvent.click(screen.getByRole('button', { name: '创建新实验并开始' }));

    await waitFor(() => expect(createExperiment).toHaveBeenCalledTimes(1));
    expect(createExperiment).toHaveBeenCalledWith(
      expect.objectContaining({
        source: { experimentId: 'experiment-1', revisionId: 'revision-1' },
        experimentSpec: expect.objectContaining({
          plan: expect.objectContaining({
            stages: [expect.objectContaining({
              parameterGrid: [{
                inputId: 'strategyParameters.window',
                values: ['10', '20', '30'],
              }],
            })],
          }),
        }),
      }),
      expect.any(String),
    );
    expect(onExperimentChange).toHaveBeenCalledWith('experiment-copy-1');
  });

  it('keeps execution and evaluation in the main workspace with compact lifecycle actions', async () => {
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      status: 'completed',
      stage: {
        ...DETAIL.stage,
        status: 'completed',
        terminalTrialCount: 2,
        succeededTrialCount: 2,
        queuedTrialCount: 0,
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

    const trialHeading = await screen.findByRole('heading', { name: 'Trial 运行' });
    expect(trialHeading.closest('.experiment-detail-main')).not.toBeNull();
    expect(screen.getByRole('heading', { name: '冻结定义' }).closest(
      '.experiment-detail-sidebar',
    )).not.toBeNull();
    expect(screen.getByRole('heading', { name: '修订历史' }).closest(
      '.experiment-detail-sidebar',
    )).not.toBeNull();
    expect(screen.getByRole('button', { name: '复制实验并调整' }).closest(
      '.experiment-detail-actions',
    )).not.toBeNull();
    expect(screen.getByRole('button', { name: '归档实验' }).closest(
      '.experiment-detail-actions',
    )).not.toBeNull();
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

  it('archives a terminal experiment only after explicit confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onExperimentChange = vi.fn();
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      status: 'completed',
      archivedAt: null,
      retentionUntil: null,
      purgeJobId: null,
      stage: {
        ...DETAIL.stage,
        status: 'completed',
        terminalTrialCount: 2,
        succeededTrialCount: 2,
        queuedTrialCount: 0,
      },
    });
    vi.mocked(archiveExperiment).mockResolvedValue({
      experimentId: 'experiment-1',
      revisionId: 'revision-1',
      status: 'completed',
      snapshotVersion: 3,
      archivedAt: '2026-10-08T00:00:00Z',
      retentionUntil: '2026-11-07T00:00:00Z',
      reason: '归档',
      operationId: 'archive-operation-1',
      replayed: false,
      acceptedAt: '2026-10-08T00:00:00Z',
    });
    render(
      <ExperimentPage
        userId="user-1"
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={onExperimentChange}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    const button = await screen.findByRole('button', { name: '归档实验' });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(archiveExperiment).toHaveBeenCalledTimes(1));
    expect(archiveExperiment).toHaveBeenCalledWith(
      'experiment-1', 'revision-1', 2,
      '用户在实验详情页确认归档终态实验', expect.any(String),
    );
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('不会删除'));
    expect(onExperimentChange).toHaveBeenCalledWith(null);
  });

  it('reuses the archive idempotency key after an uncertain network failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(fetchExperimentDetail).mockResolvedValue({
      ...DETAIL,
      status: 'completed',
    });
    vi.mocked(archiveExperiment)
      .mockRejectedValueOnce(new TypeError('network failed'))
      .mockResolvedValue({
        experimentId: 'experiment-1',
        revisionId: 'revision-1',
        status: 'completed',
        snapshotVersion: 3,
        archivedAt: '2026-10-08T00:00:00Z',
        retentionUntil: '2026-11-07T00:00:00Z',
        reason: '归档',
        operationId: 'archive-operation-1',
        replayed: true,
        acceptedAt: '2026-10-08T00:00:00Z',
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

    fireEvent.click(await screen.findByRole('button', { name: '归档实验' }));

    await waitFor(() => expect(archiveExperiment).toHaveBeenCalledTimes(2));
    expect(vi.mocked(archiveExperiment).mock.calls[0][4]).toBe(
      vi.mocked(archiveExperiment).mock.calls[1][4],
    );
  });

  it('restores an archived experiment without changing its revision identity', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const archived = {
      ...DETAIL,
      status: 'completed' as const,
      archivedAt: '2026-10-08T00:00:00Z',
      retentionUntil: '2026-11-07T00:00:00Z',
      purgeJobId: null,
    };
    vi.mocked(fetchExperimentDetail).mockResolvedValueOnce(archived).mockResolvedValue(DETAIL);
    vi.mocked(restoreArchivedExperiment).mockResolvedValue({
      experimentId: 'experiment-1',
      revisionId: 'revision-1',
      status: 'completed',
      snapshotVersion: 3,
      archivedAt: null,
      retentionUntil: null,
      reason: '恢复归档',
      operationId: 'restore-operation-1',
      replayed: false,
      acceptedAt: '2026-10-09T00:00:00Z',
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

    fireEvent.click(await screen.findByRole('button', { name: '恢复归档实验' }));

    await waitFor(() => expect(restoreArchivedExperiment).toHaveBeenCalledWith(
      'experiment-1', 'revision-1', 2,
      '用户在实验详情页确认恢复归档实验', expect.any(String),
    ));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Revision 身份保持不变'));
  });

  it('requires an admin reason and confirmation before requesting purge', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('用户申请数据清理');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(fetchExperimentDetail).mockResolvedValueOnce({
      ...DETAIL,
      status: 'completed',
      archivedAt: '2026-09-01T00:00:00Z',
      retentionUntil: '2026-09-30T00:00:00Z',
      purgeJobId: null,
    }).mockResolvedValue(DETAIL);
    vi.mocked(requestExperimentPurge).mockResolvedValue({
      purgeId: 'purge-1',
      experimentId: 'experiment-1',
      revisionId: 'revision-1',
      status: 'queued',
      phase: 'artifacts',
      scopeDigest: 'a'.repeat(64),
      scopeCounts: { experiment: 1 },
      snapshotVersion: 3,
      reason: '用户申请数据清理',
      operationId: 'purge-1',
      replayed: false,
      acceptedAt: '2026-10-08T00:00:00Z',
    });
    render(
      <ExperimentPage
        userId="admin-1"
        isAdmin
        subject={null}
        experimentId="experiment-1"
        onExperimentChange={vi.fn()}
        onSubjectChange={vi.fn()}
        onBackToStrategy={vi.fn()}
      />,
    );

    const button = await screen.findByRole('button', { name: '物理清理实验' });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(requestExperimentPurge).toHaveBeenCalledTimes(1));
    expect(requestExperimentPurge).toHaveBeenCalledWith(
      'experiment-1', 'revision-1', 2, '用户申请数据清理', expect.any(String),
    );
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('不可逆'));
  });
});
