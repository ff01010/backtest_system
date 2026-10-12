import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Search, X } from 'lucide-react';
import { fetchExperiments } from './api';
import type { ExperimentListItem, ExperimentStatus } from './contracts';
import { statusLabel } from './displayLabels';

type Props = {
  archived: boolean;
  open: boolean;
  onClose: () => void;
  onOpenExperiment: (experimentId: string) => void;
};

const STATUS_OPTIONS: ExperimentStatus[] = [
  'preparing',
  'queued',
  'running',
  'cancelling',
  'completed',
  'partially_completed',
  'failed',
  'cancelled',
  'generation_blocked',
];

function formatUpdatedAt(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return '—';
  return new Date(timestamp).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : '无法读取实验列表';
}

export default function ExperimentCatalogDrawer({
  archived,
  open,
  onClose,
  onOpenExperiment,
}: Props) {
  const [items, setItems] = useState<ExperimentListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<ExperimentStatus | ''>('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const requestSequence = useRef(0);

  const loadFirstPage = useCallback(async () => {
    const sequence = requestSequence.current + 1;
    requestSequence.current = sequence;
    setLoading(true);
    setError('');
    setItems([]);
    setNextCursor(null);
    try {
      const page = await fetchExperiments(undefined, archived, {
        limit: 20,
        query,
        status,
      });
      if (requestSequence.current !== sequence) return;
      setItems(page.items);
      setNextCursor(page.nextCursor);
    } catch (reason) {
      if (requestSequence.current === sequence) setError(errorMessage(reason));
    } finally {
      if (requestSequence.current === sequence) setLoading(false);
    }
  }, [archived, query, status]);

  useEffect(() => {
    if (open) void loadFirstPage();
  }, [loadFirstPage, open]);

  useEffect(() => {
    setSearchInput('');
    setQuery('');
    setStatus('');
  }, [archived]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, open]);

  const handleSearch = (event: FormEvent) => {
    event.preventDefault();
    const nextQuery = searchInput.trim();
    if (nextQuery === query) {
      void loadFirstPage();
      return;
    }
    setQuery(nextQuery);
  };

  const loadMore = async () => {
    if (!nextCursor || loading) return;
    const sequence = requestSequence.current + 1;
    requestSequence.current = sequence;
    setLoading(true);
    setError('');
    try {
      const page = await fetchExperiments(nextCursor, archived, {
        limit: 20,
        query,
        status,
      });
      if (requestSequence.current !== sequence) return;
      setItems((current) => [...current, ...page.items]);
      setNextCursor(page.nextCursor);
    } catch (reason) {
      if (requestSequence.current === sequence) setError(errorMessage(reason));
    } finally {
      if (requestSequence.current === sequence) setLoading(false);
    }
  };

  if (!open) return null;
  const title = archived ? '全部归档实验' : '全部实验';
  return (
    <div className="experiment-run-drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        aria-label={title}
        aria-modal="true"
        className="experiment-run-drawer experiment-catalog-drawer"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="experiment-run-drawer-header">
          <div>
            <span>{archived ? '历史归档' : '实验记录'}</span>
            <h2>{title}</h2>
            <small>按最近更新时间排序 · 每次加载 20 条</small>
          </div>
          <button className="app-button is-quiet" type="button" onClick={onClose} aria-label={`关闭${title}`}>
            <X size={16} /> 关闭
          </button>
        </header>
        <form className="experiment-catalog-filters" onSubmit={handleSearch}>
          <label className="experiment-catalog-search">
            <Search size={16} />
            <input
              aria-label="搜索实验"
              maxLength={100}
              placeholder="实验名称、策略名称或标的"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />
          </label>
          <select
            aria-label="筛选实验状态"
            value={status}
            onChange={(event) => setStatus(event.target.value as ExperimentStatus | '')}
          >
            <option value="">全部状态</option>
            {STATUS_OPTIONS.map((value) => (
              <option value={value} key={value}>{statusLabel(value)}</option>
            ))}
          </select>
          <button className="app-button is-quiet" type="submit" disabled={loading}>搜索</button>
        </form>
        <div className="experiment-run-drawer-body experiment-catalog-body">
          {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
          {!loading && !error && items.length === 0 && (
            <div className="experiment-empty">没有符合当前条件的实验。</div>
          )}
          <div className="experiment-catalog-list">
            {items.map((experiment) => (
              <button
                className="experiment-catalog-row"
                type="button"
                key={experiment.experimentId}
                onClick={() => onOpenExperiment(experiment.experimentId)}
              >
                <span className="experiment-catalog-identity">
                  <strong>{experiment.name}</strong>
                  <small>{experiment.subject.name}</small>
                </span>
                <span>{experiment.symbol} · {experiment.timeframe}</span>
                <span className="experiment-catalog-status" data-status={experiment.status}>
                  {statusLabel(experiment.status)}
                </span>
                <span>成功 {experiment.succeededTrialCount} / {experiment.plannedTrialCount}</span>
                <time dateTime={experiment.updatedAt}>更新 {formatUpdatedAt(experiment.updatedAt)}</time>
              </button>
            ))}
          </div>
          {loading && <div className="experiment-empty">正在读取实验…</div>}
          {nextCursor && !loading && (
            <button className="app-button is-quiet experiment-run-load-more" type="button" onClick={() => { void loadMore(); }}>
              加载更多实验
            </button>
          )}
        </div>
      </aside>
    </div>
  );
}
