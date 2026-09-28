import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import BacktestDateRangePicker, {
  buildPresetRange,
  replaceDatePart,
  type RangeAvailability,
} from '../components/BacktestDateRangePicker';

interface Range {
  startDate: string;
  endDate: string;
}

function PickerHarness({
  initialRange = { startDate: '2026-09-01', endDate: '2026-09-03' },
  rangeAvailability = {
    state: 'available',
    firstDate: '2015-01-05',
    lastDate: '2026-09-18',
  },
  onApplyRange = vi.fn(),
}: {
  initialRange?: Range;
  rangeAvailability?: RangeAvailability;
  onApplyRange?: (startDate: string, endDate: string) => void;
}) {
  const [range, setRange] = useState(initialRange);
  const handleApplyRange = (startDate: string, endDate: string): void => {
    onApplyRange(startDate, endDate);
    setRange({ startDate, endDate });
  };
  return (
    <BacktestDateRangePicker
      {...range}
      rangeAvailability={rangeAvailability}
      onApplyRange={handleApplyRange}
    />
  );
}

describe('BacktestDateRangePicker', () => {
  it('keeps the compact horizontal controls without a calendar', () => {
    render(<PickerHarness />);

    expect(screen.getByText('可回测范围：')).toBeTruthy();
    expect(screen.getByText('2015-01-05 — 2026-09-18')).toBeTruthy();
    expect(screen.getByRole('group', { name: '开始日期 2026-09-01' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2026-09-03' })).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows the approved shortcuts, earliest action, and draft actions', () => {
    render(<PickerHarness />);

    expect(screen.getByRole('button', { name: '近1月' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '近3月' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '今年' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '全部数据' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '从最早日期开始' })).toBeTruthy();
    expect((screen.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '应用' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps shortcut changes as a draft until Apply is clicked', () => {
    const onApplyRange = vi.fn();
    render(<PickerHarness onApplyRange={onApplyRange} />);

    fireEvent.click(screen.getByRole('button', { name: '近3月' }));

    expect(screen.getByRole('group', { name: '开始日期 2026-06-19' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2026-09-18' })).toBeTruthy();
    expect(onApplyRange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '应用' }));
    expect(onApplyRange).toHaveBeenCalledOnce();
    expect(onApplyRange).toHaveBeenCalledWith('2026-06-19', '2026-09-18');
  });

  it('validates the closed available range only when Apply is clicked', () => {
    const onApplyRange = vi.fn();
    render(<PickerHarness onApplyRange={onApplyRange} />);

    fireEvent.click(screen.getByRole('button', { name: '结束日期年份' }));
    fireEvent.click(screen.getByRole('option', { name: '2027' }));

    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '应用' }));

    expect(screen.getByRole('alert').textContent).toContain(
      '请选择 2015-01-05 至 2026-09-18 范围内的回测区间（含起止日期）',
    );
    expect(onApplyRange).not.toHaveBeenCalled();
  });

  it('accepts both endpoints of the closed available range', () => {
    const onApplyRange = vi.fn();
    render(<PickerHarness onApplyRange={onApplyRange} />);

    fireEvent.click(screen.getByRole('button', { name: '全部数据' }));
    fireEvent.click(screen.getByRole('button', { name: '应用' }));

    expect(screen.queryByRole('alert')).toBeNull();
    expect(onApplyRange).toHaveBeenCalledWith('2015-01-05', '2026-09-18');
  });

  it('cancels shortcut and wheel draft changes together', () => {
    const onApplyRange = vi.fn();
    render(<PickerHarness onApplyRange={onApplyRange} />);

    fireEvent.click(screen.getByRole('button', { name: '全部数据' }));
    expect(screen.getByRole('group', { name: '开始日期 2015-01-05' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getByRole('group', { name: '开始日期 2026-09-01' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2026-09-03' })).toBeTruthy();
    expect(onApplyRange).not.toHaveBeenCalled();
  });

  it('uses the earliest available date without linking the draft end date', () => {
    render(
      <PickerHarness initialRange={{ startDate: '2014-12-01', endDate: '2014-12-31' }} />,
    );

    fireEvent.click(screen.getByRole('button', { name: '从最早日期开始' }));

    expect(screen.getByRole('group', { name: '开始日期 2015-01-05' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2014-12-31' })).toBeTruthy();
  });

  it('opens a visible wheel with context around the selected value', () => {
    render(<PickerHarness />);

    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));

    const list = screen.getByRole('listbox', { name: '开始日期年份选项' });
    expect(list.parentElement?.parentElement).toBe(document.body);
    expect(screen.getByRole('option', { name: '2025' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '2026' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('option', { name: '2027' })).toBeTruthy();
  });

  it('selects visible year, month, and day options independently', () => {
    render(<PickerHarness initialRange={{ startDate: '2026-01-31', endDate: '2028-12-31' }} />);

    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));
    fireEvent.click(screen.getByRole('option', { name: '2027' }));
    expect(screen.getByRole('group', { name: '开始日期 2027-01-31' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '开始日期月份' }));
    fireEvent.click(screen.getByRole('option', { name: '02' }));
    expect(screen.getByRole('group', { name: '开始日期 2027-02-28' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '开始日期日期' }));
    fireEvent.click(screen.getByRole('option', { name: '27' }));
    expect(screen.getByRole('group', { name: '开始日期 2027-02-27' })).toBeTruthy();
  });

  it('updates the centered value when the long list scrolls', () => {
    render(<PickerHarness />);
    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));
    const list = screen.getByRole('listbox', { name: '开始日期年份选项' });

    list.scrollTop = (2028 - 1000) * 32;
    fireEvent.scroll(list);

    expect(screen.getByRole('group', { name: '开始日期 2028-09-01' })).toBeTruthy();
  });

  it('closes the wheel on an outside pointer or Escape', () => {
    render(<PickerHarness />);
    const yearButton = screen.getByRole('button', { name: '开始日期年份' });

    fireEvent.click(yearButton);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('listbox')).toBeNull();

    fireEvent.click(yearButton);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('does not apply when the start date passes the end date', () => {
    const onApplyRange = vi.fn();
    render(
      <PickerHarness
        initialRange={{ startDate: '2026-09-03', endDate: '2026-09-03' }}
        onApplyRange={onApplyRange}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '开始日期日期' }));
    fireEvent.click(screen.getByRole('option', { name: '04' }));

    expect(screen.getByRole('group', { name: '开始日期 2026-09-04' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2026-09-03' })).toBeTruthy();
    expect(onApplyRange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '应用' }));
    expect(screen.getByRole('alert').textContent).toContain('开始日期不能晚于结束日期');
    expect(onApplyRange).not.toHaveBeenCalled();
  });

  it('does not move the start date when the end date passes it', () => {
    render(<PickerHarness initialRange={{ startDate: '2026-09-03', endDate: '2026-09-03' }} />);

    fireEvent.click(screen.getByRole('button', { name: '结束日期日期' }));
    fireEvent.click(screen.getByRole('option', { name: '02' }));

    expect(screen.getByRole('group', { name: '开始日期 2026-09-03' })).toBeTruthy();
    expect(screen.getByRole('group', { name: '结束日期 2026-09-02' })).toBeTruthy();
  });

  it('does not require market-data boundaries to render wheel options', () => {
    const onApplyRange = vi.fn();
    render(
      <PickerHarness
        rangeAvailability={{ state: 'loading' }}
        onApplyRange={onApplyRange}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));

    expect(screen.getByText('正在读取')).toBeTruthy();
    expect((screen.getByRole('button', { name: '近1月' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('option', { name: '2025' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '2027' })).toBeTruthy();

    fireEvent.click(screen.getByRole('option', { name: '2025' }));
    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '应用' }));
    expect(screen.getByRole('alert').textContent).toContain('正在读取可回测范围，请稍后重试');
    expect(onApplyRange).not.toHaveBeenCalled();
  });

  it('replaces loading with no-data feedback when the range request completes empty', () => {
    const onApplyRange = vi.fn();
    const { rerender } = render(
      <PickerHarness rangeAvailability={{ state: 'loading' }} onApplyRange={onApplyRange} />,
    );
    expect(screen.getByText('正在读取')).toBeTruthy();

    rerender(<PickerHarness rangeAvailability={{ state: 'empty' }} onApplyRange={onApplyRange} />);
    expect(screen.getByText('暂无可回测数据')).toBeTruthy();
    expect(screen.queryByText('正在读取')).toBeNull();
    expect((screen.getByRole('button', { name: '全部数据' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));
    fireEvent.click(screen.getByRole('option', { name: '2025' }));
    fireEvent.click(screen.getByRole('button', { name: '应用' }));
    expect(screen.getByRole('alert').textContent).toContain('当前标的暂无可回测数据');
    expect(onApplyRange).not.toHaveBeenCalled();
  });

  it('shows a range query error rather than permanent loading', () => {
    const onApplyRange = vi.fn();
    render(<PickerHarness rangeAvailability={{ state: 'error' }} onApplyRange={onApplyRange} />);
    expect(screen.getByText('可回测范围暂不可用')).toBeTruthy();
    expect(screen.queryByText('正在读取')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '开始日期年份' }));
    fireEvent.click(screen.getByRole('option', { name: '2025' }));
    fireEvent.click(screen.getByRole('button', { name: '应用' }));
    expect(screen.getByRole('alert').textContent).toContain('行情数据异常，请联系管理员');
    expect(onApplyRange).not.toHaveBeenCalled();
  });
});

describe('buildPresetRange', () => {
  it('builds inclusive calendar presets from the latest available date', () => {
    expect(buildPresetRange('one_month', '2015-01-05', '2026-09-18')).toEqual({
      startDate: '2026-08-19',
      endDate: '2026-09-18',
    });
    expect(buildPresetRange('three_months', '2015-01-05', '2026-09-18')).toEqual({
      startDate: '2026-06-19',
      endDate: '2026-09-18',
    });
  });

  it('clamps presets to the available range', () => {
    expect(buildPresetRange('year_to_date', '2026-03-05', '2026-09-18')).toEqual({
      startDate: '2026-03-05',
      endDate: '2026-09-18',
    });
    expect(buildPresetRange('all', '2015-01-05', '2026-09-18')).toEqual({
      startDate: '2015-01-05',
      endDate: '2026-09-18',
    });
  });
});

describe('replaceDatePart', () => {
  it('clamps leap days when the year changes', () => {
    expect(replaceDatePart('2024-02-29', 'year', 2025)).toBe('2025-02-28');
  });

  it('clamps the day when the month changes', () => {
    expect(replaceDatePart('2026-01-31', 'month', 2)).toBe('2026-02-28');
  });

  it('does not use market-data boundaries when changing a valid date', () => {
    expect(replaceDatePart('2026-09-18', 'year', 2036)).toBe('2036-09-18');
  });
});
