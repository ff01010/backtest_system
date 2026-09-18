import type { BacktestRunSelection } from '../types';

export interface BacktestRunFilters {
  query: string;
  symbol: string;
  configId: string | null;
  favoritesOnly: boolean;
}

export type BacktestRunSelectionState = {
  mode: 'ids';
  runIds: Set<string>;
} | {
  mode: 'filter';
  filters: BacktestRunFilters;
  excludedRunIds: Set<string>;
  selectableTotal: number;
};

export function emptyBacktestRunSelection(): BacktestRunSelectionState {
  return { mode: 'ids', runIds: new Set() };
}

export function backtestRunFilterKey(filters: BacktestRunFilters): string {
  return JSON.stringify({
    query: filters.query.trim(),
    symbol: filters.symbol || '',
    configId: filters.configId || null,
    favoritesOnly: filters.favoritesOnly,
  });
}

export function selectedBacktestRunCount(state: BacktestRunSelectionState): number {
  if (state.mode === 'ids') return state.runIds.size;
  return Math.max(0, state.selectableTotal - state.excludedRunIds.size);
}

export function isBacktestRunSelected(
  state: BacktestRunSelectionState,
  runId: string,
): boolean {
  return state.mode === 'ids'
    ? state.runIds.has(runId)
    : !state.excludedRunIds.has(runId);
}

export function toggleBacktestRun(
  state: BacktestRunSelectionState,
  runId: string,
  checked: boolean,
): BacktestRunSelectionState {
  if (state.mode === 'ids') {
    const runIds = new Set(state.runIds);
    if (checked) runIds.add(runId);
    else runIds.delete(runId);
    return { mode: 'ids', runIds };
  }
  const excludedRunIds = new Set(state.excludedRunIds);
  if (checked) excludedRunIds.delete(runId);
  else excludedRunIds.add(runId);
  return { ...state, excludedRunIds };
}

export function toggleBacktestRunPage(
  state: BacktestRunSelectionState,
  pageRunIds: string[],
  checked: boolean,
): BacktestRunSelectionState {
  return pageRunIds.reduce(
    (current, runId) => toggleBacktestRun(current, runId, checked),
    state,
  );
}

export function areAllPageRunsSelected(
  state: BacktestRunSelectionState,
  pageRunIds: string[],
): boolean {
  return pageRunIds.length > 0
    && pageRunIds.every((runId) => isBacktestRunSelected(state, runId));
}

export function selectAllFilteredBacktestRuns(
  filters: BacktestRunFilters,
  selectableTotal: number,
): BacktestRunSelectionState {
  return {
    mode: 'filter',
    filters: { ...filters, query: filters.query.trim() },
    excludedRunIds: new Set(),
    selectableTotal,
  };
}

export function toBacktestRunSelection(
  state: BacktestRunSelectionState,
): BacktestRunSelection {
  if (state.mode === 'ids') {
    return { mode: 'ids', runIds: [...state.runIds].sort() };
  }
  return {
    mode: 'filter',
    query: state.filters.query,
    symbol: state.filters.symbol || null,
    configId: state.filters.configId,
    favoritesOnly: state.filters.favoritesOnly,
    excludedRunIds: [...state.excludedRunIds].sort(),
  };
}
