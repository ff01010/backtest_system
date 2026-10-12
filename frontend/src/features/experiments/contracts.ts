export type ExperimentSubjectRef = {
  subjectType: 'legacy_single_strategy';
  subjectId: string;
  subjectVersion: string;
};

export type TunableInput = {
  inputId: string;
  sourceName: string;
  type: 'boolean' | 'integer' | 'number' | 'string';
  group: 'strategyParameters';
  defaultValue: unknown;
  overridePolicy: 'finite_candidates';
};

export type ExperimentSubject = {
  subjectRef: ExperimentSubjectRef;
  displayName: string;
  templateType: string;
  runTemplateDigest: string;
  fixedStructure: Record<string, unknown>;
  tunableInputCatalog: {
    schemaVersion: string;
    inputs: TunableInput[];
    scenarioEditors: Array<Record<string, unknown>>;
  };
  assetManifestSummary: Record<string, unknown>;
  contracts: Record<string, unknown>;
  defaultExperimentInputs: {
    strategyParameters: Record<string, unknown>;
    dateScenario: DateScenarioDraft;
    initialCapital: string;
    slippage: string;
  };
  capability: Record<string, unknown>;
};

export type DateScenarioDraft = {
  scenarioId: string;
  name: string;
  role: 'development' | 'validation' | 'reference';
  startDate: string;
  endDate: string;
};

export type ExperimentDraft = {
  schemaVersion: 'experiment_draft_v1';
  name: string;
  description: string;
  parameterValues: Record<string, string>;
  dateScenarios: DateScenarioDraft[];
  capitalValues: string;
  slippageValues: string;
};

export type ExperimentPreviewRequest = {
  schemaVersion: 'experiment_create_v1';
  name: string;
  description: string;
  subjectRef: ExperimentSubjectRef;
  plan: {
    planVersion: 'experiment_plan_v1';
    stages: [{
      stageType: 'finite_grid_v1';
      generatorVersion: '1';
      parameterGrid: Array<{ inputId: string; values: string[] }>;
      dateScenarios: DateScenarioDraft[];
      capitalScenarios: string[];
      costScenarios: Array<{ scenarioId: string; slippage: string }>;
      executionScenarios: Array<{
        scenarioId: string;
        algorithm: 'immediate';
        parameters: Record<string, never>;
      }>;
    }];
  };
};

export type ExperimentPreview = {
  previewToken: string;
  expiresAt: string;
  specHash: string;
  canonicalizationVersion: string;
  runTemplateDigest: string;
  datasetSnapshotDigest: string;
  capabilityDigest: string;
  budgetDigest: string;
  budgetVersion: string;
  normalizedSpec: Record<string, unknown>;
  plannedTrialCount: number;
  dimensionCounts: Record<string, number>;
  dimensionNormalization: Record<string, { submitted: number; normalized: number }>;
  resourceEstimate: {
    estimatedBarCount: number;
    maxBarsPerTrial: number;
    resourceClass: string;
    confidence: string;
  };
  limitDecision: {
    allowed: boolean;
    warnings: Array<{ code: string; message: string }>;
    blockingReasons: Array<{ code: string; message: string } & Record<string, unknown>>;
  };
  examples: Array<{
    trialIndex: number;
    trialFingerprint: string;
    seedBundle: { derivationVersion: string; baseSeed: number };
    dimensionAssignment: Record<string, unknown>;
    compiledRequestDigest: string;
  }>;
};

export type ExperimentCreateRequest = {
  previewToken: string;
  specHash: string;
  experimentSpec: ExperimentPreviewRequest;
  source?: {
    experimentId: string;
    revisionId: string;
  };
};

export type ExperimentCreateResult = {
  experimentId: string;
  revisionId: string;
  stageId: string;
  status: 'preparing';
  plannedTrialCount: number;
  generatedTrialCount: number;
  specHash: string;
  replayed: boolean;
  createdAt: string;
};

export type ExperimentCreateResponse = ExperimentCreateResult | {
  status: 'processing';
  replayed: true;
};

export type ExperimentStatus =
  | 'preparing'
  | 'queued'
  | 'running'
  | 'completed'
  | 'partially_completed'
  | 'failed'
  | 'cancelling'
  | 'cancelled'
  | 'generation_blocked';

export type ExperimentListItem = {
  experimentId: string;
  name: string;
  source: ExperimentSourceSummary | null;
  subject: ExperimentSubjectRef & { name: string };
  currentRevisionId: string;
  revisionNo: number;
  status: ExperimentStatus;
  reason: { code: string; message: string } | null;
  runPath: string;
  scopeType: string;
  symbol: string;
  timeframe: string;
  fillModel: string;
  plannedTrialCount: number;
  generatedTrialCount: number;
  materializedTrialCount: number;
  queuedTrialCount: number;
  runningTrialCount: number;
  succeededTrialCount: number;
  failedTrialCount: number;
  cancelledTrialCount: number;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
  retentionUntil: string | null;
  purgeJobId: string | null;
};

