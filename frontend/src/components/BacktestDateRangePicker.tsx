import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

export interface BacktestCalendarDay {
  date: string;
  tradingDay: boolean;
  status: 'available' | 'incomplete' | 'future' | 'non_trading' | 'unknown';
}

interface Props {
  startDate: string;
  endDate: string;
  days: BacktestCalendarDay[];
  onChange: (key: 'startDate' | 'endDate', value: string) => void;
  onMonthChange: (month: string) => void;
}

interface PopoverPosition {
  left: number;
  maxHeight: number;
  top: number;
}

const POPOVER_FALLBACK_HEIGHT = 330;
const POPOVER_FALLBACK_WIDTH = 244;
const POPOVER_GAP = 6;
const VIEWPORT_MARGIN = 8;

function monthStart(month: string): Date {
  return new Date(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 1, 1);
}

function shiftMonth(month: string, offset: number): string {
  const value = monthStart(month);
  value.setMonth(value.getMonth() + offset);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`;
}

function formatDate(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

export default function BacktestDateRangePicker({ startDate, endDate, days, onChange, onMonthChange }: Props) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<'startDate' | 'endDate'>('startDate');
  const [month, setMonth] = useState(startDate.slice(0, 7));
  const [popoverPosition, setPopoverPosition] = useState<PopoverPosition>({ left: 0, maxHeight: POPOVER_FALLBACK_HEIGHT, top: 0 });
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const popoverId = useId();
  const dayByDate = useMemo(() => new Map(days.map((item) => [item.date, item])), [days]);

  const updatePopoverPosition = useCallback((): void => {
    const anchor = anchorRef.current;
    if (!anchor) return;

    const anchorRect = anchor.getBoundingClientRect();
    const popoverWidth = popoverRef.current?.offsetWidth || POPOVER_FALLBACK_WIDTH;
    const popoverHeight = popoverRef.current?.offsetHeight || POPOVER_FALLBACK_HEIGHT;
    const spaceBelow = window.innerHeight - anchorRect.bottom - POPOVER_GAP - VIEWPORT_MARGIN;
    const spaceAbove = anchorRect.top - POPOVER_GAP - VIEWPORT_MARGIN;
    const showAbove = spaceBelow < popoverHeight && spaceAbove > spaceBelow;
    const availableHeight = Math.max(96, showAbove ? spaceAbove : spaceBelow);
    const renderedHeight = Math.min(popoverHeight, availableHeight);
    const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - popoverWidth - VIEWPORT_MARGIN);
    const left = Math.min(Math.max(anchorRect.left, VIEWPORT_MARGIN), maxLeft);
    const top = showAbove
      ? Math.max(VIEWPORT_MARGIN, anchorRect.top - POPOVER_GAP - renderedHeight)
      : Math.min(anchorRect.bottom + POPOVER_GAP, window.innerHeight - VIEWPORT_MARGIN - renderedHeight);

    setPopoverPosition({ left, maxHeight: availableHeight, top });
  }, []);

  useEffect(() => {
    if (open) onMonthChange(month);
  }, [month, onMonthChange, open]);

  useLayoutEffect(() => {
    if (!open) return;
    updatePopoverPosition();
  }, [month, open, updatePopoverPosition]);

  useEffect(() => {
    if (!open) return undefined;

    const closeOnOutsidePointer = (event: PointerEvent): void => {
      const targetElement = event.target as Node | null;
      if (targetElement && (anchorRef.current?.contains(targetElement) || popoverRef.current?.contains(targetElement))) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setOpen(false);
    };

    document.addEventListener('pointerdown', closeOnOutsidePointer);
    document.addEventListener('keydown', closeOnEscape);
    window.addEventListener('resize', updatePopoverPosition);
    window.addEventListener('scroll', updatePopoverPosition, true);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer);
      document.removeEventListener('keydown', closeOnEscape);
      window.removeEventListener('resize', updatePopoverPosition);
      window.removeEventListener('scroll', updatePopoverPosition, true);
    };
  }, [open, updatePopoverPosition]);

  const choose = (value: string): void => {
    const status = dayByDate.get(value)?.status;
    if (status !== 'available') return;
    if (target === 'startDate') {
      onChange('startDate', value);
      if (value > endDate) onChange('endDate', value);
      setTarget('endDate');
      return;
    }
    onChange('endDate', value);
    if (value < startDate) onChange('startDate', value);
    setTarget('startDate');
  };

  const renderMonth = (value: string) => {
    const first = monthStart(value);
    const padding = first.getDay() === 0 ? 6 : first.getDay() - 1;
    const last = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    const cells = Array.from({ length: padding + last }, (_, index) => index < padding ? null : index - padding + 1);
    return (
      <div style={{ minWidth: 218 }}>
        <div style={{ color: '#0f172a', fontSize: 12, fontWeight: 700, marginBottom: 8 }}>{value}</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 3 }}>
          {['一', '二', '三', '四', '五', '六', '日'].map((label) => <span key={label} style={{ color: '#94a3b8', fontSize: 10, textAlign: 'center' }}>{label}</span>)}
          {cells.map((day, index) => {
            if (day === null) return <span key={`empty-${index}`} />;
            const date = formatDate(new Date(first.getFullYear(), first.getMonth(), day));
            const status = dayByDate.get(date)?.status || 'unknown';
            const selected = date === startDate || date === endDate;
            const unavailable = status !== 'available';
            return (
              <button
                key={date}
                type="button"
                disabled={unavailable}
                onClick={() => choose(date)}
                title={status === 'future' ? '未来交易日不可用于回测' : status === 'incomplete' ? '该交易日数据不完整' : status === 'unknown' ? '正在检查数据可用性' : undefined}
                style={{ height: 27, border: 0, borderRadius: 3, background: selected ? '#0f766e' : unavailable ? '#e2e8f0' : '#f8fafc', color: selected ? '#ffffff' : unavailable ? '#94a3b8' : '#334155', cursor: unavailable ? 'not-allowed' : 'pointer', fontSize: 11 }}
              >
                {day}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div ref={anchorRef} style={{ position: 'relative' }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {(['startDate', 'endDate'] as const).map((key) => (
          <button
            key={key}
            type="button"
            aria-controls={popoverId}
            aria-expanded={target === key && open}
            onClick={() => {
              setTarget(key);
              setMonth((key === 'startDate' ? startDate : endDate).slice(0, 7));
              setOpen(true);
            }}
            style={{ height: 34, border: `1px solid ${target === key && open ? '#0f766e' : '#cbd5e1'}`, borderRadius: 4, background: '#f8fafc', color: '#334155', textAlign: 'left', padding: '0 10px', fontSize: 12 }}
          >
            {key === 'startDate' ? '开始：' : '结束：'}{key === 'startDate' ? startDate : endDate}
          </button>
        ))}
      </div>
      {open && createPortal(
        <div
          id={popoverId}
          ref={popoverRef}
          role="dialog"
          aria-label="回测日期选择"
          style={{ position: 'fixed', zIndex: 70, top: popoverPosition.top, left: popoverPosition.left, maxHeight: popoverPosition.maxHeight, overflowY: 'auto', padding: 12, background: '#ffffff', border: '1px solid #cbd5e1', borderRadius: 6, boxShadow: '0 12px 28px rgba(15, 23, 42, 0.18)' }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <button type="button" onClick={() => setMonth(shiftMonth(month, -1))} style={{ border: 0, background: 'transparent', color: '#0f766e', cursor: 'pointer' }}>‹</button>
            <span style={{ color: '#64748b', fontSize: 11 }}>灰色日期不可选</span>
            <button type="button" onClick={() => setMonth(shiftMonth(month, 1))} style={{ border: 0, background: 'transparent', color: '#0f766e', cursor: 'pointer' }}>›</button>
          </div>
          {renderMonth(month)}
          <button type="button" onClick={() => setOpen(false)} style={{ width: '100%', marginTop: 10, height: 28, border: '1px solid #cbd5e1', borderRadius: 3, background: '#f8fafc', color: '#475569', fontSize: 11 }}>关闭</button>
        </div>,
        document.body
      )}
    </div>
  );
}
