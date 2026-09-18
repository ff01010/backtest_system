import { Bookmark, BookmarkCheck, Copy } from 'lucide-react';
import type { FormEvent } from 'react';
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
    <section className="shared-filter-panel">
      <form onSubmit={submit} className="shared-filter-form">
        <label className="shared-field shared-search-field">
          <span className="shared-field-label">搜索</span>
          <input
            aria-label="搜索共享回测"
            className="shared-input"
            value={props.draftQuery}
            maxLength={100}
            placeholder="标题、作者、标的或标签"
            onChange={(event) => props.onDraftQueryChange(event.target.value)}
          />
        </label>
        <label className="shared-field">
          <span className="shared-field-label">策略类别</span>
          <select
            aria-label="策略类别"
            className="shared-select"
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
        <label className="shared-field">
          <span className="shared-field-label">周期</span>
          <select
            aria-label="周期"
            className="shared-select"
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
        <label className="shared-field">
          <span className="shared-field-label">排序</span>
          <select
            aria-label="排序"
            className="shared-select"
            value={props.filters.sort}
            onChange={(event) => props.onFilterChange({
              ...props.filters, sort: event.target.value as DiscoveryFilters['sort'],
            })}
          >
            <option value="latest">最新上架</option>
            <option value="copies">复制次数</option>
          </select>
        </label>
        <button type="submit" className="shared-button is-primary shared-search-button">搜索</button>
      </form>
    </section>
  );
}

interface CardProps {
  item: SharedBacktestListItem;
  busy: boolean;
  onOpen: () => void;
  onToggleFavorite: () => void;
  formatPercent: (value: number | null) => string;
  formatNumber: (value: number | null) => string;
}

function strategyCategoryLabel(value: string): string {
  if (value === 'core_position_overlay') return '底仓叠加';
  if (value === 'directional_holding') return '方向持仓';
  return value || '--';
}

export function SharedBacktestCard(props: CardProps) {
  const { item } = props;
  return (
    <article className="shared-strategy-card">
      <header className="shared-card-header">
        <div className="shared-card-kicker">
          <span>公开回测</span>
          <span>{item.timeframe}</span>
        </div>
        <h2>{item.title}</h2>
        <div className="shared-card-meta">
          <span>{item.authorName} · {item.symbol} · {item.timeframe}</span>
          <span>首次上架：{item.firstPublishedAt} · 策略类别：{strategyCategoryLabel(item.strategyCategory)}</span>
        </div>
      </header>
      {item.sourcePublicationId && (
        <div className="shared-card-origin">
          衍生自：{item.sourceTitle || item.sourcePublicationId}
          {item.sourceAuthorName ? ` · 原作者 ${item.sourceAuthorName}` : ''}
        </div>
      )}
      <div className="shared-card-range">回测区间 · {item.startDate} 至 {item.endDate}</div>
      <div className="shared-tag-list shared-card-tags">
        {item.tags.map((tag) => <span key={tag}>#{tag}</span>)}
      </div>
      <div className="shared-card-metrics">
        <div><small>收益</small><strong>{props.formatPercent(item.totalReturn)}</strong></div>
        <div><small>回撤</small><strong>{props.formatPercent(item.maxDrawdownRate)}</strong></div>
        <div><small>夏普</small><strong>{props.formatNumber(item.annualSharpe)}</strong></div>
      </div>
      <div className="shared-card-footnote">
        <span><Copy size={14} /> 已复制 {item.copyCount} 次</span>
        {item.hasCopied && <span className="shared-copy-status">已复制</span>}
      </div>
      <div className="shared-card-actions">
        <button type="button" className="shared-button is-primary" onClick={props.onOpen}>查看详情</button>
        <button
          type="button"
          className="shared-button is-quiet"
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