export type AdminArchivedExperimentItem = {
  experimentId: string;
  name: string;
  currentRevisionId: string;
  status: ExperimentStatus;
  snapshotVersion: number;
  archivedAt: string;
  retentionUntil: string | null;
  purgeJobId: string | null;
  updatedAt: string;
};

export type ExperimentLifecycleResult = {
  experimentId: string;
  revisionId: string;
  status: ExperimentStatus;
  snapshotVersion: number;
  archivedAt: string | null;
  retentionUntil: string | null;
  reason: string;
  operationId: string;
  replayed: boolean;
  acceptedAt: string;
};

export type ExperimentPurge = {
  purgeId: string;
  experimentId: string;
  revisionId: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  phase: string;
  scopeDigest: string;
  scopeCounts: Record<string, number>;
  snapshotVersion: number;
  reason: string;
  operationId: string;
  replayed: boolean;
  acceptedAt: string;
};

export type ExperimentTrial = {
  trialId: string;
  trialIndex: number;
  stageId: string;
  candidateKey: string;
  dimensionAssignment: Record<string, unknown>;
  materializationStatus: 'pending' | 'materialized' | 'cancelled_before_materialization';
  summaryStatus: 'pending' | 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled';
  automaticRetryCount: number;
  retryNotBefore: string | null;
  activeExecution: { executionId: string; runId: string; status: string } | null;
  selectedExecution: {
    executionId: string;
    runId: string;
    status: string;
    resultHash: string;
    resultSchemaVersion: string;
    actualDataHash: string;
  } | null;
  errorSummary: Record<string, unknown> | null;
  executionAttempts: Array<{
    executionId: string;
    executionNo: number;
    runId: string;
    status: string;
    terminalStatus: 'succeeded' | 'failed' | 'cancelled' | null;
    createdReason: 'initial' | 'automatic_retry' | 'manual_retry';
    selected: boolean;
    active: boolean;
    resultHash: string | null;
    errorSummary: Record<string, unknown> | null;
    queuedAt: string;
    startedAt: string | null;
    finishedAt: string | null;
  }>;
  runId: string | null;
  runUrl: string | null;
  executionId: string | null;
};

export type ExperimentRevision = {
  revisionId: string;
  revisionNo: number;
  current: boolean;
  status: ExperimentStatus;
  specHash: string;
  stage: {
    stageId: string;
    status: string;
    plannedTrialCount: number;
    generatedTrialCount: number;
    inFlightTrialCount: number;
    terminalTrialCount: number;
    succeededTrialCount: number;
    failedTrialCount: number;
    cancelledTrialCount: number;
  };
  createdAt: string;
  updatedAt: string;
  terminalAt: string | null;
};

export type ExperimentRetrySelection = {
  experimentId: string;
  revisionId: string;
  selectedTrialCount: number;
  selectionDigest: string;
  trials: Array<{
    trialId: string;
    trialIndex: number;
    previousExecutionId: string;
    previousExecutionNo: number;
    errorSummary: Record<string, unknown> | null;
  }>;
};

export type ExperimentRetryResult = {
  experimentId: string;
  revisionId: string;
  status: 'running';
  operationId: string;
  selectedTrialCount: number;
  selectionDigest: string;
  executions: Array<{
    executionId: string;
    executionNo: number;
    runId: string;
    dispatchKey: string;
    createdReason: 'manual_retry';
  }>;
  replayed: boolean;
  acceptedAt: string;
};

