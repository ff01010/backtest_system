import { Database, DownloadCloud, RefreshCw } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  fetchDataStatus,
  fetchDataSyncRuns,
  fetchDataSyncTask,
  refillDataGaps,
  runDataSync,
  startDataSyncTask,
} from '../api/client';
import type { DataStatus, DataSyncRun, DataSyncTask, InstrumentItem } from '../types';
import { typography } from '../styles/typography';
import InstrumentSearchField from './InstrumentSearchField';

interface Props {
  symbol: string;
  instruments: InstrumentItem[];
  onInstrumentsChange: (items: InstrumentItem[]) => void;
}

const panelStyle = {
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  boxShadow: '0 4px 18px rgba(26, 53, 77, .04)',
} as const;

const labelStyle = {
  color: 'var(--text-muted)',
  fontSize: 13,
  marginBottom: 6,
} as const;

const fieldStyle = {
  width: '100%',
  height: 34,
  background: 'var(--surface-muted)',
  color: 'var(--text)',
  border: '1px solid var(--border-strong)',
  borderRadius: 4,
  padding: '0 10px',
  fontSize: 14,
} as const;

export default function DataPage({ symbol, instruments, onInstrumentsChange }: Props) {
  const [selectedSymbol, setSelectedSymbol] = useState(symbol);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([symbol]);
  const [timeframe, setTimeframe] = useState('5m');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [mode, setMode] = useState<'incremental' | 'range'>('incremental');
  const [statuses, setStatuses] = useState<Record<string, DataStatus>>({});
  const [runs, setRuns] = useState<DataSyncRun[]>([]);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [activeTask, setActiveTask] = useState<DataSyncTask | null>(null);

  const enabledInstruments = useMemo(
    () => instruments.filter((item) => item.enabled !== false),
    [instruments]
  );
  const selectedStatus = statuses[selectedSymbol];

  useEffect(() => {
    refreshRuns();
  }, []);

  useEffect(() => {
    if (enabledInstruments.length > 0) {
      refreshStatuses(enabledInstruments.map((item) => item.symbol));
    }
  }, [enabledInstruments.length, timeframe]);

  const refreshRuns = async () => {
    setRuns(await fetchDataSyncRuns(20));
  };

  const refreshStatuses = async (symbols: string[]) => {
    setLoading(true);
    setError('');
    try {
      const entries = await Promise.all(
        symbols.map(async (item) => [item, await fetchDataStatus(item, timeframe)] as const)
      );
      setStatuses((prev) => ({
        ...prev,
        ...Object.fromEntries(entries),
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新数据状态失败');
    } finally {
      setLoading(false);
    }
  };

  const toggleSymbol = (item: string) => {
    setSelectedSymbols((prev) => (
      prev.includes(item)
        ? prev.filter((x) => x !== item)
        : [...prev, item]
    ));
  };

  const syncPayload = (item: string) => ({
    symbol: item,
    timeframe,
    startDate: startDate.trim() || null,
    endDate: endDate.trim() || null,
    mode,
  });

  const handleSyncOne = async () => {
    setSyncing(true);
    setError('');
    setStatus('');
    try {
      const result = await runDataSync(syncPayload(selectedSymbol));
      setStatus(`${result.symbol} QMT 数据覆盖检查${result.status === 'ok' ? '通过' : '失败'}`);
      await refreshStatuses([selectedSymbol]);
      await refreshRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message : '覆盖检查失败');
    } finally {
      setSyncing(false);
    }
  };

  const waitTask = async (taskId: string) => {
    for (;;) {
      const task = await fetchDataSyncTask(taskId);
      setActiveTask(task);
      if (!['queued', 'running'].includes(task.status)) {
        return task;
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  };

  const handleSyncBatch = async () => {
    setSyncing(true);
    setError('');
    setStatus('');
    try {
      const task = await startDataSyncTask({
        symbols: selectedSymbols,
        timeframe,
        startDate: startDate.trim() || null,
        endDate: endDate.trim() || null,
        mode,
      });
      setActiveTask(task);
      setStatus(`后台覆盖检查任务已启动：${task.id.slice(0, 8)}`);
      const done = await waitTask(task.id);
      const results = done.results;
      const ok = results.filter((item) => item.status !== 'error').length;
      setStatus(`批量覆盖检查完成：${ok}/${results.length} 通过`);
      await refreshStatuses(selectedSymbols);
      await refreshRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message : '批量覆盖检查失败');
    } finally {
      setSyncing(false);
    }
  };

  const handleRefillGaps = async () => {
    if (!selectedStatus || selectedStatus.gapDates.length === 0) return;
    setSyncing(true);
    setError('');
    setStatus('');
    try {
      const task = await refillDataGaps({
        symbol: selectedSymbol,
        timeframe,
        dates: selectedStatus.gapDates.map((item) => item.date),
      });
      setActiveTask(task);
      setStatus(`缺口覆盖检查任务已启动：${task.id.slice(0, 8)}`);
      await waitTask(task.id);
      await refreshStatuses([selectedSymbol]);
      await refreshRuns();
      setStatus('缺口覆盖检查完成');
    } catch (e) {
      setError(e instanceof Error ? e.message : '缺口覆盖检查失败');
    } finally {
      setSyncing(false);
    }
  };

  const formatSize = (value: number) => {
    if (value <= 0) return '--';
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(2)} MB`;
  };

  return (
    <div style={{
      flex: 1, overflow: 'auto', background: 'var(--canvas)',
      padding: 16, display: 'grid',
      gridTemplateColumns: '330px minmax(0, 1fr)',
      gap: 16,
    }}>
      <aside style={{ ...panelStyle, padding: 14, alignSelf: 'start' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
          <Database size={16} color="#0f766e" />
          <div style={{ color: 'var(--text)', fontSize: typography.xl, fontWeight: 700 }}>
            数据管理
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <InstrumentSearchField
            label="当前标的"
            value={selectedSymbol}
            onChange={(item) => {
              setSelectedSymbol(item);
              setSelectedSymbols((prev) => [...new Set([...prev, item])]);
            }}
          />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label>
              <div style={labelStyle}>周期</div>
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} style={fieldStyle}>
                {['1m', '2m', '3m', '4m', '5m', '10m', '15m', '30m', '45m', '60m', '1d'].map((item) => (
                  <option key={item} value={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              <div style={labelStyle}>检查模式</div>
              <select value={mode} onChange={(e) => setMode(e.target.value as 'incremental' | 'range')} style={fieldStyle}>
                <option value="incremental">增量</option>
                <option value="range">覆盖区间</option>
              </select>
            </label>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label>
              <div style={labelStyle}>开始日期</div>
              <input value={startDate} onChange={(e) => setStartDate(e.target.value)} placeholder="YYYYMMDD" style={fieldStyle} />
            </label>
            <label>
              <div style={labelStyle}>结束日期</div>
              <input value={endDate} onChange={(e) => setEndDate(e.target.value)} placeholder="YYYYMMDD" style={fieldStyle} />
            </label>
          </div>

          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={handleSyncOne} disabled={syncing} style={primaryButtonStyle}>
              <DownloadCloud size={14} />
              {syncing ? '检查中...' : '检查当前覆盖'}
            </button>
            <button
              onClick={() => refreshStatuses(enabledInstruments.map((item) => item.symbol))}
              disabled={loading}
              style={{ ...iconButtonStyle, width: 38 }}
              title="刷新状态"
            >
              <RefreshCw size={14} />
            </button>
          </div>

          <button onClick={handleSyncBatch} disabled={syncing || selectedSymbols.length === 0} style={secondaryButtonStyle}>
            批量检查已选 {selectedSymbols.length}
          </button>

          <button
            onClick={handleRefillGaps}
            disabled={syncing || !selectedStatus || selectedStatus.gapDates.length === 0}
            style={secondaryButtonStyle}
          >
            检查当前缺口 {selectedStatus?.gapDates.length || 0}
          </button>

          {activeTask && (
            <div style={{
              color: 'var(--text-muted)',
              background: 'var(--surface-muted)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              padding: '8px 10px',
              fontSize: 12,
            }}>
              任务 {activeTask.id.slice(0, 8)} | {activeTask.status}
              {activeTask.results.length > 0 ? ` | ${activeTask.results.length} 项` : ''}
            </div>
          )}

          {(status || error) && (
            <div style={{
              color: error ? 'var(--danger)' : 'var(--success)',
              background: error ? 'var(--danger-soft)' : 'var(--surface-accent)',
              border: `1px solid ${error ? '#f3c6c0' : '#b5e0d6'}`,
              borderRadius: 4, padding: '8px 10px', fontSize: 12,
            }}>
              {error || status}
            </div>
          )}
        </div>
      </aside>

      <main style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
        <section style={{ ...panelStyle, padding: 14 }}>
          <div style={{ color: 'var(--text)', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
            本地数据状态
          </div>
          <div style={{
            display: 'grid',
            gridTemplateColumns: '32px 130px 70px 90px 90px 90px 100px 90px 1fr',
            color: 'var(--text-muted)', fontSize: 12, borderBottom: '1px solid var(--border)',
            paddingBottom: 8,
          }}>
            <span />
            <span>标的</span>
            <span>周期</span>
            <span>最早</span>
            <span>最新</span>
            <span>Bars</span>
            <span>大小</span>
            <span>缺口</span>
            <span>路径</span>
          </div>
          {enabledInstruments.map((item) => {
            const data = statuses[item.symbol];
            const selected = item.symbol === selectedSymbol;
            return (
              <div
                key={item.symbol}
                onClick={() => setSelectedSymbol(item.symbol)}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '32px 130px 70px 90px 90px 90px 100px 90px 1fr',
                  alignItems: 'center',
                  color: selected ? 'var(--text)' : '#40556a',
                  fontSize: 12,
                  borderBottom: '1px solid var(--border)',
                  padding: '8px 0',
                  cursor: 'pointer',
                  background: selected ? 'var(--surface-accent)' : 'transparent',
                }}
              >
                <input
                  type="checkbox"
                  checked={selectedSymbols.includes(item.symbol)}
                  onChange={() => toggleSymbol(item.symbol)}
                  onClick={(e) => e.stopPropagation()}
                />
                <span>{item.symbol}</span>
                <span>{timeframe}</span>
                <span>{data?.firstDate || '--'}</span>
                <span>{data?.lastDate || '--'}</span>
                <span>{data?.barCount ?? '--'}</span>
                <span>{data ? formatSize(data.fileSize) : '--'}</span>
                <span style={{ color: data && data.gapDates.length > 0 ? 'var(--warning)' : 'var(--text-muted)' }}>
                  {data ? data.gapDates.length : '--'}
                </span>
                <span title={data?.path || ''} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {data?.exists ? data.path : '无文件'}
                </span>
              </div>
            );
          })}
        </section>

        <section style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 360px', gap: 16 }}>
          <div style={{ ...panelStyle, padding: 14, minWidth: 0 }}>
            <div style={{ color: 'var(--text)', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
              最近 20 行 K 线
            </div>
            {selectedStatus?.preview?.length ? (
              <div style={{ overflowX: 'auto' }}>
                <div style={{
                  display: 'grid', gridTemplateColumns: '150px repeat(5, 90px)',
                  color: 'var(--text-muted)', fontSize: 12, paddingBottom: 8,
                  borderBottom: '1px solid var(--border)',
                }}>
                  <span>time</span><span>open</span><span>high</span><span>low</span><span>close</span><span>volume</span>
                </div>
                {selectedStatus.preview.map((row) => (
                  <div
                    key={row.time}
                    style={{
                      display: 'grid', gridTemplateColumns: '150px repeat(5, 90px)',
                      color: '#40556a', fontSize: 12,
                      borderBottom: '1px solid var(--border)', padding: '7px 0',
                    }}
                  >
                    <span>{row.time}</span>
                    <span>{Number(row.open).toFixed(2)}</span>
                    <span>{Number(row.high).toFixed(2)}</span>
                    <span>{Number(row.low).toFixed(2)}</span>
                    <span>{Number(row.close).toFixed(2)}</span>
                    <span>{row.volume}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '20px 0' }}>
                选择有本地数据的标的后显示预览
              </div>
            )}
          </div>

          <div style={{ ...panelStyle, padding: 14 }}>
            <div style={{ color: 'var(--text)', fontSize: typography.lg, fontWeight: 700, marginBottom: 12 }}>
              最近覆盖检查
            </div>
            {runs.length > 0 ? runs.map((run) => (
              <div key={run.id} style={{
                borderBottom: '1px solid var(--border)',
                padding: '8px 0', fontSize: 12, color: '#40556a',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                  <span>{run.symbol} {run.timeframe}</span>
                  <span style={{ color: run.status === 'error' ? 'var(--danger)' : 'var(--success)' }}>{run.status}</span>
                </div>
                <div style={{ color: 'var(--text-muted)', marginTop: 3 }}>
                  {run.createdAt} | 新增 {run.newRows} 行
                </div>
                {run.message && <div style={{ color: 'var(--warning)', marginTop: 3 }}>{run.message}</div>}
                {(run.sourceTimeframe || run.elapsedMs > 0) && (
                  <div style={{ color: 'var(--text-muted)', marginTop: 3 }}>
                    {run.sourceTimeframe ? `源周期 ${run.sourceTimeframe} | ` : ''}
                    {run.elapsedMs > 0 ? `${run.elapsedMs} ms` : ''}
                  </div>
                )}
              </div>
            )) : (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '12px 0' }}>
                暂无覆盖检查记录
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

const iconButtonStyle = {
  width: 36,
  height: 34,
  borderRadius: 4,
  border: '1px solid var(--border-strong)',
  background: 'var(--surface-muted)',
  color: 'var(--accent)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  cursor: 'pointer',
} as const;

const primaryButtonStyle = {
  flex: 1,
  height: 34,
  borderRadius: 4,
  border: '1px solid var(--accent)',
  background: 'var(--accent)',
  color: '#ffffff',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
  cursor: 'pointer',
} as const;

const secondaryButtonStyle = {
  height: 34,
  borderRadius: 4,
  border: '1px solid var(--border-strong)',
  background: 'var(--surface)',
  color: 'var(--text-muted)',
  cursor: 'pointer',
} as const;
