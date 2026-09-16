import type {
  BacktestAccount,
  BacktestChartResponse,
  BacktestRunHistory,
  BacktestRunResponse,
  BacktestErrorDetail,
  ChartResponse,
  DataStatus,
  InstrumentItem,
  MarketInstrument,
  DateItem,
  StrategyConfig,
  StrategyParameterSchemaResponse,
  StrategyConfigVersion,
  StrategyScript,
  StrategySource,
  ReleaseNotice,
  ReleaseStatus,
  SharedBacktestDetail,
  SharedBacktestChart,
  SharedBacktestCopyResponse,
  SharedBacktestListItem,
  SharedBacktestManagementItem,
  SharedBacktestStatus,
  SharedBacktestResultPage,
  SharedBacktestResultSection,
} from '../types';

const BASE = '/api';

export class BacktestRequestError extends Error {
  detail: BacktestErrorDetail;

  constructor(detail: BacktestErrorDetail) {
    super(detail.message);
    this.name = 'BacktestRequestError';
    this.detail = detail;
  }
}

export class ReleaseApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ReleaseApiError';
    this.status = status;
  }
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

async function fetchNoStoreJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include', cache: 'no-store' });
  if (!res.ok) {
    const text = await res.text();
    const message = getApiErrorMessage(text);
    throw new Error(message || `API ${res.status}: 请求失败`);
  }
  return res.json();
}

async function sendJson<T>(url: string, method: string, payload: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    credentials: 'include',
  });
  if (!res.ok) {
    const text = await res.text();
    const message = getApiErrorMessage(text);
    throw new Error(message || `API ${res.status}: 请求失败`);
  }
  return res.json();
}

function getApiErrorMessage(text: string): string {
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (!body.detail || typeof body.detail !== 'object') return text;
    const detail = body.detail as {
      message?: unknown;
      errorDetail?: { message?: unknown; hint?: unknown };
    };
    const message = typeof detail.errorDetail?.message === 'string'
      ? detail.errorDetail.message.trim()
      : '';
    const hint = typeof detail.errorDetail?.hint === 'string'
      ? detail.errorDetail.hint.trim()
      : '';
    if (message) return hint && hint !== message ? `${message}；${hint}` : message;
    return typeof detail.message === 'string' ? detail.message : text;
  } catch {
    // 非 JSON 错误保留服务端原文，便于诊断代理层问题。
    return text;
  }
}

export async function fetchHealth(): Promise<{ status: string }> {
  return fetchJson(`${BASE}/health`);
}

export async function fetchReleaseStatus(
  clientBuildId: string,
  clientCompatibilityVersion: number,
  etag?: string,
): Promise<{ status: ReleaseStatus | null; etag: string | null }> {
  const query = new URLSearchParams({
    clientBuildId,
    clientCompatibilityVersion: String(clientCompatibilityVersion),
  });
  const res = await fetch(`${BASE}/releases/status?${query}`, {
    credentials: 'include',
    headers: etag ? { 'If-None-Match': etag } : {},
  });
  if (res.status === 304) return { status: null, etag: etag ?? null };
  if (!res.ok) throw new ReleaseApiError(res.status, `API ${res.status}: ${await res.text()}`);
  return { status: await res.json() as ReleaseStatus, etag: res.headers.get('ETag') };
}

