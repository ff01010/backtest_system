import { beforeEach, describe, expect, it } from 'vitest';
import type { ExperimentSubject, ExperimentSubjectRef } from '../features/experiments/contracts';
import {
  buildPreviewRequest,
  createExperimentDraft,
  experimentDraftKey,
  loadExperimentDraft,
  saveExperimentDraft,
} from '../features/experiments/draftStore';

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
  fixedStructure: { symbol: '688981.SH', timeframe: '5m' },
  tunableInputCatalog: {
    schemaVersion: 'tunable_input_catalog_v1',
    inputs: [],
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

beforeEach(() => localStorage.clear());

describe('experiment local draft', () => {
  it('isolates drafts by user, subject and published version', () => {
    expect(experimentDraftKey('user-1', SUBJECT_REF)).not.toBe(experimentDraftKey('user-2', SUBJECT_REF));
    expect(experimentDraftKey('user-1', SUBJECT_REF)).not.toBe(experimentDraftKey('user-1', {
      ...SUBJECT_REF,
      subjectVersion: 'version-2',
    }));
  });

  it('restores only the current draft schema and never restores a preview token', () => {
    const draft = createExperimentDraft(SUBJECT);
    saveExperimentDraft(localStorage, 'user-1', SUBJECT_REF, draft);

    expect(loadExperimentDraft(localStorage, 'user-1', SUBJECT_REF)).toEqual(draft);
    expect(localStorage.getItem(experimentDraftKey('user-1', SUBJECT_REF))).not.toContain('previewToken');

    localStorage.setItem(experimentDraftKey('user-1', SUBJECT_REF), JSON.stringify({
      ...draft,
      schemaVersion: 'experiment_draft_v0',
    }));
    expect(loadExperimentDraft(localStorage, 'user-1', SUBJECT_REF)).toBeNull();
  });
});

describe('experiment preview request', () => {
  it('submits only finite candidate inputs and never submits trusted counts or trial arrays', () => {
    const draft = {
      ...createExperimentDraft(SUBJECT),
      parameterValues: {
        'strategyParameters.window': '10, 20, 20',
        'strategyParameters.unused': '',
      },
      capitalValues: '100000, 200000',
      slippageValues: '0, 0.0001',
    };

    const request = buildPreviewRequest(SUBJECT_REF, draft);
    const stage = request.plan.stages[0];

    expect(stage.parameterGrid).toEqual([{
      inputId: 'strategyParameters.window',
      values: ['10', '20', '20'],
    }]);
    expect(stage.capitalScenarios).toEqual(['100000', '200000']);
    expect(stage.costScenarios).toHaveLength(2);
    expect(stage.executionScenarios).toEqual([{
      scenarioId: 'immediate-1',
      algorithm: 'immediate',
      parameters: {},
    }]);
    expect(stage).not.toHaveProperty('plannedTrialCount');
    expect(stage).not.toHaveProperty('trials');
  });

  it('rejects incomplete atomic inputs before sending', () => {
    const draft = { ...createExperimentDraft(SUBJECT), capitalValues: ' , ' };

    expect(() => buildPreviewRequest(SUBJECT_REF, draft)).toThrow('初始资金');

    expect(() => buildPreviewRequest(SUBJECT_REF, {
      ...createExperimentDraft(SUBJECT),
      dateScenarios: [{
        scenarioId: 'invalid',
        name: '非法区间',
        role: 'validation',
        startDate: '2026-07-03',
        endDate: '2026-07-02',
      }],
    })).toThrow('开始日期不能晚于结束日期');
  });
});
