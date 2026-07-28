import type { ChartResponse } from '../types';
import { typography } from '../styles/typography';

interface Props {
  data: ChartResponse | null;
  error: string | null;
  loading: boolean;
}

export default function StatusBar({ data, error, loading }: Props) {
  const s = data?.summary;
  const f = data?.dataFreshness;

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: '16px', padding: '6px 16px',
      background: '#16213e', borderTop: '1px solid #2a2a4a',
      fontSize: typography.base, fontFamily: 'Consolas, monospace',
      flexWrap: 'wrap', minHeight: '32px',
    }}>
      {loading && (
        <span style={{ color: '#f0c040' }}>⏳ 加载中...</span>
      )}

      {error && (
        <span style={{ color: '#ef4444' }}>⚠ {error}</span>
      )}

      {s && (
        <>
          <span style={{ color: '#667' }}>{s.barCount} bars</span>
          <span style={{ color: '#FF6B6B' }}>
            A ▲{s.aBuy} ▼{s.aSell}
          </span>
          <span style={{ color: '#00D2D3' }}>
            B ▲{s.bBuy} ▼{s.bSell}
          </span>
        </>
      )}

      {f && (
        <span style={{ color: '#556', marginLeft: 'auto' }}>
          {f.klineMtime ? `K线 ${f.klineMtime.slice(11, 19)}` : ''}
          {f.signalMtime ? ` | 信号 ${f.signalMtime.slice(11, 19)}` : ''}
        </span>
      )}

      {s && s.warnings.length > 0 && (
        <span style={{ color: '#f0a040' }} title={s.warnings.join('\n')}>
          ⚠ {s.warnings.length} 警告
        </span>
      )}
    </div>
  );
}
