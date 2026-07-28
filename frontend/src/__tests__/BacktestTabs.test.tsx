import { FileSliders, History } from 'lucide-react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import BacktestTabs from '../components/BacktestTabs';

describe('BacktestTabs', () => {
  it('shows textual result navigation and changes the active tab', () => {
    const onChange = vi.fn();
    render(
      <BacktestTabs
        activeTab="overview"
        onChange={onChange}
        tabs={[
          { id: 'overview', label: '概览', icon: FileSliders },
          { id: 'trades', label: '交易详情', icon: History },
        ]}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '交易详情' }));

    expect(screen.getByRole('navigation', { name: '回测结果导航' })).toBeTruthy();
    expect(onChange).toHaveBeenCalledWith('trades');
  });
});
