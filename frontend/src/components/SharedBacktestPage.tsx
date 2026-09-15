import { ArrowLeft, Code2, LineChart, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import { fetchSharedBacktest, fetchSharedBacktests } from '../api/client';
import type { SharedBacktestDetail, SharedBacktestListItem } from '../types';
import { typography } from '../styles/typography';
import MySharedBacktestsPanel from './MySharedBacktestsPanel';

interface Props {
  initialPublicationId?: string | null;
}

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

export default function SharedBacktestPage({ initialPublicationId = null }: Props) {
  const [items, setItems] = useState<SharedBacktestListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialPublicationId);
  const [detail, setDetail] = useState<SharedBacktestDetail | null>(null);
  const [section, setSection] = useState<'all' | 'mine'>('all');
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetchSharedBacktests()
      .then((page) => { if (!cancelled) setItems(page.items); })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '策略广场加载失败');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refreshKey]);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    if (!selectedId) return () => { cancelled = true; };
    setLoading(true);
    setError('');
    fetchSharedBacktest(selectedId)
      .then((publication) => { if (!cancelled) setDetail(publication); })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '共享回测加载失败');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selectedId]);

  const shellStyle = {
    flex: 1, minHeight: 0, overflow: 'auto', background: '#eef3f8', color: '#0f172a',
    padding: 24, fontSize: typography.base,
  } as const;
  const cardStyle = {
    border: '1px solid #d7dde8', borderRadius: 8, background: '#ffffff', padding: 18,
  } as const;

  if (selectedId) {
    return (
      <main style={shellStyle}>
        <button type="button" onClick={() => setSelectedId(null)} style={{ marginBottom: 16 }}>
          <ArrowLeft size={14} /> 返回策略广场
        </button>
        {loading && <div>正在加载共享回测...</div>}
        {error && <div role="alert" style={{ color: '#b91c1c' }}>{error}</div>}
        {detail && (
          <div style={{ maxWidth: 1080, margin: '0 auto', display: 'grid', gap: 16 }}>
            <section style={cardStyle}>
              <h1 style={{ margin: 0, fontSize: 24 }}>{detail.title}</h1>
              <div style={{ marginTop: 8, color: '#64748b' }}>
                {detail.authorName} · {detail.symbol} · {detail.timeframe} · {detail.startDate} 至 {detail.endDate}
              </div>
              <p style={{ lineHeight: 1.8 }}>{detail.description}</p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {detail.tags.map((tag) => <span key={tag}>#{tag}</span>)}
              </div>
            </section>
            <section style={{ ...cardStyle, borderColor: '#f59e0b', background: '#fffbeb' }}>
              <ShieldCheck size={18} /> {detail.riskNotice}
            </section>
            <section style={{ ...cardStyle, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 }}>
              <div><small>总收益率</small><strong style={{ display: 'block' }}>{formatPercent(detail.totalReturn)}</strong></div>
              <div><small>最大回撤</small><strong style={{ display: 'block' }}>{formatPercent(detail.maxDrawdownRate)}</strong></div>
              <div><small>夏普比率</small><strong style={{ display: 'block' }}>{formatNumber(detail.annualSharpe)}</strong></div>
            </section>
            <section style={cardStyle}>
              <h2 style={{ display: 'flex', alignItems: 'center', gap: 8 }}><Code2 size={18} />策略代码</h2>
              <JsonBlock value={detail.strategy} />
            </section>
            <section style={cardStyle}>
              <h2>回测配置与数据摘要</h2>
              <JsonBlock value={{ configuration: detail.configuration, data: detail.data }} />
            </section>
            <section style={cardStyle}>
              <h2 style={{ display: 'flex', alignItems: 'center', gap: 8 }}><LineChart size={18} />结果摘要</h2>
              <JsonBlock value={detail.result} />
            </section>
          </div>
        )}
      </main>
    );
  }

  return (
    <main style={shellStyle}>
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <h1 style={{ marginTop: 0 }}>策略广场</h1>
        <p style={{ color: '#64748b' }}>浏览内部用户主动共享的成功回测快照。</p>
        <div role="tablist" aria-label="策略广场分区" style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
          <button type="button" role="tab" aria-selected={section === 'all'} onClick={() => setSection('all')}>全部共享</button>
          <button type="button" role="tab" aria-selected={section === 'mine'} onClick={() => setSection('mine')}>我的共享</button>
        </div>
        {section === 'mine' ? (
          <MySharedBacktestsPanel onPublicationChanged={() => setRefreshKey((value) => value + 1)} />
        ) : (
          <>
            {loading && <div>正在加载共享回测...</div>}
            {error && <div role="alert" style={{ color: '#b91c1c' }}>{error}</div>}
            {!loading && !error && items.length === 0 && <div style={cardStyle}>暂无共享回测。</div>}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 }}>
              {items.map((item) => (
                <article key={item.id} style={cardStyle}>
                  <h2 style={{ marginTop: 0, fontSize: 18 }}>{item.title}</h2>
                  <div style={{ color: '#64748b' }}>{item.authorName} · {item.symbol} · {item.timeframe}</div>
                  <div style={{ margin: '10px 0' }}>{item.startDate} 至 {item.endDate}</div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 14 }}>
                    <span>收益 {formatPercent(item.totalReturn)}</span>
                    <span>回撤 {formatPercent(item.maxDrawdownRate)}</span>
                    <span>夏普 {formatNumber(item.annualSharpe)}</span>
                  </div>
                  <button type="button" onClick={() => setSelectedId(item.id)}>查看详情</button>
                </article>
              ))}
            </div>
          </>
        )}
      </div>
    </main>
  );
}
