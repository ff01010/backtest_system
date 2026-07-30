import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import Toolbar from './components/Toolbar';
import Sidebar from './components/Sidebar';
import KlineChart from './components/KlineChart';
import StatusBar from './components/StatusBar';
import BacktestPage from './components/BacktestPage';
import DataPage from './components/DataPage';
import LoginPage from './components/LoginPage';
import UserManagementPage from './components/UserManagementPage';
import { fetchCurrentUser, fetchInstruments, fetchDates, fetchChart, fetchStrategies, fetchSignalFiles, logout, syncKline } from './api/client';
import type { SessionUser, SignalFileItem } from './api/client';
import { useUrlState, readPrefs, savePrefs } from './hooks/useUrlState';
import type { InstrumentItem, DateItem, ChartResponse } from './types';
import type { ViewerPrefs } from './hooks/useUrlState';
import { typography } from './styles/typography';

function Workspace({ currentUser, onLogout }: { currentUser: SessionUser; onLogout: () => void }) {
  const [activePage, setActivePage] = useState<'signals' | 'backtest' | 'data' | 'users'>('backtest');
  const [urlParams, setUrlParams] = useUrlState();
  const [prefs, setPrefs] = useState<ViewerPrefs>(readPrefs);

  const [instruments, setInstruments] = useState<InstrumentItem[]>([]);
  const [strategies, setStrategies] = useState<string[]>(['user_script']);
  const [signalFiles, setSignalFiles] = useState<SignalFileItem[]>([]);
  const [signalFile, setSignalFile] = useState('');
  const [dates, setDates] = useState<DateItem[]>([]);
  const [chartData, setChartData] = useState<ChartResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const refreshTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const signalStrategies = useMemo<string[]>(
    () => strategies.filter((item) => item === 'trend_grid' || item === 'trend_grid_both'),
    [strategies]
  );
  const signalStrategy = signalStrategies.includes(urlParams.strategy)
    ? urlParams.strategy
    : (signalStrategies[0] || 'user_script');

  // update prefs
  const handlePrefsChange = useCallback((newPrefs: ViewerPrefs) => {
    setPrefs(newPrefs);
    savePrefs(newPrefs);
  }, []);

  // load instruments + strategies + signal files on mount
  useEffect(() => {
    fetchInstruments().then(setInstruments).catch(() => {});
    fetchStrategies().then(setStrategies).catch(() => {});
    fetchSignalFiles().then(setSignalFiles).catch(() => {});
  }, []);

  // load dates when symbol changes
  useEffect(() => {
    let cancelled = false;
    fetchDates(urlParams.symbol).then((d) => {
      if (cancelled) return;
      setDates(d);
      // If current date not in list, pick first available
      if (d.length > 0 && !d.find((x) => x.date === urlParams.date)) {
        const best = d.find((x) => x.hasKline && x.hasSignal) || d.find((x) => x.hasKline) || d[0];
        setUrlParams({ date: best.date });
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [urlParams.symbol, urlParams.date, setUrlParams]);

  // load chart data
  const loadChart = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchChart(urlParams.symbol, urlParams.date, '5m', signalStrategy, signalFile);
      setChartData(data);
      if (data.summary.warnings.length > 0) {
        console.warn('Chart warnings:', data.summary.warnings);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setChartData(null);
    } finally {
      setLoading(false);
    }
  }, [urlParams.symbol, urlParams.date, signalStrategy, signalFile]);

  useEffect(() => {
    loadChart();
  }, [loadChart]);

  // auto refresh
  useEffect(() => {
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }

    if (prefs.autoRefresh !== 'off') {
      const ms = prefs.autoRefresh === '30s' ? 30000 : 60000;
      refreshTimerRef.current = setInterval(loadChart, ms);
    }

    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [prefs.autoRefresh, loadChart]);

  // sync K-line data
  const handleSync = useCallback(async () => {
    setSyncing(true);
    try {
      const result = await syncKline(urlParams.symbol);
      if (result.status === 'ok' && result.new_rows > 0) {
        // 刷新日期列表
        const newDates = await fetchDates(urlParams.symbol);
        setDates(newDates);
        // 选最新日期
        if (newDates.length > 0) {
          setUrlParams({ date: newDates[0].date });
        }
      } else if (result.status === 'up_to_date') {
        // 已是最新，刷新日期列表
        const newDates = await fetchDates(urlParams.symbol);
        setDates(newDates);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '同步失败');
    } finally {
      setSyncing(false);
    }
  }, [urlParams.symbol, setUrlParams]);

  // date navigation — dates 按日期倒序（最新在前），offset +1 = 更旧
  const navByOffset = useCallback((offset: number) => {
    const idx = dates.findIndex((d) => d.date === urlParams.date);
    const newIdx = idx + offset;
    if (newIdx >= 0 && newIdx < dates.length) {
      setUrlParams({ date: dates[newIdx].date });
    }
  }, [dates, urlParams.date, setUrlParams]);

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100vh',
      background: 'var(--canvas)', color: 'var(--text)',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
      fontSize: typography.base,
      lineHeight: 1.5,
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: '4px',
        padding: '10px 18px', background: 'rgba(255, 255, 255, .96)',
        borderBottom: '1px solid var(--border)', boxShadow: '0 1px 12px rgba(26, 53, 77, .05)', flexShrink: 0,
      }}>
        <div style={{
          color: 'var(--text)', fontSize: typography.lg, fontWeight: 750,
          marginRight: 14, letterSpacing: '.02em', fontFamily: 'ui-serif, "Songti SC", "Noto Serif SC", serif',
        }}>
          玄龄量化回测系统
        </div>
        {[
          { key: 'backtest' as const, label: '策略回测' },
          { key: 'signals' as const, label: '信号可视化' },
          { key: 'data' as const, label: '数据管理' },
          ...(currentUser.role === 'admin' ? [{ key: 'users' as const, label: '人员管理' }] : []),
        ].map((item) => {
          const active = activePage === item.key;
          return (
            <button
              key={item.key}
              onClick={() => setActivePage(item.key)}
              style={{
                height: 32, padding: '0 12px', borderRadius: 5,
                border: `1px solid ${active ? 'var(--accent)' : 'transparent'}`,
                background: active ? 'var(--accent-soft)' : 'transparent',
                color: active ? 'var(--accent-strong)' : 'var(--text-muted)',
                fontSize: typography.base, cursor: 'pointer',
              }}
            >
              {item.label}
            </button>
          );
        })}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: typography.sm }}>
          <span>{currentUser.displayName} · {currentUser.role === 'admin' ? '管理员' : '普通用户'}</span>
          <button onClick={onLogout} style={{ border: '1px solid var(--border-strong)', borderRadius: 5, padding: '5px 10px', background: 'var(--surface)', color: 'var(--text-muted)', cursor: 'pointer' }}>退出</button>
        </div>
      </div>

      {activePage === 'signals' ? (
        <>
          <Toolbar
            symbol={urlParams.symbol}
            date={urlParams.date}
            strategy={signalStrategy}
            strategies={signalStrategies}
            signalFile={signalFile}
            signalFiles={signalFiles}
            instruments={instruments}
            dates={dates}
            loading={loading}
            onSymbolChange={(s) => setUrlParams({ symbol: s })}
            onDateChange={(d) => setUrlParams({ date: d })}
            onStrategyChange={(s) => setUrlParams({ strategy: s })}
            onSignalFileChange={setSignalFile}
            onPrevDay={() => navByOffset(1)}
            onNextDay={() => navByOffset(-1)}
            onRefresh={loadChart}
            onSync={handleSync}
            syncing={syncing}
          />

          <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
            <Sidebar
              modules={urlParams.modules}
              sides={urlParams.sides}
              prefs={prefs}
              onModulesChange={(m) => setUrlParams({ modules: m })}
              onSidesChange={(s) => setUrlParams({ sides: s })}
              onPrefsChange={handlePrefsChange}
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
                {prefs.comparisonMode ? (
                  <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: '2px' }}>
                    <div style={{ flex: 1, position: 'relative' }}>
                      <KlineChart
                        data={chartData}
                        modules={urlParams.modules}
                        sides={urlParams.sides}
                        showPrices={prefs.showPrices}
                        markerSize={prefs.markerSize}
                        label="实盘实际触发"
                        labelColor="#14263a"
                      />
                    </div>
                    <div style={{ flex: 1, position: 'relative' }}>
                      <KlineChart
                        data={chartData}
                        modules={urlParams.modules}
                        sides={urlParams.sides}
                        showPrices={prefs.showPrices}
                        markerSize={prefs.markerSize}
                        signalSource="theoretical"
                        label="策略标准执行"
                        labelColor="#a16207"
                      />
                    </div>
                  </div>
                ) : (
                  <KlineChart
                    data={chartData}
                    modules={urlParams.modules}
                    sides={urlParams.sides}
                    showPrices={prefs.showPrices}
                    markerSize={prefs.markerSize}
                  />
                )}
              </div>
            </div>
          </div>

          <StatusBar data={chartData} error={error} loading={loading} />
        </>
      ) : activePage === 'backtest' ? (
        <BacktestPage
          symbol={urlParams.symbol}
          strategy={urlParams.strategy}
          strategies={strategies}
          instruments={instruments}
          isAdmin={currentUser.role === 'admin'}
        />
      ) : activePage === 'data' ? (
        <DataPage
          symbol={urlParams.symbol}
          instruments={instruments}
          onInstrumentsChange={setInstruments}
        />
      ) : (
        <UserManagementPage />
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