export async function acknowledgeRelease(releaseId: string, clientBuildId: string): Promise<void> {
  const res = await fetch(`${BASE}/releases/${encodeURIComponent(releaseId)}/acknowledge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientBuildId }),
    credentials: 'include',
  });
  if (!res.ok) {
    const body = await res.text();
    throw new ReleaseApiError(res.status, getApiErrorMessage(body) || `API ${res.status}: 请求失败`);
  }
}

export async function fetchReleaseHistory(): Promise<ReleaseNotice[]> {
  const data = await fetchJson<{ items: ReleaseNotice[] }>(`${BASE}/releases?limit=50`);
  return data.items;
}

export async function fetchStrategyGuide(): Promise<{ title: string; content: string }> {
  return fetchJson(`${BASE}/strategy-guide`);
}

export interface SessionUser {
  id: string;
  username: string;
  displayName: string;
  role: 'admin' | 'user';
}

export interface ApiAccessKey {
  id: string;
  name: string;
  prefix: string;
  scopes: Array<'backtest:submit' | 'backtest:read' | 'backtest:cancel'>;
  expiresAt: string | null;
  revokedAt: string | null;
  lastUsedAt: string | null;
  createdAt: string;
}

export interface ManagedUser extends SessionUser {
  status: 'active' | 'disabled';
  createdAt: string;
  lastLoginAt: string | null;
}

export interface ManagedUserPage {
  users: ManagedUser[];
  page: number;
  pageSize: number;
  total: number;
}

export interface SystemErrorLogEntry {
  id: number;
  runId: string;
  displayNo: number;
  userId: string;
  username: string;
  displayName: string;
  symbol: string;
  timeframe: string;
  source: string;
  stage: string;
  code: string;
  category: string;
  retryable: boolean;
  reason: string;
  exceptionType: string;
  context: Record<string, string>;
  rawLogRef: string;
  occurredAt: string;
}

export interface StrategyRuntimeErrorStat {
  date: string;
  code: 'runtime.contract_missing' | 'runtime.version_mismatch'
    | 'runtime.dependency_missing' | 'runtime.dependency_load_failed';
  runtimeVersion: string;
  missingName: string | null;
  missingModule: string | null;
  count: number;
  lastOccurredAt: string;
}

export async function login(username: string, password: string): Promise<SessionUser> {
  const data = await sendJson<{ user: SessionUser }>(`${BASE}/auth/login`, 'POST', { username, password });
  return data.user;
}

export async function fetchCurrentUser(): Promise<SessionUser> {
  const data = await fetchJson<{ user: SessionUser }>(`${BASE}/auth/me`);
  return data.user;
}

export async function logout(): Promise<void> {
  await sendJson<{ ok: boolean }>(`${BASE}/auth/logout`, 'POST', {});
}

export async function createResearchAuthorizationCode(state: string): Promise<{
  authorizationCode: string;
  expiresIn: number;
  callbackUrl: string;
}> {
  return sendJson(`${BASE}/research/authorization-codes`, 'POST', { state });
}

export async function fetchApiKeys(): Promise<ApiAccessKey[]> {
  const data = await fetchJson<{ keys: ApiAccessKey[] }>(`${BASE}/api-keys`);
  return data.keys;
}

export async function createApiKey(payload: {
  name: string;
  scopes: ApiAccessKey['scopes'];
  expiresAt: string | null;
}): Promise<{ key: ApiAccessKey; plainTextKey: string }> {
  return sendJson<{ key: ApiAccessKey; plainTextKey: string }>(`${BASE}/api-keys`, 'POST', payload);
}

export async function revokeApiKey(keyId: string): Promise<void> {
  const response = await fetch(`${BASE}/api-keys/${encodeURIComponent(keyId)}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) throw new Error(`API ${response.status}: ${await response.text()}`);
}

export async function fetchUsers(): Promise<ManagedUser[]> {
  const firstPage = await fetchUserPage(1, 100);
  const pageCount = Math.ceil(firstPage.total / firstPage.pageSize);
  if (pageCount <= 1) return firstPage.users;
  const remainingPages = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) => fetchUserPage(index + 2, firstPage.pageSize)),
  );
  return firstPage.users.concat(...remainingPages.map((item) => item.users));
}

export async function fetchUserPage(page: number, pageSize: number): Promise<ManagedUserPage> {
  const search = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
  return fetchJson<ManagedUserPage>(`${BASE}/admin/users?${search}`);
}

