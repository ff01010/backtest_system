import { useEffect, useState } from 'react';
import { fetchStrategyRuntimeErrorStats, fetchSystemErrorLogs, fetchUsers } from '../api/client';
import type { ManagedUser, StrategyRuntimeErrorStat, SystemErrorLogEntry } from '../api/client';

interface Filters {
  userId: string;
  runId: string;
  errorCode: string;
  fromTime: string;
  toTime: string;
}

const emptyFilters: Filters = {
  userId: '', runId: '', errorCode: '', fromTime: '', toTime: '',
};

function toIso(value: string): string | undefined {
  return value ? new Date(value).toISOString() : undefined;
}

export default function SystemErrorLogPage() {
  const [items, setItems] = useState<SystemErrorLogEntry[]>([]);
  const [runtimeStats, setRuntimeStats] = useState<StrategyRuntimeErrorStat[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const refresh = async () => {
    setLoading(true);
    try {
      const timeFilters = {
        fromTime: toIso(filters.fromTime),
        toTime: toIso(filters.toTime),
      };
      const [nextItems, nextRuntimeStats] = await Promise.all([
        fetchSystemErrorLogs({
          userId: filters.userId || undefined,
          runId: filters.runId.trim() || undefined,
          errorCode: filters.errorCode.trim() || undefined,
          ...timeFilters,
        }),
        fetchStrategyRuntimeErrorStats(timeFilters),
      ]);
      setItems(nextItems);
      setRuntimeStats(nextRuntimeStats);
      setSelectedId(null);
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取系统错误日志');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchUsers().then(setUsers).catch(() => {});
    void refresh();
  }, []);

  const selected = items.find((item) => item.id === selectedId) || null;

  return (
    <section style={pageStyle}>
      <div style={contentStyle}>
        <header style={headerStyle}>
          <div>
            <div style={eyebrowStyle}>SYSTEM OBSERVABILITY</div>
            <h2 style={{ margin: '5px 0 4px', color: 'var(--text)' }}>系统错误日志</h2>
            <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: 13 }}>仅记录回测系统错误；策略脚本错误仍在策略回测页展示。</p>
          </div>
          <button type="button" disabled={loading} onClick={() => void refresh()} style={buttonStyle}>{loading ? '读取中…' : '刷新日志'}</button>
        </header>

        <div style={filterStyle}>
          <label style={fieldStyle}>使用者<select value={filters.userId} onChange={(event) => setFilters({ ...filters, userId: event.target.value })} style={inputStyle}>
            <option value="">全部用户</option>{users.map((user) => <option key={user.id} value={user.id}>{user.displayName} · {user.username}</option>)}
          </select></label>
          <label style={fieldStyle}>任务 UUID<input value={filters.runId} onChange={(event) => setFilters({ ...filters, runId: event.target.value })} placeholder="完整任务编号" style={inputStyle} /></label>
          <label style={fieldStyle}>错误代码<input value={filters.errorCode} onChange={(event) => setFilters({ ...filters, errorCode: event.target.value })} placeholder="如 clickhouse.host_unresolved" style={inputStyle} /></label>
          <label style={fieldStyle}>开始时间<input type="datetime-local" value={filters.fromTime} onChange={(event) => setFilters({ ...filters, fromTime: event.target.value })} style={inputStyle} /></label>
          <label style={fieldStyle}>结束时间<input type="datetime-local" value={filters.toTime} onChange={(event) => setFilters({ ...filters, toTime: event.target.value })} style={inputStyle} /></label>
          <button type="button" onClick={() => void refresh()} style={{ ...buttonStyle, alignSelf: 'end' }}>筛选</button>
          <button type="button" onClick={() => { setFilters(emptyFilters); }} style={secondaryButtonStyle}>重置</button>
        </div>

        {error ? <div style={errorStyle}>{error}</div> : null}
        <section style={runtimePanelStyle}>
          <div>
            <div style={eyebrowStyle}>RUNTIME COMPATIBILITY</div>
            <h3 style={{ margin: '4px 0', color: 'var(--text)' }}>策略运行环境错误统计</h3>
            <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: 12 }}>默认保留 180 天，仅含平台错误码、运行时版本、受控模块名称和次数，无法反查用户或任务。</p>
          </div>
          <div style={runtimeGridStyle}>
            {runtimeStats.map((item) => <div key={`${item.date}-${item.code}-${item.runtimeVersion}-${item.missingModule || item.missingName || ''}`} style={runtimeCardStyle}>
              <strong style={{ color: 'var(--text)' }}>{item.missingModule || item.missingName || '版本或依赖异常'} · {item.count} 次</strong>
              <span style={subtextStyle}>{item.date} · {item.code}</span>
              <span style={monoStyle}>{item.runtimeVersion}</span>
              <span style={subtextStyle}>最近：{item.lastOccurredAt}</span>
            </div>)}
            {!loading && runtimeStats.length === 0 ? <div style={emptyRuntimeStyle}>当前时间范围内没有平台运行环境错误。</div> : null}
          </div>
        </section>
        <div style={tablePanelStyle}>
          <table style={tableStyle}>
            <thead><tr style={tableHeadStyle}>
              <th style={cellStyle}>发生时间</th><th style={cellStyle}>使用者</th><th style={cellStyle}>任务</th><th style={cellStyle}>标的 / 周期</th><th style={cellStyle}>阶段</th><th style={cellStyle}>分类 / 重试</th><th style={cellStyle}>管理员原因</th>
            </tr></thead>
            <tbody>{items.map((item) => <tr key={item.id} onClick={() => setSelectedId(item.id)} style={{ ...rowStyle, background: selectedId === item.id ? 'var(--surface-muted)' : 'transparent' }}>
              <td style={cellStyle}>{item.occurredAt}</td>
              <td style={cellStyle}><strong>{item.displayName}</strong><span style={subtextStyle}>{item.username}</span></td>
              <td style={cellStyle}><strong>#{item.displayNo}</strong><span style={monoStyle}>{item.runId}</span></td>
              <td style={cellStyle}>{item.symbol || '—'}<span style={subtextStyle}>{item.timeframe || '—'}</span></td>
              <td style={cellStyle}><span style={codeBadgeStyle}>{item.stage}</span><span style={subtextStyle}>{item.code}</span></td>
              <td style={cellStyle}><span style={categoryBadgeStyle}>{item.category}</span><span style={{ ...subtextStyle, color: item.retryable ? '#9a5d00' : 'var(--text-muted)' }}>{item.retryable ? '可重试' : '不可直接重试'}</span></td>
              <td style={{ ...cellStyle, maxWidth: 340 }}>{item.reason}</td>
            </tr>)}</tbody>
          </table>
          {!loading && items.length === 0 ? <div style={emptyStyle}>当前筛选条件下没有系统错误日志。</div> : null}
        </div>

        {selected ? <section style={detailStyle}>
          <div><div style={eyebrowStyle}>SAFE TECHNICAL DETAIL</div><h3 style={{ margin: '4px 0', color: 'var(--text)' }}>{selected.reason}</h3></div>
          <div style={detailGridStyle}>
            <Detail label="异常类型" value={selected.exceptionType} /><Detail label="来源" value={selected.source} />
            <Detail label="任务 UUID" value={selected.runId} mono /><Detail label="错误代码" value={selected.code} mono />
            <Detail label="错误分类" value={selected.category} mono /><Detail label="是否可重试" value={selected.retryable ? '是（瞬态错误）' : '否（需先处理原因）'} />
            <Detail label="原始日志索引" value={selected.rawLogRef} mono />
            {Object.entries(selected.context).map(([key, value]) => <Detail key={key} label={key} value={value} mono />)}
          </div>
          <p style={{ margin: '14px 0 0', color: 'var(--text-muted)', fontSize: 12 }}>原始堆栈仅保留在服务器容器日志中。使用“原始日志索引”可在受控运维环境查询对应任务与阶段，页面不直接展示敏感日志正文。</p>
        </section> : null}
      </div>
    </section>
  );
}

