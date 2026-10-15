import { ChevronDown, LoaderCircle, Search } from 'lucide-react';
import { useState } from 'react';
import type { FocusEvent } from 'react';
import type { StrategyConfigSummary } from '../types';

interface Props {
  value: string;
  selected: StrategyConfigSummary | null;
  items: StrategyConfigSummary[];
  total: number;
  query: string;
  loading: boolean;
  loadingMore: boolean;
  error: string;
  hasMore: boolean;
  onQueryChange: (value: string) => void;
  onSelect: (configId: string) => void;
  onLoadMore: () => void;
}

export default function StrategyConfigSelector({
  value,
  selected,
  items,
  total,
  query,
  loading,
  loadingMore,
  error,
  hasMore,
  onQueryChange,
  onSelect,
  onLoadMore,
}: Props) {
  const [open, setOpen] = useState(false);
  const visibleItems = selected && !items.some((item) => item.id === selected.id)
    ? [selected, ...items]
    : items;

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setOpen(false);
    }
  };

  return (
    <div
      className="strategy-config-selector"
      onBlur={handleBlur}
      style={{ position: 'relative', width: '100%' }}
    >
      <div style={{ position: 'relative' }}>
        <Search
          size={14}
          aria-hidden="true"
          style={{ position: 'absolute', left: 9, top: 10, color: '#64748b' }}
        />
        <input
          aria-label="搜索策略方案"
          autoComplete="off"
          value={open ? query : selected?.name ?? ''}
          placeholder="搜索或选择策略方案"
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setOpen(true);
            onQueryChange(event.target.value);
          }}
          style={{
            width: '100%',
            height: 34,
            boxSizing: 'border-box',
            border: '1px solid #cbd5e1',
            borderRadius: 4,
            background: '#fff',
            color: '#0f172a',
            padding: '0 30px 0 30px',
            fontSize: 13,
          }}
        />
        <button
          type="button"
          aria-label="展开策略方案"
          onClick={() => setOpen((current) => !current)}
          style={{
            position: 'absolute', right: 2, top: 2, width: 30, height: 30,
            border: 0, background: 'transparent', color: '#64748b', cursor: 'pointer',
          }}
        >
          <ChevronDown size={15} />
        </button>
      </div>
      {open && (
        <div
          role="listbox"
          aria-label="策略方案"
          style={{
            position: 'absolute', zIndex: 30, top: 38, left: 0, right: 0,
            maxHeight: 280, overflow: 'auto', border: '1px solid #cbd5e1',
            borderRadius: 5, background: '#fff', boxShadow: '0 10px 24px rgb(15 23 42 / 14%)',
          }}
        >
          <button
            type="button"
            role="option"
            aria-selected={!value}
            onClick={() => {
              onSelect('');
              onQueryChange('');
              setOpen(false);
            }}
            style={optionStyle(!value)}
          >
            未选择策略方案
          </button>
          {visibleItems.map((item) => (
            <button
              type="button"
              role="option"
              aria-selected={item.id === value}
              key={item.id}
              onClick={() => {
                onSelect(item.id);
                onQueryChange('');
                setOpen(false);
              }}
              style={optionStyle(item.id === value)}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.name}</span>
              <small style={{ color: '#64748b' }}>V{item.versionNo} · {item.symbol}</small>
            </button>
          ))}
          {loading && (
            <div style={messageStyle}><LoaderCircle className="is-spinning" size={14} /> 正在加载…</div>
          )}
          {!loading && visibleItems.length === 0 && (
            <div style={messageStyle}>{error || '没有匹配的策略方案'}</div>
          )}
          {error && visibleItems.length > 0 && <div role="alert" style={messageStyle}>{error}</div>}
          {hasMore && (
            <button
              type="button"
              disabled={loadingMore}
              onClick={onLoadMore}
              style={{ ...optionStyle(false), justifyContent: 'center', color: '#0f766e' }}
            >
              {loadingMore ? '正在加载…' : '加载更多'}
            </button>
          )}
          <div style={{ ...messageStyle, borderTop: '1px solid #e2e8f0' }}>
            共 {total.toLocaleString()} 个策略
          </div>
        </div>
      )}
    </div>
  );
}

const messageStyle = {
  padding: '8px 10px',
  color: '#64748b',
  fontSize: 12,
  display: 'flex',
  alignItems: 'center',
  gap: 6,
} as const;

function optionStyle(selected: boolean) {
  return {
    width: '100%',
    border: 0,
    borderBottom: '1px solid #f1f5f9',
    background: selected ? '#ecfdf5' : '#fff',
    color: '#0f172a',
    padding: '8px 10px',
    display: 'flex',
    flexDirection: 'column' as const,
    alignItems: 'flex-start',
    gap: 3,
    textAlign: 'left' as const,
    cursor: 'pointer',
  };
}