export type ExperimentDetail = {
  experimentId: string;
  name: string;
  description: string;
  source: ExperimentSourceSummary | null;
  status: ExperimentStatus;
  snapshotVersion: number;
  relatedBacktestRuns?: {
    totalCount: number;
    initialCount: number;
    retryCount: number;
  };
  revision: {
    revisionId: string;
    revisionNo: number;
    specHash: string;
  };
  stage: {
    stageId: string;
    stageType: string;
    generatorVersion: string;
    status: string;
    plannedTrialCount: number;
    generatedTrialCount: number;
    generationCursor: number;
    trialCollectionDigest: string | null;
    inFlightTrialCount: number;
    terminalTrialCount: number;
    succeededTrialCount: number;
    failedTrialCount: number;
    cancelledTrialCount: number;
    queuedTrialCount: number;
  };
  resourceBudget: Record<string, unknown>;
  failurePolicy: Record<string, unknown>;
  errorAggregates: Array<{
    fingerprint: string;
    category: string;
    code: string;
    message: string | null;
    failureStage: string;
    component: string;
    retryable: boolean;
    count: number;
    lastTerminalSequence: number;
  }>;
  consecutiveFailureCount: number;
  frozenDefinition: {
    subjectRef: ExperimentSubjectRef;
    fixedStructure: Record<string, unknown>;
    plan: Record<string, unknown>;
    datasetSnapshots: Array<Record<string, unknown>>;
    determinism: Record<string, unknown>;
  };
  blockedReason: {
    code: string;
    message: string;
    scope: 'stage' | 'experiment' | null;
    blockedAt: string | null;
  } | null;
  archivedAt: string | null;
  retentionUntil: string | null;
  purgeJobId: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ExperimentBacktestRun = {
  revisionId: string;
  revisionNo: number;
  currentRevision: boolean;
  stageId: string;
  stageIndex: number;
  trialId: string;
  trialIndex: number;
  executionId: string;
  executionNo: number;
  createdReason: 'initial' | 'automatic_retry' | 'manual_retry';
  selected: boolean;
  runId: string;
  displayNo: number;
  status: string;
};

export type ExperimentBacktestRunPage = {
  totalCount: number;
  initialCount: number;
  retryCount: number;
  items: ExperimentBacktestRun[];
  nextCursor: string | null;
};

export type ExperimentSourceSummary = {
  experimentId: string;
  experimentName: string;
  revisionId: string;
  revisionNo: number;
};

export type EvaluationMetricDefinition = {
  metricId: string;
  displayName: string;
  dataType: 'decimal' | 'integer' | 'string';
  unit: string | null;
  direction: 'higher' | 'lower' | 'neutral';
  grain: string;
  currency: string | null;
  windowSemantics: string;
  annualization: string;
  benchmarkSemantics: string;
  comparabilityKeyVersion: string;
  nullSemantics: string;
};

export type EvaluationDimensionDefinition = {
  dimensionId: string;
  displayName: string;
  dataType: string;
  unit: string | null;
  nullSemantics: string;
  orderedValues?: unknown[] | null;
};

export type EvaluationCatalogs = {
  experimentId: string;
  experimentRevisionId: string;
  stages: Array<{
    stageId: string;
    stageIndex: number;
    stageType: string;
    status: string;
    plannedTrialCount: number;
  }>;
  metricCatalog: {
    version: 'metric_catalog_v1';
    metrics: EvaluationMetricDefinition[];
  };
  dimensionCatalog: {
    version: 'dimension_catalog_v1';
    dimensions: EvaluationDimensionDefinition[];
  };
  pipelineVersion: string;
  adapter: { type: string; version: string };
  normalizers?: Array<{
    version: 'candidate_rate_distribution_v1';
    name: string;
    supportedUnits: string[];
  }>;
};

export type EvaluationFilter = {
  type: 'metric_comparison_v1';
  metricId: string;
  operator: '>' | '>=' | '<' | '<=' | '=' | '!=' | 'is_null' | 'is_not_null';
  value?: string | null;
};

export type EvaluationSort = {
  type: 'metric_sort_v1';
  metricId: string;
  direction: 'asc' | 'desc';
};

export type EvaluationFormula = {
  type: 'row_formula_v1' | 'group_formula_v1';
  metricId: string;
  displayName: string;
  formula: string;
  precision: number;
  unit?: string | null;
  direction?: 'higher' | 'lower' | 'neutral';
};

export type EvaluationAggregate = {
  type: 'metric_aggregate_v1';
  aggregateId: string;
  metricId: string;
  function: 'count' | 'mean' | 'median' | 'min' | 'max' | 'sample_stddev' | 'nearest_rank_percentile';
  percentile?: string | null;
};

export type EvaluationVisualization =
  | { type: 'parameter_curve_v1'; parameterDimensionId: string; metricId: string; seriesBy: string[] }
  | { type: 'parameter_heatmap_v1'; xDimensionId: string; yDimensionId: string; metricId: string; facetBy: string[] }
  | { type: 'cost_stress_v1'; metricId: string; baselineScenarioId: string; degradationThreshold?: string | null }
  | { type: 'time_stability_v1'; metricId: string };

export type EvaluationSpecPayload = {
  schemaVersion: 'evaluation_spec_v1';
  name: string;
  source: {
    experimentRevisionId: string;
    stageIds: string[];
    executionSelection: 'selected_execution_at_run_creation';
  };
  catalogs: {
    metricCatalogVersion: 'metric_catalog_v1';
    dimensionCatalogVersion: 'dimension_catalog_v1';
    normalizerVersion: 'candidate_rate_distribution_v1' | null;
  };
  completeness: {
    allowPartial: boolean;
    minimumCompletionRate: string;
    requiredDimensionCoverage: Array<{
      dimensionId: string;
      minimumCoverageRate: string;
    }>;
  };
  columns: string[];
  derivedMetrics: EvaluationFormula[];
  filters: EvaluationFilter[];
  groupBy: string[];
  aggregates: EvaluationAggregate[];
  groupFilters: Array<Omit<EvaluationFilter, 'type'> & { type: 'group_metric_comparison_v1' }>;
  sort: EvaluationSort[];
  visualizations: EvaluationVisualization[];
  reportSections: Array<'completeness' | 'ranking' | 'groups' | 'analysis' | 'exceptions' | 'dataset_notice'>;
};

export type SavedEvaluationSpec = {
  evaluationSpecId: string;
  experimentId: string;
  revisionId: string;
  versionNo: number;
  name: string;
  specHash: string;
  spec: EvaluationSpecPayload;
  metricCatalogVersion: string;
  dimensionCatalogVersion: string;
  normalizerVersion: string | null;
  createdAt: string;
  replayed: boolean;
};

export type EvaluationRun = {
  evaluationRunId: string;
  evaluationSpecId: string;
  experimentId: string;
  revisionId: string;
  specVersionNo: number | null;
  specName: string | null;
  specHash?: string | null;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  pipelineVersion: string;
  adapterType: string;
  adapterVersion: string;
  metricCatalogVersion: string;
  dimensionCatalogVersion: string;
  executionCount: number;
  completeness: EvaluationCompletenessSnapshot;
  resultSetHash: string;
  outputHash: string | null;
  error: { code: string; message: string } | null;
  attemptCount: number;
  createdAt: string;
  finishedAt: string | null;
  replayed?: boolean;
};

export type EvaluationRunDetail = EvaluationRun & {
  executionSet: Array<{
    stageId: string;
    stageIndex: number;
    stageType: string;
    trialId: string;
    trialIndex: number;
    trialExecutionId: string;
    runId: string;
    resultHash: string;
    resultSchemaVersion: string;
    actualDataHash: string | null;
  }>;
  output: null | {
    schemaVersion: string;
    pipelineVersion: string;
    resultSetHash: string;
    sourceRowCount: number;
    matchedRowCount: number;
    isPartial?: boolean;
    reportStatus?: 'complete' | 'provisional';
    candidateSelectionEligible?: boolean;
    finalTestEligible?: false;
    datasetNotice?: string;
    completeness?: EvaluationCompletenessSnapshot;
    columns: string[];
    sort: EvaluationSort[];
    groupBy?: string[];
    rows: Array<{
      trialId?: string;
      trialExecutionId?: string;
      runId?: string;
      stageId?: string;
      stageIndex: number;
      trialIndex: number;
      resultHash?: string;
      startDate?: string | null;
      groupKey?: string;
      memberCount?: number;
      members?: Array<{
        trialId: string;
        trialExecutionId: string;
        runId: string;
        stageId: string;
        stageIndex: number;
        trialIndex: number;
        resultHash: string;
        startDate?: string | null;
      }>;
      values: Record<string, string | number | null>;
      unsupportedReasons: Record<string, string>;
    }>;
    visualizations?: Array<Record<string, unknown>>;
  };
  spec: EvaluationSpecPayload;
};

export type EvaluationCompletenessSnapshot = {
  plannedTrialCount: number;
  generatedTrialCount: number;
  succeededCount: number;
  failedCount: number;
  cancelledCount: number;
  missingCount: number;
  completionRate: string;
  isPartial: boolean;
  dimensionCoverage: Array<{
    dimensionId: string;
    requiredRate: string;
    minimumObservedRate: string;
    values: Array<{
      value: unknown;
      expectedCount: number;
      succeededCount: number;
      coverageRate: string;
    }>;
  }>;
  exceptions: Array<{
    stageId: string;
    stageIndex: number;
    trialId: string | null;
    trialIndex: number;
    status: 'failed' | 'cancelled' | 'missing';
    materializationStatus: string;
    error: Record<string, unknown> | null;
  }>;
};

export type EvaluationExportFormat = 'csv' | 'xlsx' | 'report';

export type EvaluationExport = {
  exportId: string;
  evaluationRunId: string;
  evaluationSpecId: string;
  experimentId: string;
  evaluationName: string | null;
  format: EvaluationExportFormat;
  columns: string[];
  locale: 'zh-CN' | 'en-US';
  outputHash: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancel_requested' | 'cancelled' | 'expired';
  progress: { completed: number; total: number };
  attemptCount: number;
  artifact: null | {
    filename: string;
    mediaType: string;
    byteSize: number;
    sha256: string;
  };
  error: null | { code: string; message: string };
  retryOfExportId: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  expiresAt: string | null;
  replayed?: boolean;
};
