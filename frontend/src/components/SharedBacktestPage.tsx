import { ArrowLeft, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  favoriteSharedBacktest,
  fetchSharedBacktest,
  fetchSharedBacktests,
  unfavoriteSharedBacktest,
} from '../api/client';
import type { SharedBacktestDetail, SharedBacktestListItem } from '../types';
import AdminSharedBacktestsPanel from './AdminSharedBacktestsPanel';
import MySharedBacktestsPanel from './MySharedBacktestsPanel';
import SharedBacktestCopyAction from './SharedBacktestCopyAction';
import SharedBacktestDetailTabs from './SharedBacktestDetailTabs';
import {
  SharedBacktestCard,
  SharedBacktestFilters,
  type DiscoveryFilters,
} from './SharedBacktestDiscovery';

interface Props {
  initialPublicationId?: string | null;
  isAdmin?: boolean;
  onOpenCopiedStrategy?: (configId: string) => void;
}

const PAGE_SIZE = 12;
const INITIAL_FILTERS: DiscoveryFilters = {
  query: '', strategyCategory: '', timeframe: '', sort: 'latest',
};

function formatPercent(value: number | null): string {
  return value == null ? '--' : `${(value * 100).toFixed(2)}%`;
}

function formatNumber(value: number | null): string {
  return value == null ? '--' : value.toFixed(2);
}

