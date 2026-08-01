import type {
  BacktestAccount,
  BacktestRunHistory,
  BacktestRunResponse,
  ChartResponse,
  DataStatus,
  DataSyncResult,
  DataSyncRun,
  DataSyncTask,
  InstrumentItem,
  MarketInstrument,
  DateItem,
  StrategyConfig,
  StrategyParameterSchemaResponse,
  StrategyConfigVersion,
  StrategyScript,
  StrategySource,
} from '../types';

const BASE = '/api';

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
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
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

export async function fetchHealth(): Promise<{ status: string }> {
  return fetchJson(`${BASE}/health`);
}

export interface SessionUser {
  id: string;
  username: string;
  displayName: string;
  role: 'admin' | 'user';
}

export interface ManagedUser extends SessionUser {
  status: 'active' | 'disabled';
  createdAt: string;
  lastLoginAt: string | null;
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

export async function fetchUsers(): Promise<ManagedUser[]> {
  const data = await fetchJson<{ users: ManagedUser[] }>(`${BASE}/admin/users`);
  return data.users;
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

export async function refreshMarketInstruments(): Promise<number> {
  const data = await sendJson<{ count: number }>(`${BASE}/admin/market-instruments/refresh`, 'POST', {});
  return data.count;
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
  timeframe: string;
  startDate: string;
  endDate: string;
  initialCapital: number;
  commissionRate: number;
  slippage: number;
  allowLong: boolean;
  allowShort: boolean;
  holdingPolicy: 'intraday' | 'overnight';
  isDefault: boolean;
  strategyParams?: Record<string, string | boolean>;
  runDraft?: boolean;
}

export async function runBacktest(payload: BacktestRunPayload): Promise<BacktestRunResponse> {
  return sendJson<BacktestRunResponse>(`${BASE}/backtest/run`, 'POST', payload);
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

export async function fetchBacktestRun(runId: string): Promise<BacktestRunHistory> {
  const data = await fetchJson<{ run: BacktestRunHistory }>(`${BASE}/backtest/runs/${runId}`);
  return data.run;
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

export interface StrategyScriptPayload {
  name: string;
  description: string;
  code: string;
  entryType: string;
}

export interface BacktestTimeframeAvailability {
  timeframe: string;
  available: boolean;
  reason: string;
  dataStart: string | null;
  dataEnd: string | null;
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

export interface DataSyncPayload {
  symbol: string;
  timeframe: string;
  startDate: string | null;
  endDate: string | null;
  mode: 'incremental' | 'range' | string;
}

export async function fetchDataStatus(symbol: string, timeframe = '5m'): Promise<DataStatus> {
  const data = await fetchJson<{ status: DataStatus }>(
    `${BASE}/data/status?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`
  );
  return data.status;
}

export async function fetchDataSyncRuns(limit = 20): Promise<DataSyncRun[]> {
  const data = await fetchJson<{ runs: DataSyncRun[] }>(`${BASE}/data/sync-runs?limit=${limit}`);
  return data.runs;
}

export async function runDataSync(payload: DataSyncPayload): Promise<DataSyncResult> {
  return sendJson<DataSyncResult>(`${BASE}/data/sync`, 'POST', payload);
}

export async function runDataSyncBatch(
  payload: Omit<DataSyncPayload, 'symbol'> & { symbols: string[] }
): Promise<DataSyncResult[]> {
  const data = await sendJson<{ results: DataSyncResult[] }>(
    `${BASE}/data/sync/batch`,
    'POST',
    payload
  );
  return data.results;
}

export async function startDataSyncTask(
  payload: Omit<DataSyncPayload, 'symbol'> & { symbols: string[] }
): Promise<DataSyncTask> {
  const data = await sendJson<{ task: DataSyncTask }>(
    `${BASE}/data/sync/start`,
    'POST',
    payload
  );
  return data.task;
}

export async function fetchDataSyncTask(taskId: string): Promise<DataSyncTask> {
  const data = await fetchJson<{ task: DataSyncTask }>(`${BASE}/data/sync/tasks/${taskId}`);
  return data.task;
}

export async function refillDataGaps(
  payload: { symbol: string; timeframe: string; dates: string[] }
): Promise<DataSyncTask> {
  const data = await sendJson<{ task: DataSyncTask }>(`${BASE}/data/refill`, 'POST', payload);
  return data.task;
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

export interface SyncResult {
  status: string;
  symbol: string;
  from: string;
  to: string;
  new_rows: number;
  new_dates: string[];
  message?: string;
}

export async function syncKline(symbol: string): Promise<SyncResult> {
  const res = await fetch(`${BASE}/data/sync?symbol=${symbol}`, { method: 'POST', credentials: 'include' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text);
  }
  return res.json();
}
