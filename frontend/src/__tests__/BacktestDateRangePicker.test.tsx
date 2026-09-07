import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import BacktestDateRangePicker, { type BacktestCalendarDay } from '../components/BacktestDateRangePicker';

const days: BacktestCalendarDay[] = [
  { date: '2026-09-01', tradingDay: true, status: 'available' },
  { date: '2026-09-02', tradingDay: true, status: 'available' },
  { date: '2026-09-03', tradingDay: true, status: 'available' },
];

function renderPicker(onChange = vi.fn()): ReturnType<typeof render> {
  return render(
    <details open data-testid="scope-section" style={{ overflow: 'hidden' }}>
      <summary>回测范围</summary>
      <BacktestDateRangePicker
        startDate="2026-09-01"
        endDate="2026-09-03"
        days={days}
        onChange={onChange}
        onMonthChange={vi.fn()}
      />
    </details>
  );
}

describe('BacktestDateRangePicker', () => {
  it('renders the calendar outside the clipped config section', () => {
    renderPicker();

    fireEvent.click(screen.getByRole('button', { name: '开始：2026-09-01' }));

    const dialog = screen.getByRole('dialog', { name: '回测日期选择' });
    expect(dialog.parentElement).toBe(document.body);
    expect(screen.getByTestId('scope-section').hasAttribute('open')).toBe(true);
  });

  it('keeps the section and calendar open while switching to the end date', () => {
    const onChange = vi.fn();
    renderPicker(onChange);

    fireEvent.click(screen.getByRole('button', { name: '开始：2026-09-01' }));
    fireEvent.click(screen.getByRole('button', { name: '2' }));

    expect(onChange).toHaveBeenCalledWith('startDate', '2026-09-02');
    expect(screen.getByRole('dialog', { name: '回测日期选择' })).toBeTruthy();
    expect(screen.getByTestId('scope-section').hasAttribute('open')).toBe(true);
  });

  it('closes on an outside pointer or Escape', () => {
    renderPicker();
    const startButton = screen.getByRole('button', { name: '开始：2026-09-01' });

    fireEvent.click(startButton);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog', { name: '回测日期选择' })).toBeNull();

    fireEvent.click(startButton);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '回测日期选择' })).toBeNull();
  });
});
