import { fetchJson, sendJson } from '../../api/client';
import type {
  AdminArchivedExperimentItem,
  ExperimentPreview,
  ExperimentPreviewRequest,
  ExperimentCreateRequest,
  ExperimentCreateResponse,
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentListItem,
  ExperimentLifecycleResult,
  ExperimentPurge,
  ExperimentRetryResult,
  ExperimentRetrySelection,
  ExperimentRevision,
  ExperimentRevisionCreateRequest,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
  EvaluationCatalogs,
  EvaluationExport,
  EvaluationExportFormat,
  EvaluationRun,
  EvaluationRunDetail,
  EvaluationSpecPayload,
  SavedEvaluationSpec,
} from './contracts';

export function fetchExperimentSubject(subject: ExperimentSubjectRef): Promise<ExperimentSubject> {
  const type = encodeURIComponent(subject.subjectType);
  const id = encodeURIComponent(subject.subjectId);
  const version = encodeURIComponent(subject.subjectVersion);
  return fetchJson(`/api/backtest/experiment-subjects/${type}/${id}/versions/${version}`);
}

export function previewExperiment(request: ExperimentPreviewRequest): Promise<ExperimentPreview> {
  return sendJson('/api/backtest/experiments/preview', 'POST', request);
}

export function createExperiment(
  request: ExperimentCreateRequest,
  idempotencyKey: string,
): Promise<ExperimentCreateResponse> {
  return sendJson('/api/backtest/experiments', 'POST', request, {
    'Idempotency-Key': idempotencyKey,
  });
}

export function findCreatedExperiment(idempotencyKey: string): Promise<ExperimentCreateResponse> {
  return fetchJson(
    `/api/backtest/experiments/by-idempotency-key/${encodeURIComponent(idempotencyKey)}`,
  );
}

export function createExperimentRevision(
  experimentId: string,
  request: ExperimentRevisionCreateRequest,
  idempotencyKey: string,
): Promise<ExperimentCreateResponse> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/revisions`,
    'POST',
    request,
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchExperimentDetail(experimentId: string): Promise<ExperimentDetail> {
  return fetchJson(`/api/backtest/experiments/${encodeURIComponent(experimentId)}`);
}

export function fetchExperiments(
  cursor?: string,
  archived = false,
): Promise<{ items: ExperimentListItem[]; nextCursor: string | null }> {
  const query = new URLSearchParams();
  if (cursor) query.set('cursor', cursor);
  if (archived) query.set('archived', 'true');
  const suffix = query.size ? `?${query.toString()}` : '';
  return fetchJson(`/api/backtest/experiments${suffix}`);
}

export function fetchAdminArchivedExperiments(
  cursor?: string,
): Promise<{ items: AdminArchivedExperimentItem[]; nextCursor: string | null }> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return fetchJson(`/api/backtest/admin/experiments/archived${query}`);
}

export function archiveExperiment(
  experimentId: string,
  expectedRevisionId: string,
  expectedSnapshotVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ExperimentLifecycleResult> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/archive`,
    'POST',
    { expectedRevisionId, expectedSnapshotVersion, reason },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function restoreArchivedExperiment(
  experimentId: string,
  expectedRevisionId: string,
  expectedSnapshotVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ExperimentLifecycleResult> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/restore-archive`,
    'POST',
    { expectedRevisionId, expectedSnapshotVersion, reason },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function requestExperimentPurge(
  experimentId: string,
  expectedRevisionId: string,
  expectedSnapshotVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ExperimentPurge> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/purge`,
    'POST',
    { expectedRevisionId, expectedSnapshotVersion, reason },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchExperimentRevisions(
  experimentId: string,
  cursor?: string,
): Promise<{ items: ExperimentRevision[]; nextCursor: string | null }> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/revisions${query}`,
  );
}

export function fetchExperimentTrials(
  experimentId: string,
  cursor?: string,
  revisionId?: string,
): Promise<{ items: ExperimentTrial[]; nextCursor: string | null }> {
  const query = new URLSearchParams();
  if (cursor) query.set('cursor', cursor);
  if (revisionId) query.set('revisionId', revisionId);
  const suffix = query.size ? `?${query.toString()}` : '';
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/trials${suffix}`,
  );
}

