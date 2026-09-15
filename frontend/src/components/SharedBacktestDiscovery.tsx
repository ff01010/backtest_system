import { Bookmark, BookmarkCheck, Copy } from 'lucide-react';
import type { CSSProperties, FormEvent } from 'react';
import type { SharedBacktestListItem } from '../types';

export interface DiscoveryFilters {
  query: string;
  strategyCategory: string;
  timeframe: string;
  sort: 'latest' | 'copies';
}

interface FilterProps {
  draftQuery: string;
  filters: DiscoveryFilters;
  onDraftQueryChange: (value: string) => void;
  onFilterChange: (filters: DiscoveryFilters) => void;
  onSearch: () => void;
}

export function SharedBacktestFilters(props: FilterProps) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onSearch();
  };
  return (
    <section style={{ display: 'grid', gap: 14, marginBottom: 18 }}>
      <form onSubmit={submit} style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <label>
          <span style={{ marginRight: 6 }}>搜索</span>
          <input
            aria-label="搜索共享回测"
            value={props.draftQuery}
            maxLength={100}
            placeholder="标题、作者、标的或标签"
            onChange={(event) => props.onDraftQueryChange(event.target.value)}
          />
        </label>
        <label>
          <span style={{ marginRight: 6 }}>策略类别</span>
          <select
            aria-label="策略类别"
            value={props.filters.strategyCategory}
            onChange={(event) => props.onFilterChange({
              ...props.filters, strategyCategory: event.target.value,
            })}
          >
            <option value="">全部</option>
            <option value="directional_holding">方向持仓</option>
            <option value="core_position_overlay">底仓叠加</option>
          </select>
        </label>
        <label>
          <span style={{ marginRight: 6 }}>周期</span>
          <select
            aria-label="周期"
            value={props.filters.timeframe}
            onChange={(event) => props.onFilterChange({
              ...props.filters, timeframe: event.target.value,
            })}
          >
            <option value="">全部</option>
            {['1m', '5m', '15m', '30m', '60m', '1d'].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          <span style={{ marginRight: 6 }}>排序</span>
          <select
            aria-label="排序"
            value={props.filters.sort}
            onChange={(event) => props.onFilterChange({
              ...props.filters, sort: event.target.value as DiscoveryFilters['sort'],
            })}
          >
            <option value="latest">最新上架</option>
            <option value="copies">复制次数</option>
          </select>
        </label>
        <button type="submit">搜索</button>
      </form>
    </section>
  );
}

interface CardProps {
  item: SharedBacktestListItem;
  busy: boolean;
  cardStyle: CSSProperties;
  onOpen: () => void;
  onToggleFavorite: () => void;
  formatPercent: (value: number | null) => string;
  formatNumber: (value: number | null) => string;
}

export function SharedBacktestCard(props: CardProps) {
  const { item } = props;
  return (
    <article style={props.cardStyle}>
      <h2 style={{ marginTop: 0, fontSize: 18 }}>{item.title}</h2>
      <div style={{ color: '#64748b' }}>{item.authorName} · {item.symbol} · {item.timeframe}</div>
      <div style={{ margin: '10px 0' }}>{item.startDate} 至 {item.endDate}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
        {item.tags.map((tag) => <span key={tag}>#{tag}</span>)}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <span>收益 {props.formatPercent(item.totalReturn)}</span>
        <span>回撤 {props.formatPercent(item.maxDrawdownRate)}</span>
        <span>夏普 {props.formatNumber(item.annualSharpe)}</span>
      </div>
      <div style={{ color: '#64748b', marginBottom: 12 }}>
        <Copy size={14} /> 已复制 {item.copyCount} 次
        {item.hasCopied && <span style={{ marginLeft: 10 }}>已复制</span>}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <button type="button" onClick={props.onOpen}>查看详情</button>
        <button
          type="button"
          disabled={props.busy}
          aria-label={item.isFavorited ? `取消收藏 ${item.title}` : `收藏 ${item.title}`}
          onClick={props.onToggleFavorite}
        >
          {item.isFavorited ? <BookmarkCheck size={15} /> : <Bookmark size={15} />}
          {item.isFavorited ? '取消收藏' : '收藏'}
        </button>
      </div>
    </article>
  );
}
