import { useEffect, useState } from 'react';
import {
  fetchAdminSharedBacktests,
  removeSharedBacktest,
  restoreSharedBacktest,
} from '../api/client';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';

interface Props {
  onPublicationChanged: (publication: SharedBacktestManagementItem) => void;
}

const PAGE_SIZE = 20;

const statusLabels: Record<SharedBacktestStatus, string> = {
  active: '已上架',
  withdrawn: '已撤回',
  removed: '已下架',
};

const fieldStyle = {
  width: '100%',
  boxSizing: 'border-box',
  border: '1px solid #cbd5e1',
  borderRadius: 6,
  padding: '8px 10px',
} as const;

export default function AdminSharedBacktestsPanel({ onPublicationChanged }: Props) {
  const [items, setItems] = useState<SharedBacktestManagementItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<SharedBacktestStatus | ''>('');
  const [removing, setRemoving] = useState<SharedBacktestManagementItem | null>(null);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchAdminSharedBacktests(PAGE_SIZE, offset, status || undefined)
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setTotal(page.total);
      })
      .catch((problem: unknown) => {
        if (!cancelled) setError(errorMessage(problem, '共享回测管理列表加载失败'));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [offset, status]);

  function beginRemove(item: SharedBacktestManagementItem) {
    setRemoving(item);
    setReason('');
    setError('');
  }

  async function submitRemove(event: React.FormEvent) {
    event.preventDefault();
    if (!removing || !reason.trim()) return;
    const changed = await transition(() => removeSharedBacktest(removing.id, reason.trim()));
    if (changed) {
      setRemoving(null);
      setReason('');
    }
  }

  async function restore(item: SharedBacktestManagementItem) {
    await transition(() => restoreSharedBacktest(item.id));
  }

  async function transition(action: () => Promise<SharedBacktestManagementItem>): Promise<boolean> {
    setSaving(true);
    setError('');
    try {
      const updated = await action();
      replaceItem(updated);
      onPublicationChanged(updated);
      return true;
    } catch (problem) {
      setError(errorMessage(problem, '共享回测管理员状态更新失败'));
      return false;
    } finally {
      setSaving(false);
    }
  }

  function replaceItem(updated: SharedBacktestManagementItem) {
    if (status && updated.status !== status) {
      setItems((current) => current.filter((item) => item.id !== updated.id));
      setTotal((current) => Math.max(0, current - 1));
      return;
    }
    setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
  }

  const cardStyle = {
    border: '1px solid #d7dde8', borderRadius: 8, background: '#ffffff', padding: 18,
  } as const;

  return (
    <section aria-label="共享回测管理" style={{ display: 'grid', gap: 14 }}>
      <label style={{ maxWidth: 220 }}>
        状态
        <select
          aria-label="管理员共享状态"
          value={status}
          onChange={(event) => { setStatus(event.target.value as SharedBacktestStatus | ''); setOffset(0); }}
          style={fieldStyle}
        >
          <option value="">全部状态</option>
          <option value="active">已上架</option>
          <option value="withdrawn">已撤回</option>
          <option value="removed">已下架</option>
        </select>
      </label>
      {loading && <div>正在加载共享回测管理列表...</div>}
      {error && <div role="alert" style={{ color: '#b91c1c' }}>{error}</div>}
      {!loading && items.length === 0 && <div style={cardStyle}>暂无共享记录。</div>}
      {items.map((item) => (
        <article key={item.id} style={cardStyle}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
            <div>
              <h2 style={{ margin: 0, fontSize: 18 }}>{item.title}</h2>
              <div style={{ marginTop: 6, color: '#64748b' }}>
                {statusLabels[item.status]} · 作者 {item.authorName} · {item.symbol} · {item.timeframe}
              </div>
            </div>
            <div>
              {item.status === 'active' && (
                <button type="button" disabled={saving} onClick={() => beginRemove(item)}>下架</button>
              )}
              {item.status === 'removed' && (
                <button type="button" disabled={saving} onClick={() => void restore(item)}>恢复上架</button>
              )}
            </div>
          </div>
          {item.status === 'removed' && (
            <p style={{ color: '#b91c1c' }}>
              下架时间：{item.removedAt || '--'}；原因：{item.removalReason || '--'}
            </p>
          )}
        </article>
      ))}
      {total > PAGE_SIZE && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
          <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(total / PAGE_SIZE)}</span>
          <button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
        </div>
      )}
      {removing && (
        <div role="dialog" aria-modal="true" aria-label="下架共享回测" style={{ ...cardStyle, position: 'fixed', inset: '20% 25%', zIndex: 20, boxShadow: '0 18px 48px #0f172a55' }}>
          <form onSubmit={(event) => void submitRemove(event)} style={{ display: 'grid', gap: 12 }}>
            <h2 style={{ margin: 0 }}>下架“{removing.title}”</h2>
            <label>
              下架原因
              <textarea required maxLength={1000} rows={5} value={reason} onChange={(event) => setReason(event.target.value)} style={fieldStyle} />
            </label>
            <p style={{ margin: 0, color: '#64748b' }}>下架后普通用户无法查看或复制，作者可在“我的共享”查看此原因。</p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button type="button" disabled={saving} onClick={() => setRemoving(null)}>取消</button>
              <button type="submit" disabled={saving || !reason.trim()}>{saving ? '下架中...' : '确认下架'}</button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}
