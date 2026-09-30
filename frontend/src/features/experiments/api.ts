import { fetchJson, sendJson } from '../../api/client';
import type {
  ExperimentPreview,
  ExperimentPreviewRequest,
  ExperimentCreateRequest,
  ExperimentCreateResponse,
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentListItem,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
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

export function fetchExperimentDetail(experimentId: string): Promise<ExperimentDetail> {
  return fetchJson(`/api/backtest/experiments/${encodeURIComponent(experimentId)}`);
}

export function fetchExperiments(
  cursor?: string,
): Promise<{ items: ExperimentListItem[]; nextCursor: string | null }> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return fetchJson(`/api/backtest/experiments${query}`);
}

export function fetchExperimentTrials(
  experimentId: string,
  cursor?: string,
): Promise<{ items: ExperimentTrial[]; nextCursor: string | null }> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return fetchJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/trials${query}`,
  );
}

export function cancelExperiment(
  experimentId: string,
  idempotencyKey: string,
): Promise<{ experimentId: string; status: 'cancelled' }> {
  return sendJson(
    `/api/backtest/experiments/${encodeURIComponent(experimentId)}/cancel`,
    'POST',
    {},
    { 'Idempotency-Key': idempotencyKey },
  );
}