export async function createUser(payload: { username: string; displayName: string; password: string; role: string }): Promise<ManagedUser> {
  const data = await sendJson<{ user: ManagedUser }>(`${BASE}/admin/users`, 'POST', payload);
  return data.user;
}

export async function updateUser(userId: string, payload: { displayName: string; role: string; status: string }): Promise<ManagedUser> {
  const data = await sendJson<{ user: ManagedUser }>(`${BASE}/admin/users/${userId}`, 'PUT', payload);
  return data.user;
}

export async function resetUserPassword(userId: string, password: string): Promise<void> {
  await sendJson<{ ok: boolean }>(`${BASE}/admin/users/${userId}/reset-password`, 'POST', { password });
}

export async function deleteUser(userId: string): Promise<{ action: 'deleted' | 'disabled'; message: string }> {
  const res = await fetch(`${BASE}/admin/users/${userId}`, { method: 'DELETE', credentials: 'include' });
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

export async function fetchSystemErrorLogs(filters: {
  userId?: string;
  runId?: string;
  errorCode?: string;
  fromTime?: string;
  toTime?: string;
} = {}): Promise<SystemErrorLogEntry[]> {
  const search = new URLSearchParams({ limit: '100' });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const data = await fetchJson<{ items: SystemErrorLogEntry[] }>(`${BASE}/admin/system-errors?${search}`);
  return data.items;
}

export async function fetchStrategyRuntimeErrorStats(filters: {
  fromTime?: string;
  toTime?: string;
} = {}): Promise<StrategyRuntimeErrorStat[]> {
  const search = new URLSearchParams({ limit: '100' });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const data = await fetchJson<{ items: StrategyRuntimeErrorStat[] }>(
    `${BASE}/admin/strategy-runtime-errors?${search}`,
  );
  return data.items;
}

export async function fetchInstruments(): Promise<InstrumentItem[]> {
  const data = await fetchJson<{ instruments: InstrumentItem[] }>(`${BASE}/instruments`);
  return data.instruments;
}

export async function searchMarketInstruments(query: string): Promise<MarketInstrument[]> {
  const search = new URLSearchParams({ q: query, limit: '20' });
  const data = await fetchJson<{ instruments: MarketInstrument[] }>(
    `${BASE}/market-instruments/search?${search.toString()}`
  );
  return data.instruments;
}

export interface BacktestAccountPayload {
  name: string;
  initialCapital: number;
  commissionRate: number;
  slippage: number;
  positionMode: string;
  orderCash: number | null;
  orderPct: number | null;
  allowShort: boolean;
  isDefault: boolean;
}

export async function fetchAccounts(): Promise<BacktestAccount[]> {
  const data = await fetchJson<{ accounts: BacktestAccount[] }>(`${BASE}/accounts`);
  return data.accounts;
}

export async function createAccount(payload: BacktestAccountPayload): Promise<BacktestAccount> {
  const data = await sendJson<{ account: BacktestAccount }>(
    `${BASE}/accounts`,
    'POST',
    payload
  );
  return data.account;
}

export interface BacktestRunPayload {
  configId: string | null;
  configVersionId: string | null;
  scriptId: string | null;
  accountId: number | null;
  account: string;
  mode: 'backtest' | 'live';
  symbol: string;
  benchmarkSymbol: string | null;
  strategy: string;
  signalDatasetId?: string | null;
  timeframe: string;
  startDate: string;
  endDate: string;
  initialCapital: number;
  commissionRate: number;
  slippage: number;
  allowLong: boolean;
  allowShort: boolean;
  strategyCategory: 'core_position_overlay' | 'directional_holding';
  holdingPolicy: 'intraday' | 'overnight';
  overlaySettlementPolicy: 'intraday_restore' | 'cross_day_carry';
  fillModel: 'bar_next_open' | 'l2_quote';
  executionAlgorithm: 'immediate' | 'twap';
  executionParameters: Record<string, number | string>;
  baseQuantity: number;
  baseCostPrice: number;
  initialPositionEnabled: boolean;
  t0Enabled: boolean;
  isDefault: boolean;
  strategyParams?: Record<string, string | boolean>;
  runDraft?: boolean;
}

export interface ResearchSignalDataset {
  id: string;
  name: string;
  datasetKey: string | null;
  version: number | null;
  timeframe: string | null;
  recordCount: number;
  imported?: boolean;
}

export async function fetchResearchSignalDatasets(): Promise<ResearchSignalDataset[]> {
  const data = await fetchJson<{ items: ResearchSignalDataset[] }>(BASE + '/research/signal-datasets');
  return data.items;
}

export interface QueuedBacktestResponse {
  runId: string;
  displayNo: number;
  configId: string | null;
  configVersionId: string | null;
  scriptId: string | null;
  status: 'queued';
  fillModel: 'bar_next_open' | 'l2_quote';
  message: string;
}

export async function runBacktest(payload: BacktestRunPayload): Promise<BacktestRunResponse | QueuedBacktestResponse> {
  const res = await fetch(`${BASE}/backtest/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    credentials: 'include',
  });
  if (!res.ok) {
    const response: unknown = await res.json().catch(() => null);
    const detail = response && typeof response === 'object'
      ? (response as { detail?: { errorDetail?: BacktestErrorDetail } }).detail?.errorDetail
      : undefined;
    if (detail && (detail.category === 'strategy_error' || detail.category === 'system_error' || detail.category === 'data')) {
      throw new BacktestRequestError(detail);
    }
    if (res.status === 422) {
      const message = getValidationErrorMessage(response);
      if (message) {
        throw new Error(`回测参数校验失败：${message}`);
      }
    }
    if (res.status === 503) {
      throw new Error('回测执行服务暂不可用，请稍后重试');
    }
    throw new Error(`API ${res.status}: 回测请求失败`);
  }
  return res.json();
}

function getValidationErrorMessage(response: unknown): string | null {
  if (!response || typeof response !== 'object') return null;
  const detail = (response as { detail?: unknown }).detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (!Array.isArray(detail)) return null;
  const messages = detail.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const message = (item as { msg?: unknown }).msg;
    return typeof message === 'string' && message.trim() ? [message.trim()] : [];
  });
  return messages.length > 0 ? Array.from(new Set(messages)).join('；') : null;
}

export interface LiveStrategyResponse {
  runId: string | null;
  displayNo?: number | null;
  configId: string | null;
  configVersionId: string | null;
  scriptId: string | null;
  status: string;
  message: string;
}

export async function runLiveStrategy(payload: BacktestRunPayload): Promise<LiveStrategyResponse> {
  return sendJson<LiveStrategyResponse>(`${BASE}/backtest/live`, 'POST', payload);
}

export async function fetchBacktestRuns(
  limit = 30,
  configId: string | null = null
): Promise<BacktestRunHistory[]> {
  const search = new URLSearchParams({ limit: String(limit) });
  if (configId != null) search.set('configId', String(configId));
  const data = await fetchJson<{ runs: BacktestRunHistory[] }>(
    `${BASE}/backtest/runs?${search.toString()}`
  );
  return data.runs;
}

export interface BacktestRunPageQuery {
  limit: number;
  offset: number;
  query?: string;
  symbol?: string;
  configId?: string | null;
  favoritesOnly?: boolean;
}

export interface BacktestRunPage {
  runs: BacktestRunHistory[];
  total: number;
  limit: number;
  offset: number;
  symbols: string[];
}

export async function fetchBacktestRunPage(
  params: BacktestRunPageQuery
): Promise<BacktestRunPage> {
  const search = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  });
  if (params.query?.trim()) search.set('query', params.query.trim());
  if (params.symbol) search.set('symbol', params.symbol);
  if (params.configId) search.set('configId', params.configId);
  if (params.favoritesOnly) search.set('favoritesOnly', 'true');
  return fetchJson<BacktestRunPage>(`${BASE}/backtest/runs?${search.toString()}`);
}

export async function fetchBacktestRun(runId: string): Promise<BacktestRunHistory> {
  const data = await fetchJson<{ run: BacktestRunHistory }>(`${BASE}/backtest/runs/${runId}`);
  return data.run;
}

export interface SharedBacktestPage {
  items: SharedBacktestListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface SharedBacktestQuery {
  limit?: number;
  offset?: number;
  query?: string;
  strategyCategory?: string;
  timeframe?: string;
  sort?: 'latest' | 'copies';
  favoritesOnly?: boolean;
}

export async function publishBacktestRun(
  runId: string,
  payload: { title: string; description: string; tags: string[] }
): Promise<{ publication: SharedBacktestDetail; created: boolean }> {
  return sendJson(
    `${BASE}/backtest/runs/${encodeURIComponent(runId)}/publication`,
    'POST',
    payload,
  );
}

export async function fetchSharedBacktests(
  params: SharedBacktestQuery = {},
): Promise<SharedBacktestPage> {
  const search = new URLSearchParams({
    limit: String(params.limit ?? 20),
    offset: String(params.offset ?? 0),
  });
  if (params.query?.trim()) search.set('query', params.query.trim());
  if (params.strategyCategory) search.set('strategyCategory', params.strategyCategory);
  if (params.timeframe) search.set('timeframe', params.timeframe);
  if (params.sort) search.set('sort', params.sort);
  if (params.favoritesOnly) search.set('favoritesOnly', 'true');
  return fetchNoStoreJson<SharedBacktestPage>(`${BASE}/shared-backtests?${search.toString()}`);
}

export async function fetchSharedBacktest(publicationId: string): Promise<SharedBacktestDetail> {
  const data = await fetchNoStoreJson<{ publication: SharedBacktestDetail }>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}`,
  );
  return data.publication;
}

export async function fetchSharedBacktestChart(
  publicationId: string,
): Promise<SharedBacktestChart> {
  return fetchJson<SharedBacktestChart>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/chart`,
  );
}

export async function fetchSharedBacktestResultPage(
  publicationId: string,
  section: SharedBacktestResultSection,
  cursor = '0',
  limit = 50,
): Promise<SharedBacktestResultPage> {
  const search = new URLSearchParams({ cursor, limit: String(limit) });
  return fetchJson<SharedBacktestResultPage>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/${section}?${search.toString()}`,
  );
}

