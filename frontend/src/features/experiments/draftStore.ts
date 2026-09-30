import type {
  ExperimentDraft,
  ExperimentPreviewRequest,
  ExperimentSubject,
  ExperimentSubjectRef,
} from './contracts';

const DRAFT_SCHEMA_VERSION = 'experiment_draft_v1' as const;

export function experimentDraftKey(userId: string, subject: ExperimentSubjectRef): string {
  return [
    'trend_grid_experiment_draft_v1',
    userId,
    subject.subjectType,
    subject.subjectId,
    subject.subjectVersion,
  ].join(':');
}

export function createExperimentDraft(subject: ExperimentSubject): ExperimentDraft {
  return {
    schemaVersion: DRAFT_SCHEMA_VERSION,
    name: `${subject.displayName} · 有限网格`,
    description: '',
    parameterValues: {},
    dateScenarios: [{ ...subject.defaultExperimentInputs.dateScenario }],
    capitalValues: subject.defaultExperimentInputs.initialCapital,
    slippageValues: subject.defaultExperimentInputs.slippage,
  };
}

export function loadExperimentDraft(
  storage: Storage,
  userId: string,
  subject: ExperimentSubjectRef,
): ExperimentDraft | null {
  try {
    const raw = storage.getItem(experimentDraftKey(userId, subject));
    if (!raw) return null;
    const draft = JSON.parse(raw) as Partial<ExperimentDraft>;
    if (
      draft.schemaVersion !== DRAFT_SCHEMA_VERSION
      || typeof draft.name !== 'string'
      || typeof draft.description !== 'string'
      || !Array.isArray(draft.dateScenarios)
      || !draft.dateScenarios.every((item) => (
        item
        && typeof item.scenarioId === 'string'
        && typeof item.name === 'string'
        && typeof item.startDate === 'string'
        && typeof item.endDate === 'string'
      ))
      || draft.parameterValues === null
      || typeof draft.parameterValues !== 'object'
      || Array.isArray(draft.parameterValues)
      || typeof draft.capitalValues !== 'string'
      || typeof draft.slippageValues !== 'string'
    ) return null;
    return draft as ExperimentDraft;
  } catch {
    return null;
  }
}

export function saveExperimentDraft(
  storage: Storage,
  userId: string,
  subject: ExperimentSubjectRef,
  draft: ExperimentDraft,
): void {
  try {
    storage.setItem(experimentDraftKey(userId, subject), JSON.stringify(draft));
  } catch {
    // 本地存储不可用不应阻断服务端预览。
  }
}

export function clearExperimentDraft(
  storage: Storage,
  userId: string,
  subject: ExperimentSubjectRef,
): void {
  try {
    storage.removeItem(experimentDraftKey(userId, subject));
  } catch {
    // 清理失败不影响已经提交成功的实验。
  }
}

function candidateValues(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

export function buildPreviewRequest(
  subject: ExperimentSubjectRef,
  draft: ExperimentDraft,
): ExperimentPreviewRequest {
  const capitalScenarios = candidateValues(draft.capitalValues);
  const slippageValues = candidateValues(draft.slippageValues);
  if (!draft.name.trim()) throw new Error('请填写实验名称');
  if (draft.dateScenarios.length === 0) throw new Error('请至少添加一个日期场景');
  for (const scenario of draft.dateScenarios) {
    if (!scenario.scenarioId || !scenario.name.trim() || !scenario.startDate || !scenario.endDate) {
      throw new Error('请完整填写每个日期场景');
    }
    if (scenario.startDate > scenario.endDate) {
      throw new Error(`日期场景“${scenario.name}”的开始日期不能晚于结束日期`);
    }
  }
  if (capitalScenarios.length === 0) throw new Error('请至少填写一个初始资金候选值');
  if (slippageValues.length === 0) throw new Error('请至少填写一个滑点候选值');
  return {
    schemaVersion: 'experiment_create_v1',
    name: draft.name.trim(),
    description: draft.description,
    subjectRef: subject,
    plan: {
      planVersion: 'experiment_plan_v1',
      stages: [{
        stageType: 'finite_grid_v1',
        generatorVersion: '1',
        parameterGrid: Object.entries(draft.parameterValues)
          .map(([inputId, value]) => ({ inputId, values: candidateValues(value) }))
          .filter((item) => item.values.length > 0),
        dateScenarios: draft.dateScenarios,
        capitalScenarios,
        costScenarios: slippageValues.map((slippage, index) => ({
          scenarioId: `cost-${index + 1}`,
          slippage,
        })),
        executionScenarios: [{
          scenarioId: 'immediate-1',
          algorithm: 'immediate',
          parameters: {},
        }],
      }],
    },
  };
}
