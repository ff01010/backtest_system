import { useCallback, useState } from 'react';
import type { DisplayModule, DisplaySide } from '../types';

export interface ViewerParams {
  symbol: string;
  date: string;
  timeframe: string;
  strategy: string;
  modules: DisplayModule[];
  sides: DisplaySide[];
}

const DEFAULTS: ViewerParams = {
  symbol: '688981.SH',
  date: '20260622',
  timeframe: '5m',
  strategy: 'trend_grid',
  modules: ['A', 'B'],
  sides: ['BUY', 'SELL'],
};

function getSearchParams(): URLSearchParams {
  return new URLSearchParams(window.location.search);
}

function updateUrl(params: ViewerParams): void {
  const sp = new URLSearchParams();
  sp.set('symbol', params.symbol);
  sp.set('date', params.date);
  if (params.timeframe !== '5m') sp.set('timeframe', params.timeframe);
  if (params.strategy !== 'trend_grid') sp.set('strategy', params.strategy);
  if (params.modules.length < 2) sp.set('modules', params.modules.join(','));
  if (params.sides.length < 2) sp.set('sides', params.sides.join(','));
  const qs = sp.toString();
  const newUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
  window.history.replaceState(null, '', newUrl);
}

export function readUrlParams(): ViewerParams {
  const sp = getSearchParams();
  return {
    symbol: sp.get('symbol') || DEFAULTS.symbol,
    date: sp.get('date') || DEFAULTS.date,
    timeframe: sp.get('timeframe') || DEFAULTS.timeframe,
    strategy: sp.get('strategy') || DEFAULTS.strategy,
    modules: (sp.get('modules') || 'A,B').split(',').filter(
      (m): m is DisplayModule => m === 'A' || m === 'B'
    ),
    sides: (sp.get('sides') || 'BUY,SELL').split(',').filter(
      (s): s is DisplaySide => s === 'BUY' || s === 'SELL'
    ),
  };
}

export function useUrlState() {
  const [params, setParamsState] = useState<ViewerParams>(readUrlParams);

  const setParams = useCallback((update: Partial<ViewerParams>) => {
    setParamsState((prev) => {
      const next = { ...prev, ...update };
      updateUrl(next);
      return next;
    });
  }, []);

  return [params, setParams] as const;
}

// localStorage preferences
export interface ViewerPrefs {
  showPrices: boolean;
  markerSize: 'small' | 'normal' | 'large';
  autoRefresh: 'off' | '30s' | '60s';
  sidebarOpen: boolean;
  comparisonMode: boolean;
}

const PREFS_KEY = 'trend_grid_viewer_prefs';
const PREFS_DEFAULTS: ViewerPrefs = {
  showPrices: true,
  markerSize: 'small',
  autoRefresh: 'off',
  sidebarOpen: true,
  comparisonMode: false,
};

export function readPrefs(): ViewerPrefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) return { ...PREFS_DEFAULTS, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return { ...PREFS_DEFAULTS };
}

export function savePrefs(prefs: ViewerPrefs): void {
  localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
}
