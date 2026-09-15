import { ArrowLeft, Code2, LineChart, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  favoriteSharedBacktest,
  fetchSharedBacktest,
  fetchSharedBacktests,
  unfavoriteSharedBacktest,
} from '../api/client';
import type { SharedBacktestDetail, SharedBacktestListItem } from '../types';
import { typography } from '../styles/typography';
import AdminSharedBacktestsPanel from './AdminSharedBacktestsPanel';
import MySharedBacktestsPanel from './MySharedBacktestsPanel';
import SharedBacktestCopyAction from './SharedBacktestCopyAction';
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

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

const shellStyle = {
  flex: 1, minHeight: 0, overflow: 'auto', background: '#eef3f8', color: '#0f172a',
  padding: 24, fontSize: typography.base,
} as const;
const cardStyle = {
  border: '1px solid #d7dde8', borderRadius: 8, background: '#ffffff', padding: 18,
} as const;

function SharedBacktestDetailView(props: {
  detail: SharedBacktestDetail | null;
  loading: boolean;
  error: string;
  onBack: () => void;
  onOpenCopiedStrategy: (configId: string) => void;
}) {
  return (
    <main style={shellStyle}>
      <button type="button" onClick={props.onBack} style={{ marginBottom: 16 }}>
        <ArrowLeft size={14} /> 返回策略广场
      </button>
      {props.loading && <div>正在加载共享回测...</div>}
      {props.error && <div role="alert" style={{ color: '#b91c1c' }}>{props.error}</div>}
      {props.detail && (
        <div style={{ maxWidth: 1080, margin: '0 auto', display: 'grid', gap: 16 }}>
          <section style={cardStyle}>
            <h1 style={{ margin: 0, fontSize: 24 }}>{props.detail.title}</h1>
            <div style={{ marginTop: 8, color: '#64748b' }}>
              {props.detail.authorName} · {props.detail.symbol} · {props.detail.timeframe} · {props.detail.startDate} 至 {props.detail.endDate}
            </div>
            <p style={{ lineHeight: 1.8 }}>{props.detail.description}</p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {props.detail.tags.map((tag) => <span key={tag}>#{tag}</span>)}
            </div>
          </section>
          {props.detail.sourcePublicationId && (
            <section style={cardStyle}>
              来源：{props.detail.sourceTitle} · 原作者 {props.detail.sourceAuthorName}
            </section>
          )}
          <SharedBacktestCopyAction
            publicationId={props.detail.id}
            hasCopied={props.detail.hasCopied}
            onOpenStrategy={props.onOpenCopiedStrategy}
          />
          <section style={{ ...cardStyle, borderColor: '#f59e0b', background: '#fffbeb' }}>
            <ShieldCheck size={18} /> {props.detail.riskNotice}
          </section>
          <section style={{ ...cardStyle, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 }}>
            <div><small>总收益率</small><strong style={{ display: 'block' }}>{formatPercent(props.detail.totalReturn)}</strong></div>
            <div><small>最大回撤</small><strong style={{ display: 'block' }}>{formatPercent(props.detail.maxDrawdownRate)}</strong></div>
            <div><small>夏普比率</small><strong style={{ display: 'block' }}>{formatNumber(props.detail.annualSharpe)}</strong></div>
          </section>
          <section style={cardStyle}>
            <h2><Code2 size={18} />策略代码</h2>
            <JsonBlock value={props.detail.strategy} />
          </section>
          <section style={cardStyle}>
            <h2>回测配置与数据摘要</h2>
            <JsonBlock value={{ configuration: props.detail.configuration, data: props.detail.data }} />
          </section>
          <section style={cardStyle}>
            <h2><LineChart size={18} />结果摘要</h2>
            <JsonBlock value={props.detail.result} />
          </section>
        </div>
      )}
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
    <main style={shellStyle}>
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <h1 style={{ marginTop: 0 }}>策略广场</h1>
        <p style={{ color: '#64748b' }}>浏览内部用户主动共享的成功回测快照。</p>
        <div role="tablist" aria-label="策略广场分区" style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
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
            {loading && <div>正在加载共享回测...</div>}
            {error && <div role="alert" style={{ color: '#b91c1c' }}>{error}</div>}
            {!loading && !error && items.length === 0 && (
              <div style={cardStyle}>{section === 'favorites' ? '暂无收藏的共享回测。' : '暂无符合条件的共享回测。'}</div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 }}>
              {items.map((item) => (
                <SharedBacktestCard
                  key={item.id}
                  item={item}
                  busy={busyFavoriteId === item.id}
                  cardStyle={cardStyle}
                  onOpen={() => setSelectedId(item.id)}
                  onToggleFavorite={() => void toggleFavorite(item)}
                  formatPercent={formatPercent}
                  formatNumber={formatNumber}
                />
              ))}
            </div>
            {!loading && !error && total > 0 && (
              <nav aria-label="策略广场分页" style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 18 }}>
                <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
                <span>第 {pageNumber} / {pageCount} 页</span>
                <button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
              </nav>
            )}
          </>
        )}
      </div>
    </main>
  );
}