function SharedBacktestDetailView(props: {
  detail: SharedBacktestDetail | null;
  loading: boolean;
  error: string;
  onBack: () => void;
  onOpenCopiedStrategy: (configId: string) => void;
}) {
  return (
    <main className="shared-square-shell">
      <div className="shared-detail-container">
        <button type="button" onClick={props.onBack} className="shared-button is-quiet shared-back-button">
          <ArrowLeft size={15} /> 返回策略广场
        </button>
        {props.loading && <div className="shared-state">正在加载共享回测...</div>}
        {props.error && <div role="alert" className="shared-alert is-error">{props.error}</div>}
        {props.detail && (
          <div className="shared-detail-layout">
            <section className="shared-detail-hero">
              <div className="shared-page-eyebrow">共享策略快照</div>
              <h1 className="shared-detail-title">{props.detail.title}</h1>
              <div className="shared-detail-meta">
                <span>{props.detail.authorName}</span>
                <span>{props.detail.symbol}</span>
                <span>{props.detail.timeframe}</span>
                <span>{props.detail.startDate} 至 {props.detail.endDate}</span>
              </div>
              <p className="shared-detail-description">{props.detail.description}</p>
              <div className="shared-tag-list">
                {props.detail.tags.map((tag) => <span key={tag}>#{tag}</span>)}
              </div>
            </section>
            {props.detail.sourcePublicationId && (
              <section className="shared-origin-note">
                <span>衍生来源</span>
                <strong>{props.detail.sourceTitle}</strong>
                <span>原作者 {props.detail.sourceAuthorName}</span>
              </section>
            )}
            <SharedBacktestCopyAction
              publicationId={props.detail.id}
              hasCopied={props.detail.hasCopied}
              onOpenStrategy={props.onOpenCopiedStrategy}
            />
            <section className="shared-risk-panel">
              <ShieldCheck size={19} />
              <div>
                <strong>风险提示</strong>
                <span>{props.detail.riskNotice}</span>
              </div>
            </section>
            <section className="shared-metric-strip" aria-label="核心回测指标">
              <div className="shared-metric">
                <small>总收益率</small>
                <strong>{formatPercent(props.detail.totalReturn)}</strong>
              </div>
              <div className="shared-metric">
                <small>最大回撤</small>
                <strong>{formatPercent(props.detail.maxDrawdownRate)}</strong>
              </div>
              <div className="shared-metric">
                <small>夏普比率</small>
                <strong>{formatNumber(props.detail.annualSharpe)}</strong>
              </div>
            </section>
            {props.detail.artifacts.length > 0 && (
              <section className="shared-detail-panel">
                <div className="shared-panel-heading">
                  <div>
                    <span>REPRODUCTION</span>
                    <h2>复现附件</h2>
                  </div>
                  <small>{props.detail.artifacts.length} 个文件</small>
                </div>
                <div className="shared-artifact-list">
                  {props.detail.artifacts.map((artifact) => (
                    <div key={artifact.id} className="shared-artifact-row">
                      <a href={artifact.downloadUrl}>{artifact.displayName}</a>
                      <span>
                        {artifact.type} · {artifact.byteSize} 字节 · SHA-256 {artifact.contentSha256}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}
            <SharedBacktestDetailTabs detail={props.detail} />
          </div>
        )}
      </div>
    </main>
  );
}

export default function SharedBacktestPage({
  initialPublicationId = null,
  isAdmin = false,
  onOpenCopiedStrategy = () => {},
}: Props) {
  const [items, setItems] = useState<SharedBacktestListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [section, setSection] = useState<'all' | 'favorites' | 'mine' | 'admin'>('all');
  const [filters, setFilters] = useState<DiscoveryFilters>(INITIAL_FILTERS);
  const [draftQuery, setDraftQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(initialPublicationId);
  const [detail, setDetail] = useState<SharedBacktestDetail | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [busyFavoriteId, setBusyFavoriteId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchSharedBacktests({
      ...filters, limit: PAGE_SIZE, offset, favoritesOnly: section === 'favorites',
    }).then((page) => {
      if (!cancelled) { setItems(page.items); setTotal(page.total); }
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '策略广场加载失败');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [filters, offset, refreshKey, section]);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    if (!selectedId) return () => { cancelled = true; };
    setLoading(true);
    setError('');
    fetchSharedBacktest(selectedId).then((publication) => {
      if (!cancelled) setDetail(publication);
    }).catch((reason: unknown) => {
      if (!cancelled) {
        const message = reason instanceof Error ? reason.message : '';
        setDetail(null);
        setError(message.includes('404') ? '该共享回测已不可用' : message || '共享回测加载失败');
      }
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selectedId]);

  const toggleFavorite = async (item: SharedBacktestListItem) => {
    setBusyFavoriteId(item.id);
    setError('');
    try {
      if (item.isFavorited) await unfavoriteSharedBacktest(item.id);
      else await favoriteSharedBacktest(item.id);
      setItems((current) => section === 'favorites' && item.isFavorited
        ? current.filter((candidate) => candidate.id !== item.id)
        : current.map((candidate) => candidate.id === item.id
          ? { ...candidate, isFavorited: !item.isFavorited }
          : candidate));
      if (section === 'favorites' && item.isFavorited) setTotal((value) => value - 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '收藏操作失败');
    } finally {
      setBusyFavoriteId(null);
    }
  };

  if (selectedId) {
    return <SharedBacktestDetailView
      detail={detail}
      loading={loading}
      error={error}
      onBack={() => setSelectedId(null)}
      onOpenCopiedStrategy={onOpenCopiedStrategy}
    />;
  }

  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <main className="shared-square-shell">
      <div className="shared-square-container">
        <header className="shared-page-hero">
          <div className="shared-page-eyebrow">STRATEGY COMMONS</div>
          <h1 className="shared-page-title">策略广场</h1>
          <p className="shared-page-description">浏览内部用户主动共享的成功回测快照。</p>
        </header>
        <div role="tablist" aria-label="策略广场分区" className="shared-section-tabs">
          <button type="button" role="tab" aria-selected={section === 'all'} onClick={() => { setSection('all'); setOffset(0); }}>全部共享</button>
          <button type="button" role="tab" aria-selected={section === 'favorites'} onClick={() => { setSection('favorites'); setOffset(0); }}>我的收藏</button>
          <button type="button" role="tab" aria-selected={section === 'mine'} onClick={() => { setSection('mine'); setOffset(0); }}>我的共享</button>
          {isAdmin && (
            <button type="button" role="tab" aria-selected={section === 'admin'} onClick={() => { setSection('admin'); setOffset(0); }}>共享管理</button>
          )}
        </div>
        {section === 'admin' ? (
          <AdminSharedBacktestsPanel
            onPublicationChanged={(publication) => {
              setRefreshKey((value) => value + 1);
              if (publication.status !== 'active' && detail?.id === publication.id) {
                setDetail(null);
                setSelectedId(null);
              }
            }}
          />
        ) : section === 'mine' ? (
          <MySharedBacktestsPanel onPublicationChanged={() => setRefreshKey((value) => value + 1)} />
        ) : (
          <>
            <SharedBacktestFilters
              draftQuery={draftQuery}
              filters={filters}
              onDraftQueryChange={setDraftQuery}
              onFilterChange={(next) => { setFilters(next); setOffset(0); }}
              onSearch={() => { setFilters((value) => ({ ...value, query: draftQuery.trim() })); setOffset(0); }}
            />
            {loading && <div className="shared-state">正在加载共享回测...</div>}
            {error && <div role="alert" className="shared-alert is-error">{error}</div>}
            {!loading && !error && items.length === 0 && (
              <div className="shared-empty-state">{section === 'favorites' ? '暂无收藏的共享回测。' : '暂无符合条件的共享回测。'}</div>
            )}
            <div className="shared-card-grid">
              {items.map((item) => (
                <SharedBacktestCard
                  key={item.id}
                  item={item}
                  busy={busyFavoriteId === item.id}
                  onOpen={() => setSelectedId(item.id)}
                  onToggleFavorite={() => void toggleFavorite(item)}
                  formatPercent={formatPercent}
                  formatNumber={formatNumber}
                />
              ))}
            </div>
            {!loading && !error && total > 0 && (
              <nav aria-label="策略广场分页" className="shared-pagination">
                <button type="button" className="shared-button is-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
                <span>第 {pageNumber} / {pageCount} 页</span>
                <button type="button" className="shared-button is-quiet" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
              </nav>
            )}
          </>
        )}
      </div>
    </main>
  );
}
