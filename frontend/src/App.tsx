import { useState, useEffect, useCallback, useRef } from 'react';
import Toolbar from './components/Toolbar';
import KlineChart from './components/KlineChart';
import StatusBar from './components/StatusBar';
import BacktestPage from './components/BacktestPage';
import LoginPage from './components/LoginPage';
import SystemErrorLogPage from './components/SystemErrorLogPage';
import UserManagementPage from './components/UserManagementPage';
import ApiKeyPage from './components/ApiKeyPage';
import SharedBacktestPage from './components/SharedBacktestPage';
import ReleaseNotificationManager from './components/ReleaseNotificationManager';
import {
  fetchBacktestRunChart,
  fetchChart,
  fetchCurrentUser,
  fetchDates,
  fetchInstruments,
  fetchStrategies,
  createResearchAuthorizationCode,
  logout,
} from './api/client';
import type { SessionUser } from './api/client';
import { useUrlState } from './hooks/useUrlState';
import type { BacktestChartResponse, ChartResponse, DateItem, DisplayModule, DisplaySide, InstrumentItem } from './types';
import { typography } from './styles/typography';

function createResearchState(): string {
  const bytes = new Uint8Array(24);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function enterResearchCenter(): Promise<void> {
  const state = createResearchState();
  const payload = await createResearchAuthorizationCode(state);
  const callback = new URL(payload.callbackUrl);
  callback.searchParams.set('authorizationCode', payload.authorizationCode);
  callback.searchParams.set('state', state);
  window.location.assign(callback.toString());
}

function Workspace({ currentUser, onLogout }: { currentUser: SessionUser; onLogout: () => void }) {
  const [activePage, setActivePage] = useState<'signals' | 'backtest' | 'sharedBacktests' | 'apiKeys' | 'users' | 'systemErrors'>('backtest');
  const [sharedPublicationId, setSharedPublicationId] = useState<string | null>(null);
  const [strategyConfigToOpen, setStrategyConfigToOpen] = useState<string | null>(null);
  const [urlParams, setUrlParams] = useUrlState();
  const [researchError, setResearchError] = useState<string | null>(null);
  const [researchStarting, setResearchStarting] = useState(false);
  const [workspaceSafety, setWorkspaceSafety] = useState({
    hasUnsavedStrategy: false,
    isStrategySavePending: false,
  });
  const strategySaveActionRef = useRef<() => Promise<boolean>>(async () => false);
  const requestStrategySave = useCallback(
    () => strategySaveActionRef.current(),
    [],
  );
  const registerStrategySave = useCallback((action: () => Promise<boolean>) => {
    strategySaveActionRef.current = action;
  }, []);

  const [instruments, setInstruments] = useState<InstrumentItem[]>([]);
  const [strategies, setStrategies] = useState<string[]>(['user_script']);
  const [dates, setDates] = useState<DateItem[]>([]);
  const [chartData, setChartData] = useState<ChartResponse | null>(null);
  const [backtestChart, setBacktestChart] = useState<BacktestChartResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const chartRequestRef = useRef(0);
  const signalModules: DisplayModule[] = ['A', 'B'];
  const signalSides: DisplaySide[] = ['BUY', 'SELL'];

  // 加载策略列表与标的目录，供回测页面使用。
  useEffect(() => {
    fetchInstruments().then(setInstruments).catch(() => {});
    fetchStrategies().then(setStrategies).catch(() => {});
  }, []);

  // load dates when symbol changes
  useEffect(() => {
    let cancelled = false;
    fetchDates(urlParams.symbol, urlParams.timeframe).then((d) => {
      if (cancelled) return;
      setDates(d);
      // If current date not in list, pick first available
      if (d.length > 0 && !d.find((x) => x.date === urlParams.date)) {
        const best = d.find((x) => x.hasKline && x.hasSignal) || d.find((x) => x.hasKline) || d[0];
        setUrlParams({ date: best.date });
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [urlParams.symbol, urlParams.date, urlParams.timeframe, setUrlParams]);

  const toBacktestChartData = useCallback((response: BacktestChartResponse): ChartResponse => ({
    symbol: response.symbol,
    date: response.date,
    timeframe: response.timeframe,
    bars: response.bars,
    markers: [],
    annotations: [],
    theoretical: [],
    summary: { aBuy: 0, aSell: 0, bBuy: 0, bSell: 0, barCount: response.bars.length, warnings: [] },
    dataFreshness: { klineMtime: null, signalMtime: null },
  }), []);

  // load chart data
  const loadChart = useCallback(async () => {
    const requestId = chartRequestRef.current + 1;
    chartRequestRef.current = requestId;
    setLoading(true);
    setError(null);
    try {
      let nextBacktestChart: BacktestChartResponse | null = null;
      const data = urlParams.runId
        ? toBacktestChartData(nextBacktestChart = await fetchBacktestRunChart(
          urlParams.runId,
          urlParams.date,
          urlParams.timeframe,
        ))
        : await fetchChart(urlParams.symbol, urlParams.date, urlParams.timeframe, 'user_script');
      if (requestId !== chartRequestRef.current) return;
      setChartData(data);
      setBacktestChart(nextBacktestChart);
      if (data.summary.warnings.length > 0) {
        console.warn('Chart warnings:', data.summary.warnings);
      }
    } catch (e) {
      if (requestId !== chartRequestRef.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setChartData(null);
      setBacktestChart(null);
    } finally {
      if (requestId === chartRequestRef.current) setLoading(false);
    }
  }, [toBacktestChartData, urlParams.date, urlParams.runId, urlParams.symbol, urlParams.timeframe]);

  useEffect(() => {
    loadChart();
  }, [loadChart]);

  // date navigation — dates 按日期倒序（最新在前），offset +1 = 更旧
  const navByOffset = useCallback((offset: number) => {
    const idx = dates.findIndex((d) => d.date === urlParams.date);
    const newIdx = idx + offset;
    if (newIdx >= 0 && newIdx < dates.length) {
      setUrlParams({ date: dates[newIdx].date });
    }
  }, [dates, urlParams.date, setUrlParams]);

  return (
    <div className="workspace-shell" style={{
      display: 'flex', flexDirection: 'column', height: '100vh',
      background: 'var(--canvas)', color: 'var(--text)',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
      fontSize: typography.base,
      lineHeight: 1.5,
    }}>
      <div className="app-topbar" style={{
        display: 'flex', alignItems: 'center', gap: '4px',
        minHeight: 44, padding: '0 18px', background: 'var(--chrome)',
        borderBottom: '1px solid #2a2e39', flexShrink: 0,
      }}>
        <div className="app-brand" style={{
          color: '#f1f3f7', fontSize: typography.lg, fontWeight: 750,
          marginRight: 14, letterSpacing: '.02em', fontFamily: 'ui-serif, "Songti SC", "Noto Serif SC", serif',
        }}>
          玄龄量化回测系统
        </div>
        {[
          { key: 'backtest' as const, label: '策略回测' },
          { key: 'sharedBacktests' as const, label: '策略广场' },
          { key: 'signals' as const, label: '信号可视化' },
          { key: 'apiKeys' as const, label: 'API Key' },
          ...(currentUser.role === 'admin' ? [{ key: 'users' as const, label: '人员管理' }] : []),
          ...(currentUser.role === 'admin' ? [{ key: 'systemErrors' as const, label: '回测诊断日志' }] : []),
        ].map((item) => {
          const active = activePage === item.key;
          return (
            <button
              key={item.key}
              className="app-nav-button"
              onClick={() => {
                if (item.key === 'sharedBacktests') setSharedPublicationId(null);
                setActivePage(item.key);
              }}
              style={{
                height: 44, padding: '0 12px', borderRadius: 0,
                border: 'none', borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                background: active ? 'var(--chrome-raised)' : 'transparent',
                color: active ? '#ffffff' : 'var(--chrome-text)',
                fontSize: typography.base, cursor: 'pointer',
              }}
            >
              {item.label}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => {
            setResearchStarting(true);
            setResearchError(null);
            enterResearchCenter().catch((error: unknown) => {
              setResearchError(error instanceof Error ? error.message : '无法进入研究中心');
              setResearchStarting(false);
            });
          }}
          disabled={researchStarting}
          style={{ marginLeft: 8, border: '1px solid #437463', borderRadius: 4, padding: '5px 10px', background: '#163a32', color: '#c8f1e2', cursor: researchStarting ? 'wait' : 'pointer', fontSize: typography.sm }}
        >
          {researchStarting ? '正在进入…' : '研究中心'}
        </button>
        {researchError && <span role="alert" style={{ color: '#fda4af', fontSize: typography.sm }}>{researchError}</span>}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10, color: 'var(--chrome-text)', fontSize: typography.sm }}>
          <ReleaseNotificationManager
            userId={currentUser.id}
            hasUnsavedStrategy={workspaceSafety.hasUnsavedStrategy}
            isStrategySavePending={workspaceSafety.isStrategySavePending}
            onRequestStrategySave={requestStrategySave}
          />
          <span>{currentUser.displayName} · {currentUser.role === 'admin' ? '管理员' : '普通用户'}</span>
          <button onClick={onLogout} style={{ border: '1px solid #3a3f4b', borderRadius: 3, padding: '4px 9px', background: 'transparent', color: 'var(--chrome-text)', cursor: 'pointer' }}>退出</button>
        </div>
      </div>

      {activePage === 'signals' && (
        <>
          <Toolbar
            symbol={urlParams.symbol}
            date={urlParams.date}
            timeframe={urlParams.timeframe}
            dates={dates}
            loading={loading}
            backtestDisplayNo={backtestChart?.displayNo}
            onDateChange={(d) => setUrlParams({ date: d })}
            onTimeframeChange={(timeframe) => setUrlParams({ timeframe })}
            onPrevDay={() => navByOffset(1)}
            onNextDay={() => navByOffset(-1)}
            onRefresh={loadChart}
          />

          <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <div style={{ flex: 1, position: 'relative' }}>
                {error && !loading && (
                  <div style={{
                    position: 'absolute', top: 0, left: 0, right: 0, zIndex: 10,
                    background: 'var(--danger-soft)', color: 'var(--danger)', padding: '7px 16px',
                    borderBottom: '1px solid #f6c7c2',
                    fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px',
                  }}>
                    <span>⚠</span>
                    <span style={{ flex: 1 }}>{error}</span>
                  </div>
                )}
                <KlineChart
                  data={chartData}
                  modules={signalModules}
                  sides={signalSides}
                  showPrices={false}
                  markerSize="normal"
                  backtestTrades={urlParams.runId ? backtestChart?.trades ?? [] : undefined}
                  backtestDisplayNo={backtestChart?.displayNo}
                  backtestStrategyCategory={backtestChart?.strategyCategory}
                />
            </div>
          </div>

          <StatusBar data={chartData} error={error} loading={loading} />
        </>
      )}

      <div style={{ display: activePage === 'backtest' ? 'contents' : 'none' }}>
        <BacktestPage
          symbol={urlParams.symbol}
          strategy={urlParams.strategy}
          strategies={strategies}
          instruments={instruments}
          isAdmin={currentUser.role === 'admin'}
          onWorkspaceSafetyStateChange={setWorkspaceSafety}
          onStrategySaveActionChange={registerStrategySave}
          onViewTradeChart={({ runId, symbol, timeframe, date }) => {
            setUrlParams({ runId, symbol, timeframe, date });
            setActivePage('signals');
          }}
          onViewSharedBacktest={(publicationId) => {
            setSharedPublicationId(publicationId);
            setActivePage('sharedBacktests');
          }}
          requestedConfigId={strategyConfigToOpen}
        />
      </div>

      {activePage === 'sharedBacktests' && (
        <SharedBacktestPage
          initialPublicationId={sharedPublicationId}
          isAdmin={currentUser.role === 'admin'}
          onOpenCopiedStrategy={(configId) => {
            setStrategyConfigToOpen(configId);
            setActivePage('backtest');
          }}
        />
      )}

      {activePage === 'users' && (
        <UserManagementPage />
      )}

      {activePage === 'systemErrors' && (
        <SystemErrorLogPage />
      )}

      {activePage === 'apiKeys' && (
        <ApiKeyPage />
      )}
    </div>
  );
}

export default function App() {
  const [currentUser, setCurrentUser] = useState<SessionUser | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    fetchCurrentUser().then(setCurrentUser).catch(() => setCurrentUser(null)).finally(() => setCheckingSession(false));
  }, []);

  const handleLogout = async () => {
    try {
      await logout();
    } finally {
      setCurrentUser(null);
    }
  };

  if (checkingSession) {
    return <div style={{ height: '100vh', display: 'grid', placeItems: 'center', background: 'var(--canvas)', color: 'var(--text-muted)' }}>正在检查登录状态…</div>;
  }
  if (!currentUser) return <LoginPage onLogin={setCurrentUser} />;
  return <Workspace currentUser={currentUser} onLogout={() => { void handleLogout(); }} />;
}
