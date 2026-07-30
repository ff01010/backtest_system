import { Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { searchMarketInstruments } from '../api/client';
import type { MarketInstrument } from '../types';

interface Props {
  label: string;
  value: string;
  onChange: (symbol: string) => void;
}

export default function InstrumentSearchField({ label, value, onChange }: Props) {
  const [query, setQuery] = useState(value);
  const [items, setItems] = useState<MarketInstrument[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setQuery(value);
  }, [value]);

  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => {
      searchMarketInstruments(query).then((result) => {
        setItems(result);
        setError('');
      }).catch((reason) => {
        setItems([]);
        setError(reason instanceof Error ? reason.message : '搜索标的失败');
      });
    }, 180);
    return () => clearTimeout(timer);
  }, [open, query]);

  const select = (item: MarketInstrument) => {
    setQuery(`${item.symbol} · ${item.name}`);
    onChange(item.symbol);
    setOpen(false);
  };

  const commitExactCode = async () => {
    const code = query.trim().toUpperCase();
    if (!/^\d{6}(?:\.(SH|SZ|BJ))?$/.test(code)) return;
    const matches = await searchMarketInstruments(code);
    const item = matches.find((candidate) => candidate.symbol === code || candidate.symbol.startsWith(`${code}.`));
    if (item) select(item);
  };

  return (
    <label style={{ display: 'grid', gap: 6, position: 'relative' }}>
      <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>{label}</span>
      <div style={{ position: 'relative' }}>
        <Search size={15} color="#0f766e" style={{ position: 'absolute', left: 10, top: 10, pointerEvents: 'none' }} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onFocus={() => {
            if (closeTimer.current) clearTimeout(closeTimer.current);
            setOpen(true);
          }}
          onBlur={() => {
            void commitExactCode();
            closeTimer.current = setTimeout(() => setOpen(false), 150);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              void commitExactCode();
            }
          }}
          placeholder="输入代码或名称，例如 600519、贵州"
          style={inputStyle}
        />
        {open && (
          <div style={menuStyle}>
            {error ? <div style={hintStyle}>{error}</div> : items.length === 0 ? (
              <div style={hintStyle}>未找到匹配标的，请先由管理员刷新证券目录。</div>
            ) : items.map((item) => (
              <button key={item.symbol} type="button" onMouseDown={() => select(item)} style={optionStyle}>
                <strong>{item.symbol}</strong><span>{item.name}</span><small>{item.board === 'star' ? '科创板' : item.board === 'chinext' ? '创业板' : item.market}</small>
              </button>
            ))}
          </div>
        )}
      </div>
      <span style={{ color: 'var(--text-subtle)', fontSize: 11 }}>无缓存时，首次运行将自动从东财拉取并缓存。</span>
    </label>
  );
}

const inputStyle = {
  width: '100%', height: 36, border: '1px solid var(--border-strong)', borderRadius: 6,
  padding: '0 10px 0 32px', background: 'var(--surface-muted)', color: 'var(--text)',
} as const;

const menuStyle = {
  position: 'absolute', zIndex: 30, top: 40, left: 0, right: 0, maxHeight: 240, overflowY: 'auto',
  background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 7,
  boxShadow: '0 12px 30px rgba(26, 53, 77, .14)', padding: 4,
} as const;

const optionStyle = {
  width: '100%', display: 'grid', gridTemplateColumns: '112px 1fr auto', gap: 8, alignItems: 'center',
  border: 0, borderRadius: 5, padding: '8px 9px', background: 'transparent', color: 'var(--text)', cursor: 'pointer', textAlign: 'left',
} as const;

const hintStyle = { padding: '9px', color: 'var(--text-muted)', fontSize: 12 } as const;
