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
  const [selected, setSelected] = useState<MarketInstrument | null>(null);
  const [items, setItems] = useState<MarketInstrument[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [error, setError] = useState('');
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchRequest = useRef(0);
  const valueRequest = useRef(0);
  const commitRequest = useRef(0);

  useEffect(() => {
    setQuery(value);
    setEditing(false);
    setOpen(false);
    setActiveIndex(-1);
    const requestId = ++valueRequest.current;
    if (!value) {
      setSelected(null);
      return;
    }
    searchMarketInstruments(value).then((result) => {
      if (requestId !== valueRequest.current) return;
      setSelected(result.find((item) => item.symbol === value) || null);
    }).catch(() => {
      if (requestId === valueRequest.current) setSelected(null);
    });
  }, [value]);

  useEffect(() => {
    if (!open) return;
    const requestId = ++searchRequest.current;
    setLoading(true);
    setActiveIndex(-1);
    const timer = setTimeout(() => {
      searchMarketInstruments(query).then((result) => {
        if (requestId !== searchRequest.current) return;
        setItems(result);
        setActiveIndex(result.length > 0 ? 0 : -1);
        setLoading(false);
        setError('');
      }).catch((reason) => {
        if (requestId !== searchRequest.current) return;
        setItems([]);
        setActiveIndex(-1);
        setLoading(false);
        setError(reason instanceof Error ? reason.message : '搜索标的失败');
      });
    }, 180);
    return () => {
      clearTimeout(timer);
      if (requestId === searchRequest.current) searchRequest.current += 1;
    };
  }, [open, query]);

  const select = (item: MarketInstrument) => {
    commitRequest.current += 1;
    setSelected(item);
    setQuery(item.symbol);
    setEditing(false);
    setOpen(false);
    setActiveIndex(-1);
    setError('');
    if (item.symbol !== value) onChange(item.symbol);
  };

  const commitExactCode = async (draft: string): Promise<boolean> => {
    const code = draft.trim().toUpperCase();
    if (!/^\d{6}(?:\.(SH|SZ|BJ))?$/.test(code)) return false;
    const requestId = ++commitRequest.current;
    const matches = await searchMarketInstruments(code);
    if (requestId !== commitRequest.current) return false;
    const item = matches.find((candidate) => candidate.symbol === code || candidate.symbol.startsWith(`${code}.`));
    if (!item) return false;
    select(item);
    return true;
  };

  const cancelEditing = () => {
    commitRequest.current += 1;
    setQuery(value);
    setEditing(false);
    setOpen(false);
    setActiveIndex(-1);
    setError('');
  };

  const displayedValue = editing ? query : selected?.symbol === value ? formatInstrument(selected) : value;

  return (
    <label className="instrument-search" style={{ display: 'grid', gap: 6, position: 'relative' }}>
      <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>{label}</span>
      <div style={{ position: 'relative' }}>
        <Search size={15} color="#0f766e" style={{ position: 'absolute', left: 10, top: 10, pointerEvents: 'none' }} />
        <input
          className="instrument-search-input"
          role="combobox"
          aria-expanded={open}
          aria-controls={`${label}-instrument-options`}
          aria-activedescendant={activeIndex >= 0 ? `${label}-instrument-option-${activeIndex}` : undefined}
          value={displayedValue}
          onChange={(event) => {
            setEditing(true);
            setQuery(event.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            if (closeTimer.current) clearTimeout(closeTimer.current);
            commitRequest.current += 1;
            setQuery(value);
            setEditing(true);
            setOpen(true);
          }}
          onBlur={() => {
            const draft = query;
            closeTimer.current = setTimeout(() => {
              if (/^\d{6}(?:\.(SH|SZ|BJ))?$/i.test(draft.trim())) {
                void commitExactCode(draft).then((committed) => {
                  if (!committed) cancelEditing();
                }).catch(cancelEditing);
                return;
              }
              cancelEditing();
            }, 150);
          }}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' && items.length > 0) {
              event.preventDefault();
              setActiveIndex((index) => (index + 1) % items.length);
              return;
            }
            if (event.key === 'ArrowUp' && items.length > 0) {
              event.preventDefault();
              setActiveIndex((index) => (index <= 0 ? items.length - 1 : index - 1));
              return;
            }
            if (event.key === 'Enter') {
              event.preventDefault();
              if (activeIndex >= 0 && items[activeIndex]) {
                select(items[activeIndex]);
              } else {
                void commitExactCode(query).then((committed) => {
                  if (!committed) setError('请选择一个匹配的标的');
                }).catch(() => setError('搜索标的失败'));
              }
            }
            if (event.key === 'Escape') {
              event.preventDefault();
              cancelEditing();
            }
          }}
          placeholder="输入代码或名称，例如 600519、贵州"
          style={inputStyle}
        />
        {open && (
          <div id={`${label}-instrument-options`} role="listbox" className="instrument-search-menu" style={menuStyle}>
            {loading ? <div style={hintStyle}>正在搜索…</div> : error ? <div style={hintStyle}>{error}</div> : items.length === 0 ? (
              <div style={hintStyle}>未找到匹配标的，证券目录会在系统启动时自动同步。</div>
            ) : items.map((item, index) => (
              <button
                id={`${label}-instrument-option-${index}`}
                role="option"
                aria-selected={index === activeIndex}
                className="instrument-search-option"
                key={item.symbol}
                type="button"
                onMouseEnter={() => setActiveIndex(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  select(item);
                }}
                style={{ ...optionStyle, background: index === activeIndex ? 'var(--surface-muted)' : 'transparent' }}
              >
                <strong>{item.symbol}</strong><span>{item.name}</span><small>{item.board === 'star' ? '科创板' : item.board === 'chinext' ? '创业板' : item.market}</small>
              </button>
            ))}
          </div>
        )}
      </div>
      <span style={{ color: 'var(--text-subtle)', fontSize: 11 }}>回测只使用已导入的 QMT 历史数据；数据缺失时请联系管理员导入。</span>
    </label>
  );
}

function formatInstrument(item: MarketInstrument): string {
  return `${item.symbol} · ${item.name}`;
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