export function previewRetryFailedTrials(
  experimentId: string,
  expectedRevisionId: string,
  trialIds: string[],
): Promise<ExperimentRetrySelection> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/retry-failed/preview`,
    'POST',
    { expectedRevisionId, selection: { mode: 'trial_ids', trialIds } },
  );
}

export function retryFailedTrials(
  experimentId: string,
  expectedRevisionId: string,
  trialIds: string[],
  preview: ExperimentRetrySelection,
  idempotencyKey: string,
): Promise<ExperimentRetryResult> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/retry-failed`,
    'POST',
    {
      expectedRevisionId,
      selection: { mode: 'trial_ids', trialIds },
      expectedSelectedTrialCount: preview.selectedTrialCount,
      expectedSelectionDigest: preview.selectionDigest,
      reason: '用户在实验详情页确认重试失败 Trial',
    },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function cancelExperiment(
  experimentId: string,
  idempotencyKey: string,
): Promise<{ experimentId: string; status: string }> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/cancel`,
    'POST',
    {},
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function resumeExperiment(
  experimentId: string,
  expectedRevisionId: string,
  reason: string,
  idempotencyKey: string,
): Promise<{ experimentId: string; revisionId: string; status: string }> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/resume`,
    'POST',
    { expectedRevisionId, reason },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchEvaluationCatalogs(experimentId: string): Promise<EvaluationCatalogs> {
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/evaluation-catalogs`,
  );
}

export function saveEvaluationSpec(
  experimentId: string,
  spec: EvaluationSpecPayload,
  idempotencyKey: string,
): Promise<SavedEvaluationSpec> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/evaluation-specs`,
    'POST',
    spec,
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchEvaluationSpecs(
  experimentId: string,
): Promise<{ items: SavedEvaluationSpec[]; nextCursor: null }> {
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/evaluation-specs`,
  );
}

export function createEvaluationRun(
  experimentId: string,
  evaluationSpecId: string,
  idempotencyKey: string,
): Promise<EvaluationRun> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/evaluation-runs`,
    'POST',
    { evaluationSpecId },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchEvaluationRuns(
  experimentId: string,
): Promise<{ items: EvaluationRun[]; nextCursor: null }> {
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/evaluation-runs`,
  );
}

export function fetchEvaluationRun(
  experimentId: string,
  evaluationRunId: string,
): Promise<EvaluationRunDetail> {
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}`
      + `/evaluation-runs/${encodeURIComponent(evaluationRunId)}`,
  );
}

export function retryEvaluationRun(
  experimentId: string,
  evaluationRunId: string,
  idempotencyKey: string,
): Promise<{ evaluationRunId: string; status: 'queued'; replayed: boolean }> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}`
      + `/evaluation-runs/${encodeURIComponent(evaluationRunId)}/retry`,
    'POST',
    {},
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function createEvaluationExport(
  evaluationRunId: string,
  format: EvaluationExportFormat,
  columns: string[],
  locale: 'zh-CN' | 'en-US',
  idempotencyKey: string,
): Promise<EvaluationExport> {
  return sendJson(
    `/api/backtest/evaluation-runs/${encodeURIComponent(evaluationRunId)}/exports`,
    'POST',
    { format, columns, locale },
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function fetchEvaluationExports(
  evaluationRunId: string,
): Promise<{ items: EvaluationExport[]; nextCursor: null }> {
  return fetchJson(
    `/api/backtest/evaluation-runs/${encodeURIComponent(evaluationRunId)}/exports`,
  );
}

export function fetchEvaluationExport(exportId: string): Promise<EvaluationExport> {
  return fetchJson(`/api/backtest/evaluation-exports/${encodeURIComponent(exportId)}`);
}

export function cancelEvaluationExport(
  exportId: string,
  idempotencyKey: string,
): Promise<{ exportId: string; status: string; replayed: boolean }> {
  return sendJson(
    `/api/backtest/evaluation-exports/${encodeURIComponent(exportId)}/cancel`,
    'POST',
    {},
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function retryEvaluationExport(
  exportId: string,
  idempotencyKey: string,
): Promise<EvaluationExport> {
  return sendJson(
    `/api/backtest/evaluation-exports/${encodeURIComponent(exportId)}/retry`,
    'POST',
    {},
    { 'Idempotency-Key': idempotencyKey },
  );
}

export function evaluationExportDownloadUrl(exportId: string): string {
  return `/api/backtest/evaluation-exports/${encodeURIComponent(exportId)}/download`;
}
