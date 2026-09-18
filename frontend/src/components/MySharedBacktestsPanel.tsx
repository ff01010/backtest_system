import { useEffect, useState } from 'react';
import {
  fetchMySharedBacktests,
  relistSharedBacktest,
  updateSharedBacktestMetadata,
  withdrawSharedBacktest,
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
  onPublicationChanged: () => void;
}

export default function MySharedBacktestsPanel({ onPublicationChanged }: Props) {
  const [items, setItems] = useState<SharedBacktestManagementItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<SharedBacktestStatus | ''>('');
  const [editing, setEditing] = useState<SharedBacktestManagementItem | null>(null);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchMySharedBacktests(MANAGEMENT_PAGE_SIZE, offset, status || undefined)
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setTotal(page.total);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(managementErrorMessage(reason, '我的共享加载失败'));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [offset, reloadKey, status]);

  function beginEdit(item: SharedBacktestManagementItem) {
    setEditing(item);
    setTitle(item.title);
    setDescription(item.description);
    setTags(item.tags.join('，'));
    setError('');
  }

  async function saveMetadata(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setSaving(true);
    setError('');
    try {
      const updated = await updateSharedBacktestMetadata(editing.id, {
        title: title.trim(),
        description: description.trim(),
        tags: tags.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
      });
      replaceItem(updated);
      setEditing(null);
      onPublicationChanged();
    } catch (reason) {
      setError(managementErrorMessage(reason, '共享展示信息更新失败'));
    } finally {
      setSaving(false);
    }
  }

  async function transition(item: SharedBacktestManagementItem) {
    setSaving(true);
    setError('');
    try {
      const updated = item.status === 'active'
        ? await withdrawSharedBacktest(item.id)
        : await relistSharedBacktest(item.id);
      replaceItem(updated);
      onPublicationChanged();
    } catch (reason) {
      setError(managementErrorMessage(reason, '共享回测状态更新失败'));
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
    <section aria-label="我的共享" className="shared-management">
      <SharedBacktestStatusFilter
        ariaLabel="共享状态"
        value={status}
        onChange={(value) => { setStatus(value); setOffset(0); }}
      />
      {loading && <div className="shared-state">正在加载我的共享...</div>}
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
                {item.symbol} · {item.timeframe} · 首次上架 {item.firstPublishedAt}
              </div>
            </div>
            <div className="shared-management-actions">
              <button type="button" className="shared-button is-quiet" disabled={saving} onClick={() => beginEdit(item)}>编辑展示信息</button>
              {item.status === 'active' && (
                <button type="button" className="shared-button is-danger" disabled={saving} onClick={() => void transition(item)}>撤回</button>
              )}
              {item.status === 'withdrawn' && (
                <button type="button" className="shared-button is-primary" disabled={saving} onClick={() => void transition(item)}>重新上架</button>
              )}
            </div>
          </div>
          <p className="shared-management-description">{item.description}</p>
          <div className="shared-tag-list">{item.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>
          {!item.sourceRunAvailable && (
            <p className="shared-management-note">原回测已删除，共享快照仍保留。</p>
          )}
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
        trailingAction={<button type="button" className="shared-button is-quiet" onClick={() => setReloadKey((value) => value + 1)}>刷新</button>}
      />
      {editing && (
        <div className="shared-modal-backdrop">
          <div role="dialog" aria-modal="true" aria-label="编辑共享展示信息" className="shared-modal">
            <form onSubmit={(event) => void saveMetadata(event)} className="shared-form">
              <div className="shared-modal-heading">
                <span>PUBLICATION DETAILS</span>
                <h2>编辑共享展示信息</h2>
              </div>
              <label className="shared-field">
                <span className="shared-field-label">共享标题</span>
                <input className="shared-input" required maxLength={128} value={title} onChange={(event) => setTitle(event.target.value)} />
              </label>
              <label className="shared-field">
                <span className="shared-field-label">策略说明</span>
                <textarea className="shared-textarea" required maxLength={4000} rows={6} value={description} onChange={(event) => setDescription(event.target.value)} />
              </label>
              <label className="shared-field">
                <span className="shared-field-label">标签（逗号分隔）</span>
                <input className="shared-input" value={tags} onChange={(event) => setTags(event.target.value)} />
              </label>
              <p className="shared-modal-note">策略快照、结果、署名、首次上架时间和统计数据不会被修改。</p>
              <div className="shared-modal-actions">
                <button type="button" className="shared-button is-quiet" disabled={saving} onClick={() => setEditing(null)}>取消</button>
                <button type="submit" className="shared-button is-primary" disabled={saving || !title.trim() || !description.trim()}>{saving ? '保存中...' : '保存'}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