export async function fetchMySharedBacktests(
  limit = 20,
  offset = 0,
  status?: SharedBacktestStatus,
): Promise<{ items: SharedBacktestManagementItem[]; total: number; limit: number; offset: number }> {
  const search = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) search.set('status', status);
  return fetchJson(`${BASE}/shared-backtests/mine?${search.toString()}`);
}

export async function updateSharedBacktestMetadata(
  publicationId: string,
  payload: { title: string; description: string; tags: string[] },
): Promise<SharedBacktestManagementItem> {
  const data = await sendJson<{ publication: SharedBacktestManagementItem }>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}`,
    'PATCH',
    payload,
  );
  return data.publication;
}

export async function withdrawSharedBacktest(
  publicationId: string,
): Promise<SharedBacktestManagementItem> {
  const data = await sendJson<{ publication: SharedBacktestManagementItem }>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/withdraw`,
    'POST',
    {},
  );
  return data.publication;
}

export async function relistSharedBacktest(
  publicationId: string,
): Promise<SharedBacktestManagementItem> {
  const data = await sendJson<{ publication: SharedBacktestManagementItem }>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/relist`,
    'POST',
    {},
  );
  return data.publication;
}

export async function copySharedBacktest(
  publicationId: string,
): Promise<SharedBacktestCopyResponse> {
  return sendJson<SharedBacktestCopyResponse>(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/copy`,
    'POST',
    {},
  );
}

