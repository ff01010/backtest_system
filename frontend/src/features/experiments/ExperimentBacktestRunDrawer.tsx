import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { Search, X } from 'lucide-react';
import { fetchExperimentBacktestRuns } from './api';
import type { ExperimentBacktestRun } from './contracts';
import { statusLabel } from './displayLabels';

type Props = {
  experimentId: string;
  open: boolean;
  summary: { totalCount: number; initialCount: number; retryCount: number };
  onClose: () => void;
};

type TrialGroup = {
  trialId: string;
  trialIndex: number;
  runs: ExperimentBacktestRun[];
};

type RevisionGroup = {
  revisionId: string;
  revisionNo: number;
  current: boolean;
  trials: TrialGroup[];
};

function createdReasonLabel(value: ExperimentBacktestRun['createdReason']): string {
  if (value === 'automatic_retry') return '系统重试';
  if (value === 'manual_retry') return '人工重试';
  return '首次执行';
}

function groupRuns(items: ExperimentBacktestRun[]): RevisionGroup[] {
  const revisions = new Map<string, RevisionGroup>();
  const trials = new Map<string, TrialGroup>();
  items.forEach((item) => {
    let revision = revisions.get(item.revisionId);
    if (!revision) {
      revision = {
        revisionId: item.revisionId,
        revisionNo: item.revisionNo,
        current: item.currentRevision,
        trials: [],
      };
      revisions.set(item.revisionId, revision);
    }
    let trial = trials.get(item.trialId);
    if (!trial) {
      trial = { trialId: item.trialId, trialIndex: item.trialIndex, runs: [] };
      trials.set(item.trialId, trial);
      revision.trials.push(trial);
    }
    trial.runs.push(item);
  });
  return [...revisions.values()];
}

export default function ExperimentBacktestRunDrawer({
  experimentId,
  open,
  summary,
  onClose,
}: Props) {
  const [items, setItems] = useState<ExperimentBacktestRun[]>([]);
  const [counts, setCounts] = useState({
    total: summary.totalCount,
    initial: summary.initialCount,
    retry: summary.retryCount,
  });
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const groups = useMemo(() => groupRuns(items), [items]);

  const loadFirstPage = useCallback(async (nextQuery: string) => {
    setLoading(true);
    setError('');
    try {
      const page = await fetchExperimentBacktestRuns(experimentId, undefined, nextQuery);
      setItems(page.items);
      setCounts({ total: page.totalCount, initial: page.initialCount, retry: page.retryCount });
      setNextCursor(page.nextCursor);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取关联回测任务');
    } finally {
      setLoading(false);
    }
  }, [experimentId]);

  useEffect(() => {
    if (!open) return;
    setCounts({
      total: summary.totalCount,
      initial: summary.initialCount,
      retry: summary.retryCount,
    });
    void loadFirstPage(query);
  }, [loadFirstPage, open, query, summary.initialCount, summary.retryCount, summary.totalCount]);

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
    setQuery(searchInput.trim());
  };

  const loadMore = async () => {
    if (!nextCursor || loading) return;
    setLoading(true);
    setError('');
    try {
      const page = await fetchExperimentBacktestRuns(experimentId, nextCursor, query);
      setItems((current) => [...current, ...page.items]);
      setNextCursor(page.nextCursor);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法继续读取关联回测任务');
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;
  return (
    <div className="experiment-run-drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        aria-label="关联回测任务"
        aria-modal="true"
        className="experiment-run-drawer"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="experiment-run-drawer-header">
          <div>
            <span>实验运行索引</span>
            <h2>关联回测任务 <strong>{counts.total}</strong></h2>
            <small>首次执行 {counts.initial} · 重试 {counts.retry}</small>
          </div>
          <button className="app-button is-quiet" type="button" onClick={onClose} aria-label="关闭关联回测任务">
            <X size={16} /> 关闭
          </button>
        </header>
        <form className="experiment-run-search" onSubmit={handleSearch}>
          <Search size={16} />
          <input
            aria-label="筛选回测任务编号"
            inputMode="numeric"
            placeholder="输入回测任务编号，例如 #187"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
          <button className="app-button is-quiet" type="submit">筛选</button>
        </form>
        <div className="experiment-run-drawer-body">
          {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
          {!loading && !error && items.length === 0 && (
            <div className="experiment-empty">没有匹配的回测任务。</div>
          )}
          {groups.map((revision) => (
            <details className="experiment-run-revision" open={revision.current || Boolean(query) || undefined} key={revision.revisionId}>
              <summary>
                <strong>Revision {revision.revisionNo}{revision.current ? ' · 当前' : ''}</strong>
                <span>本页 {revision.trials.reduce((count, trial) => count + trial.runs.length, 0)} 个</span>
              </summary>
              <div className="experiment-run-trials">
                {revision.trials.map((trial) => (
                  <section className="experiment-run-trial" key={trial.trialId}>
                    <header><strong>Trial #{trial.trialIndex + 1}</strong></header>
                    {trial.runs.map((run) => (
                      <div className="experiment-run-row" key={run.executionId}>
                        <strong>#{run.displayNo}</strong>
                        <span>{statusLabel(run.status)}</span>
                        <span>{createdReasonLabel(run.createdReason)}</span>
                        {run.selected ? <small>选定结果</small> : <small />}
                      </div>
                    ))}
                  </section>
                ))}
              </div>
            </details>
          ))}
          {loading && <div className="experiment-empty">正在读取回测任务…</div>}
          {nextCursor && !loading && (
            <button className="app-button is-quiet experiment-run-load-more" type="button" onClick={() => { void loadMore(); }}>
              加载更多
            </button>
          )}
        </div>
      </aside>
    </div>
  );
}
