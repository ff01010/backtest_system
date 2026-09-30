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
};

export type ExperimentTrial = {
  trialId: string;
  trialIndex: number;
  stageId: string;
  candidateKey: string;
  dimensionAssignment: Record<string, unknown>;
  materializationStatus: 'pending' | 'materialized' | 'cancelled_before_materialization';
  summaryStatus: 'pending' | 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled';
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
  runId: string | null;
  runUrl: string | null;
  executionId: string | null;
};

export type ExperimentDetail = {
  experimentId: string;
  name: string;
  description: string;
  status: ExperimentStatus;
  snapshotVersion: number;
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
  frozenDefinition: {
    subjectRef: ExperimentSubjectRef;
    fixedStructure: Record<string, unknown>;
    plan: Record<string, unknown>;
    datasetSnapshots: Array<Record<string, unknown>>;
    determinism: Record<string, unknown>;
  };
  blockedReason: { code: string; message: string } | null;
  createdAt: string;
  updatedAt: string;
};
