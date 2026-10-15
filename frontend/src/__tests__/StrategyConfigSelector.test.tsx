import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import StrategyConfigSelector from '../components/StrategyConfigSelector';
import type { StrategyConfigSummary } from '../types';

function summary(id: string, name: string): StrategyConfigSummary {
  return {
    id,
    name,
    scriptId: `script-${id}`,
    strategyVersionId: `code-${id}`,
    scriptVersionNo: 3,
    versionNo: 5,
    revision: 7,
    strategy: 'user_script',
    mode: 'backtest',
    symbol: '688981.SH',
    timeframe: '5m',
    fillModel: 'bar_next_open',
    enabled: true,
    published: true,
    publishedVersionId: `version-${id}`,
    updatedAt: '2026-10-15T08:00:00Z',
  };
}

describe('StrategyConfigSelector', () => {
  it('只渲染已加载摘要，并固定显示不在当前页的选中策略', () => {
    render(
      <StrategyConfigSelector
        value="selected"
        selected={summary('selected', '固定策略')}
        items={[summary('page-1', '第一页策略')]}
        total={10000}
        query=""
        loading={false}
        loadingMore={false}
        error=""
        hasMore
        onQueryChange={vi.fn()}
        onSelect={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    fireEvent.focus(screen.getByLabelText('搜索策略方案'));

    expect(screen.getByText('固定策略')).toBeTruthy();
    expect(screen.getByText('第一页策略')).toBeTruthy();
    expect(screen.getByText('共 10,000 个策略')).toBeTruthy();
    expect(screen.getAllByRole('option')).toHaveLength(3);
  });

  it('把搜索、选择和加载更多操作交给分页调用方', () => {
    const onQueryChange = vi.fn();
    const onSelect = vi.fn();
    const onLoadMore = vi.fn();
    render(
      <StrategyConfigSelector
        value=""
        selected={null}
        items={[summary('page-1', '第一页策略')]}
        total={2}
        query=""
        loading={false}
        loadingMore={false}
        error=""
        hasMore
        onQueryChange={onQueryChange}
        onSelect={onSelect}
        onLoadMore={onLoadMore}
      />,
    );

    const input = screen.getByLabelText('搜索策略方案');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '均值' } });
    fireEvent.click(screen.getByText('第一页策略'));
    fireEvent.focus(input);
    fireEvent.click(screen.getByText('加载更多'));

    expect(onQueryChange).toHaveBeenCalledWith('均值');
    expect(onSelect).toHaveBeenCalledWith('page-1');
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });
});
