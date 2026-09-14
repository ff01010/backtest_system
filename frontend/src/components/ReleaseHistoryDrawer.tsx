import type { ReleaseNotice } from '../types';

type Props = {
  open: boolean;
  loading: boolean;
  items: ReleaseNotice[];
  onClose: () => void;
};

export default function ReleaseHistoryDrawer({ open, loading, items, onClose }: Props) {
  if (!open) return null;
  return (
    <div role="dialog" aria-modal="true" aria-label="更新记录">
      <button type="button" aria-label="关闭更新记录" onClick={onClose} style={{
        position: 'fixed', zIndex: 1200, inset: 0, border: 0, background: 'rgba(0, 0, 0, .55)',
      }} />
      <aside style={{
        position: 'fixed', zIndex: 1201, right: 0, top: 0, bottom: 0, width: 'min(440px, 92vw)',
        background: 'var(--canvas)', color: 'var(--text)', boxShadow: '-8px 0 30px rgba(0, 0, 0, .35)',
      }}>
        <header style={{ display: 'flex', alignItems: 'center', padding: '18px 20px', borderBottom: '1px solid #2a2e39' }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>更新记录</h2>
          <button type="button" onClick={onClose} style={{ marginLeft: 'auto' }}>关闭</button>
        </header>
        <div style={{ overflowY: 'auto', height: 'calc(100% - 65px)', padding: 20 }}>
          {loading && <p style={{ color: 'var(--text-muted)' }}>正在加载…</p>}
          {!loading && items.length === 0 && <p style={{ color: 'var(--text-muted)' }}>暂无更新记录</p>}
          {items.map((item) => (
            <article key={item.id} style={{ paddingBottom: 20, marginBottom: 20, borderBottom: '1px solid #2a2e39' }}>
              <div style={{ color: '#8ea3c7', fontSize: 12 }}>{item.version} · {new Date(item.publishedAt).toLocaleString()}</div>
              <h3 style={{ margin: '6px 0', fontSize: 16 }}>{item.title}</h3>
              {item.summary && <p style={{ margin: '0 0 8px', color: 'var(--text-muted)' }}>{item.summary}</p>}
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {item.highlights.map((highlight) => <li key={highlight}>{highlight}</li>)}
              </ul>
            </article>
          ))}
        </div>
      </aside>
    </div>
  );
}