function Detail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><span style={subtextStyle}>{label}</span><strong style={{ display: 'block', marginTop: 3, color: 'var(--text)', fontFamily: mono ? 'ui-monospace, Consolas, monospace' : 'inherit', fontSize: 13 }}>{value || '—'}</strong></div>;
}

const pageStyle = { padding: 28, overflow: 'auto' as const, height: '100%', background: 'var(--canvas)' };
const contentStyle = { maxWidth: 1480, margin: '0 auto' };
const headerStyle = { display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 18, marginBottom: 18 };
const eyebrowStyle = { color: 'var(--accent)', fontSize: 12, letterSpacing: 1.5, fontWeight: 750 };
const filterStyle = { display: 'grid', gridTemplateColumns: 'minmax(150px, 1fr) minmax(210px, 1.25fr) minmax(210px, 1.25fr) minmax(170px, 1fr) minmax(170px, 1fr) auto auto', gap: 10, alignItems: 'end', marginBottom: 14, padding: 14, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10 };
const fieldStyle = { display: 'grid', gap: 5, color: 'var(--text-muted)', fontSize: 12 };
const inputStyle = { width: '100%', height: 34, boxSizing: 'border-box' as const, borderRadius: 6, border: '1px solid var(--border-strong)', background: 'var(--surface-muted)', color: 'var(--text)', padding: '0 9px' };
const buttonStyle = { height: 34, border: 0, borderRadius: 6, padding: '0 13px', background: 'var(--accent)', color: '#ffffff', fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap' as const };
const secondaryButtonStyle = { ...buttonStyle, alignSelf: 'end', background: 'transparent', border: '1px solid var(--border-strong)', color: 'var(--text-muted)' };
const errorStyle = { marginBottom: 14, border: '1px solid #f6c7c2', borderRadius: 7, background: 'var(--danger-soft)', color: 'var(--danger)', padding: '9px 12px' };
const runtimePanelStyle = { marginBottom: 14, padding: 14, border: '1px solid var(--border)', borderLeft: '3px solid var(--accent)', borderRadius: 10, background: 'var(--surface)' };
const runtimeGridStyle = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: 10, marginTop: 12 };
const runtimeCardStyle = { display: 'grid', gap: 2, padding: 11, border: '1px solid var(--border)', borderRadius: 7, background: 'var(--surface-muted)' };
const emptyRuntimeStyle = { padding: 12, color: 'var(--text-muted)', fontSize: 13 };
const tablePanelStyle = { overflow: 'auto', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' };
const tableStyle = { width: '100%', minWidth: 1050, borderCollapse: 'collapse' as const, color: 'var(--text)', fontSize: 13 };
const tableHeadStyle = { background: 'var(--surface-muted)', color: 'var(--text-muted)', textAlign: 'left' as const };
const rowStyle = { borderTop: '1px solid var(--border)', cursor: 'pointer' };
const cellStyle = { padding: '11px 13px', textAlign: 'left' as const, verticalAlign: 'top' as const, fontWeight: 'normal' };
const subtextStyle = { display: 'block', marginTop: 3, color: 'var(--text-muted)', fontSize: 11 };
const monoStyle = { ...subtextStyle, fontFamily: 'ui-monospace, Consolas, monospace', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' as const, whiteSpace: 'nowrap' as const };
const codeBadgeStyle = { display: 'inline-block', borderRadius: 4, background: '#edf4f6', color: '#17605a', fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 11, padding: '3px 5px' };
const categoryBadgeStyle = { display: 'inline-block', borderRadius: 999, background: '#fff3da', color: '#895500', fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 11, padding: '3px 7px' };
const emptyStyle = { padding: 38, color: 'var(--text-muted)', textAlign: 'center' as const };
const detailStyle = { marginTop: 14, border: '1px solid var(--border)', borderLeft: '3px solid var(--accent)', borderRadius: 9, background: 'var(--surface)', padding: 16 };
const detailGridStyle = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 14, marginTop: 14 };
