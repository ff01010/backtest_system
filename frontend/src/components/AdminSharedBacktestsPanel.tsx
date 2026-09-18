import { useEffect, useState } from 'react';
import {
  fetchAdminSharedBacktests,
  removeSharedBacktest,
  restoreSharedBacktest,
} from '../api/client';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';
import {
  MANAGEMENT_PAGE_SIZE,
  managementErrorMessage,
  managementStatusLabels,
  nextManagementItems,
  SharedBacktestManagementPagination,
  SharedBacktestStatusFilter,
} from './SharedBacktestManagementUi';

interface Props {
  onPublicationChanged: (publication: SharedBacktestManagementItem) => void;
}

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
    fetchAdminSharedBacktests(MANAGEMENT_PAGE_SIZE, offset, status || undefined)
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setTotal(page.total);
      })
      .catch((problem: unknown) => {
        if (!cancelled) setError(managementErrorMessage(problem, '共享回测管理列表加载失败'));
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
      setError(managementErrorMessage(problem, '共享回测管理员状态更新失败'));
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
    setItems((current) => nextManagementItems(current, updated, status));
  }

  return (
    <section aria-label="共享回测管理" className="shared-management">
      <SharedBacktestStatusFilter
        ariaLabel="管理员共享状态"
        value={status}
        onChange={(value) => { setStatus(value); setOffset(0); }}
      />
      {loading && <div className="shared-state">正在加载共享回测管理列表...</div>}
      {error && <div role="alert" className="shared-alert is-error">{error}</div>}
      {!loading && items.length === 0 && <div className="shared-empty-state">暂无共享记录。</div>}
      {items.map((item) => (
        <article key={item.id} className="shared-management-card">
          <div className="shared-management-card-head">
            <div>
              <div className="shared-management-title-row">
                <h2>{item.title}</h2>
                <span className="shared-status-chip" data-status={item.status}>{managementStatusLabels[item.status]}</span>
              </div>
              <div className="shared-management-meta">
                作者 {item.authorName} · {item.symbol} · {item.timeframe}
              </div>
            </div>
            <div className="shared-management-actions">
              {item.status === 'active' && (
                <button type="button" className="shared-button is-danger" disabled={saving} onClick={() => beginRemove(item)}>下架</button>
              )}
              {item.status === 'removed' && (
                <button type="button" className="shared-button is-primary" disabled={saving} onClick={() => void restore(item)}>恢复上架</button>
              )}
            </div>
          </div>
          {item.status === 'removed' && (
            <p className="shared-management-note is-error">
              下架时间：{item.removedAt || '--'}；原因：{item.removalReason || '--'}
            </p>
          )}
        </article>
      ))}
      <SharedBacktestManagementPagination
        total={total}
        offset={offset}
        onOffsetChange={setOffset}
      />
      {removing && (
        <div className="shared-modal-backdrop">
          <div role="dialog" aria-modal="true" aria-label="下架共享回测" className="shared-modal is-compact">
            <form onSubmit={(event) => void submitRemove(event)} className="shared-form">
              <div className="shared-modal-heading">
                <span>ADMIN ACTION</span>
                <h2>下架“{removing.title}”</h2>
              </div>
              <label className="shared-field">
                <span className="shared-field-label">下架原因</span>
                <textarea className="shared-textarea" required maxLength={1000} rows={5} value={reason} onChange={(event) => setReason(event.target.value)} />
              </label>
              <p className="shared-modal-note">下架后普通用户无法查看或复制，作者可在“我的共享”查看此原因。</p>
              <div className="shared-modal-actions">
                <button type="button" className="shared-button is-quiet" disabled={saving} onClick={() => setRemoving(null)}>取消</button>
                <button type="submit" className="shared-button is-danger-solid" disabled={saving || !reason.trim()}>{saving ? '下架中...' : '确认下架'}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