export async function favoriteSharedBacktest(
  publicationId: string,
): Promise<{ favorited: true; created: boolean }> {
  return sendJson(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/favorite`,
    'POST',
    {},
  );
}

export async function unfavoriteSharedBacktest(
  publicationId: string,
): Promise<{ favorited: false; deleted: boolean }> {
  return sendJson(
    `${BASE}/shared-backtests/${encodeURIComponent(publicationId)}/favorite`,
    'DELETE',
    {},
  );
}

export async function fetchAdminSharedBacktests(
  limit = 20,
  offset = 0,
  status?: SharedBacktestStatus,
): Promise<{ items: SharedBacktestManagementItem[]; total: number; limit: number; offset: number }> {
  const search = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) search.set('status', status);
  return fetchNoStoreJson(`${BASE}/admin/shared-backtests?${search.toString()}`);
}

export async function removeSharedBacktest(
  publicationId: string,
  reason: string,
): Promise<SharedBacktestManagementItem> {
  const data = await sendJson<{ publication: SharedBacktestManagementItem }>(
    `${BASE}/admin/shared-backtests/${encodeURIComponent(publicationId)}/remove`,
    'POST',
    { reason },
  );
  return data.publication;
}

export async function restoreSharedBacktest(
  publicationId: string,
): Promise<SharedBacktestManagementItem> {
  const data = await sendJson<{ publication: SharedBacktestManagementItem }>(
    `${BASE}/admin/shared-backtests/${encodeURIComponent(publicationId)}/restore`,
    'POST',
    {},
  );
  return data.publication;
}

export async function cancelBacktestRun(runId: string): Promise<{
  runId: string;
  status: 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled';
}> {
  const res = await fetch(`${BASE}/backtest/runs/${runId}/cancel`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!res.ok) throw new Error(`API ${res.status}: 取消回测失败`);
  return res.json();
}

export async function deleteBacktestRun(runId: string): Promise<void> {
  const res = await fetch(`${BASE}/backtest/runs/${runId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
}

export async function deleteBacktestRuns(runIds: string[]): Promise<number> {
  const data = await sendJson<{ deleted: number }>(`${BASE}/backtest/runs`, 'DELETE', { runIds });
  return data.deleted;
}

export async function fetchBacktestRunChart(
  runId: string,
  date: string,
  timeframe: string
): Promise<BacktestChartResponse> {
  const search = new URLSearchParams({ date, timeframe });
  return fetchJson<BacktestChartResponse>(
    `${BASE}/backtest/runs/${runId}/chart?${search.toString()}`
  );
}

export async function updateBacktestRunMeta(
  runId: string,
  payload: { note: string; tags: string[] }
): Promise<BacktestRunHistory> {
  const data = await sendJson<{ run: BacktestRunHistory }>(
    `${BASE}/backtest/runs/${runId}`,
    'PUT',
    payload
  );
  return data.run;
}

export interface BacktestRunFavoriteState {
  runId: string;
  isFavorited: boolean;
  favoritedAt: string | null;
}

export async function setBacktestRunFavorite(
  runId: string,
  favorited: boolean
): Promise<BacktestRunFavoriteState> {
  return sendJson<BacktestRunFavoriteState>(
    `${BASE}/backtest/runs/${runId}/favorite`,
    'PUT',
    { favorited }
  );
}

export interface StrategyScriptPayload {
  name: string;
  description: string;
  code: string;
  entryType: string;
  signalRequired: boolean;
}

export interface BacktestTimeframeAvailability {
  timeframe: string;
  available: boolean;
  reason: string;
  dataStart: string | null;
  dataEnd: string | null;
}

export interface BacktestPreflightResponse {
  symbol: string;
  startDate: string;
  endDate: string;
  fillModel: 'bar_next_open' | 'l2_quote';
  calendarDays: Array<{
    date: string;
    tradingDay: boolean;
    status: 'available' | 'incomplete' | 'future' | 'non_trading' | 'unknown';
    missingDataTypes: string[];
  }>;
  missingDates: string[];
  timeframes: Array<BacktestTimeframeAvailability & {
    missingDataTypes: string[];
    missingDates: string[];
    availableWarmupDays?: number;
    sourcePeriod?: string | null;
    derivation?: string | null;
  }>;
}

export async function fetchBacktestPreflight(symbol: string, timeframe: string, startDate: string, endDate: string, fillModel: 'bar_next_open' | 'l2_quote'): Promise<BacktestPreflightResponse> {
  const search = new URLSearchParams({ symbol, timeframe, startDate, endDate, fillModel });
  return fetchJson<BacktestPreflightResponse>(`${BASE}/backtest/preflight?${search.toString()}`);
}

export async function fetchBacktestTimeframes(
  symbol: string,
  startDate: string,
  endDate: string
): Promise<BacktestTimeframeAvailability[]> {
  const search = new URLSearchParams({ symbol, startDate, endDate });
  const data = await fetchJson<{ timeframes: BacktestTimeframeAvailability[] }>(
    `${BASE}/backtest/timeframes?${search.toString()}`
  );
  return data.timeframes;
}

export async function fetchBacktestTimeframesBatch(
  symbols: string[],
  startDate: string,
  endDate: string
): Promise<Record<string, BacktestTimeframeAvailability[]>> {
  const search = new URLSearchParams({
    symbols: symbols.join(','),
    startDate,
    endDate,
  });
  const data = await fetchJson<{ timeframesBySymbol: Record<string, BacktestTimeframeAvailability[]> }>(
    `${BASE}/backtest/timeframes/batch?${search.toString()}`
  );
  return data.timeframesBySymbol;
}

export async function fetchStrategyScripts(): Promise<StrategyScript[]> {
  const data = await fetchJson<{ scripts: StrategyScript[] }>(`${BASE}/strategy-scripts`);
  return data.scripts;
}

export async function fetchStrategySource(strategyId: string): Promise<StrategySource> {
  return fetchJson<StrategySource>(`${BASE}/strategies/${strategyId}/source`);
}

export async function createStrategyScript(payload: StrategyScriptPayload): Promise<StrategyScript> {
  const data = await sendJson<{ script: StrategyScript }>(
    `${BASE}/strategy-scripts`,
    'POST',
    payload
  );
  return data.script;
}

export async function updateStrategyScript(
  scriptId: string,
  payload: StrategyScriptPayload
): Promise<StrategyScript> {
  const data = await sendJson<{ script: StrategyScript }>(
    `${BASE}/strategy-scripts/${scriptId}`,
    'PUT',
    payload
  );
  return data.script;
}

export async function deleteStrategyScript(scriptId: string): Promise<boolean> {
  const data = await fetch(`${BASE}/strategy-scripts/${scriptId}`, { method: 'DELETE', credentials: 'include' });
  if (!data.ok) throw new Error(`API ${data.status}: ${await data.text()}`);
  return (await data.json()).deleted;
}

export interface StrategyConfigPayload {
  name: string;
  scriptId: string | null;
  strategy: string;
  mode: string;
  symbol: string;
  timeframe: string;
  benchmarkSymbol: string | null;
  accountId: number | null;
  paramsJson: Record<string, unknown>;
  enabled: boolean;
  publish?: boolean;
}

export async function fetchStrategyConfigs(): Promise<StrategyConfig[]> {
  const data = await fetchJson<{ configs: StrategyConfig[] }>(`${BASE}/strategy-configs`);
  return data.configs;
}

export async function fetchStrategyConfigVersions(configId: string): Promise<StrategyConfigVersion[]> {
  const data = await fetchJson<{ versions: StrategyConfigVersion[] }>(
    `${BASE}/strategy-configs/${configId}/versions`
  );
  return data.versions;
}

export async function createStrategyConfig(payload: StrategyConfigPayload): Promise<StrategyConfig> {
  const data = await sendJson<{ config: StrategyConfig }>(
    `${BASE}/strategy-configs`,
    'POST',
    payload
  );
  return data.config;
}

export async function updateStrategyConfig(
  configId: string,
  payload: StrategyConfigPayload
): Promise<StrategyConfig> {
  const data = await sendJson<{ config: StrategyConfig }>(
    `${BASE}/strategy-configs/${configId}`,
    'PUT',
    payload
  );
  return data.config;
}

export async function saveCompleteStrategy(
  script: StrategyScriptPayload,
  config: StrategyConfigPayload,
  scriptId: string | null,
  configId: string | null,
  expectedRevision: number | null,
): Promise<{ script: StrategyScript; config: StrategyConfig }> {
  return sendJson<{ script: StrategyScript; config: StrategyConfig }>(
    `${BASE}/complete-strategies`,
    'POST',
    { script, config, scriptId, configId, expectedRevision },
  );
}

export async function publishStrategyConfig(
  configId: string
): Promise<{ version: StrategyConfigVersion; created: boolean }> {
  return sendJson<{ version: StrategyConfigVersion; created: boolean }>(
    `${BASE}/strategy-configs/${configId}/publish`,
    'POST',
    {}
  );
}

export async function deleteStrategyConfig(configId: string): Promise<boolean> {
  const data = await fetch(`${BASE}/strategy-configs/${configId}`, { method: 'DELETE', credentials: 'include' });
  if (!data.ok) throw new Error(`API ${data.status}: ${await data.text()}`);
  return (await data.json()).deleted;
}

export async function fetchDataStatus(symbol: string, timeframe = '5m'): Promise<DataStatus> {
  const response = await fetch(
    `${BASE}/data/status?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`,
    { credentials: 'include' },
  );
  if (!response.ok) {
    const text = await response.text();
    let message: string | null = null;
    try {
      const payload = JSON.parse(text) as { detail?: { message?: unknown } };
      if (typeof payload.detail?.message === 'string') {
        message = payload.detail.message;
      }
    } catch {}
    if (message) throw new Error(message);
    throw new Error(`API ${response.status}: ${text}`);
  }
  const data = await response.json() as { status: DataStatus };
  return data.status;
}

export async function fetchDates(symbol: string, timeframe = '5m'): Promise<DateItem[]> {
  const data = await fetchJson<{ dates: DateItem[] }>(
    `${BASE}/dates?symbol=${symbol}&timeframe=${timeframe}`
  );
  return data.dates;
}

export async function fetchChart(
  symbol: string,
  date: string,
  timeframe = '5m',
  strategy = 'trend_grid',
  signalFile = ''
): Promise<ChartResponse> {
  let url = `${BASE}/chart?symbol=${symbol}&date=${date}&timeframe=${timeframe}&strategy=${strategy}`;
  if (signalFile) url += `&signal_file=${encodeURIComponent(signalFile)}`;
  return fetchJson<ChartResponse>(url);
}

export async function fetchStrategies(): Promise<string[]> {
  const data = await fetchJson<{ strategies: string[] }>(`${BASE}/strategies`);
  return data.strategies;
}

export async function fetchStrategyParameterSchema(
  strategy: string
): Promise<StrategyParameterSchemaResponse> {
  return fetchJson<StrategyParameterSchemaResponse>(
    `${BASE}/strategies/${encodeURIComponent(strategy)}/parameters`
  );
}

export interface SignalFileItem {
  name: string;
  mtime: string;
}

export async function fetchSignalFiles(): Promise<SignalFileItem[]> {
  const data = await fetchJson<{ files: SignalFileItem[] }>(`${BASE}/signal-files`);
  return data.files;
}
