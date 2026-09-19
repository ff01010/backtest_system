import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

type DateEndpoint = 'startDate' | 'endDate';
type DatePart = 'year' | 'month' | 'day';

interface Props {
  startDate: string;
  endDate: string;
  dataStartDate?: string | null;
  dataEndDate?: string | null;
  onApplyRange: (startDate: string, endDate: string) => void;
}

interface DateParts {
  day: number;
  month: number;
  year: number;
}

interface ActiveWheel {
  activationId: number;
  anchor: HTMLButtonElement;
  endpoint: DateEndpoint;
  part: DatePart;
}

interface DateControlProps {
  activePart: DatePart | null;
  endpoint: DateEndpoint;
  label: string;
  value: string;
  wheelId: string;
  onOpenWheel: (endpoint: DateEndpoint, part: DatePart, anchor: HTMLButtonElement) => void;
}

interface WheelListProps {
  label: string;
  selectedValue: number;
  values: readonly number[];
  onSelect: (value: number) => void;
}

const MIN_YEAR = 1000;
const MAX_YEAR = 9999;
const WHEEL_ROW_HEIGHT = 32;
const WHEEL_VISIBLE_ROWS = 7;
const WHEEL_BUFFER_ROWS = 3;
const WHEEL_HEIGHT = WHEEL_ROW_HEIGHT * WHEEL_VISIBLE_ROWS;
const WHEEL_PADDING = WHEEL_ROW_HEIGHT * Math.floor(WHEEL_VISIBLE_ROWS / 2);
const VIEWPORT_MARGIN = 8;
const YEAR_OPTIONS = Array.from({ length: MAX_YEAR - MIN_YEAR + 1 }, (_, index) => MIN_YEAR + index);
const MONTH_OPTIONS = Array.from({ length: 12 }, (_, index) => index + 1);
const PRESETS = [
  { key: 'one_month', label: '近1月' },
  { key: 'three_months', label: '近3月' },
] as const;

function parseDate(value: string): DateParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < MIN_YEAR || year > MAX_YEAR || month < 1 || month > 12) return null;
  const lastDay = new Date(year, month, 0).getDate();
  return day >= 1 && day <= lastDay ? { year, month, day } : null;
}

function formatDate({ year, month, day }: DateParts): string {
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function lastDayOfMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

function shiftDate(value: string, days: number): string {
  const parsed = parseDate(value);
  if (!parsed) return value;
  const date = new Date(parsed.year, parsed.month - 1, parsed.day);
  date.setDate(date.getDate() + days);
  return formatDate({ year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate() });
}

function shiftDateMonths(value: string, months: number): string {
  const parsed = parseDate(value);
  if (!parsed) return value;
  const target = new Date(parsed.year, parsed.month - 1 + months, 1);
  return formatDate({
    year: target.getFullYear(),
    month: target.getMonth() + 1,
    day: Math.min(parsed.day, lastDayOfMonth(target.getFullYear(), target.getMonth() + 1)),
  });
}

export function buildPresetRange(
  key: 'one_month' | 'three_months' | 'year_to_date' | 'all',
  dataStartDate: string | null | undefined,
  dataEndDate: string | null | undefined,
): { startDate: string; endDate: string } | null {
  const dataStart = dataStartDate ? parseDate(dataStartDate) : null;
  const dataEnd = dataEndDate ? parseDate(dataEndDate) : null;
  if (!dataStart || !dataEnd || dataStartDate! > dataEndDate!) return null;
  if (key === 'all') return { startDate: dataStartDate!, endDate: dataEndDate! };
  const candidate = key === 'year_to_date'
    ? `${dataEnd.year}-01-01`
    : shiftDate(shiftDateMonths(dataEndDate!, key === 'one_month' ? -1 : -3), 1);
  return {
    startDate: candidate < dataStartDate! ? dataStartDate! : candidate,
    endDate: dataEndDate!,
  };
}

export function replaceDatePart(value: string, part: DatePart, nextPartValue: number): string {
  const parsed = parseDate(value);
  if (!parsed) return value;

  if (part === 'year') {
    if (nextPartValue < MIN_YEAR || nextPartValue > MAX_YEAR) return value;
    return formatDate({
      year: nextPartValue,
      month: parsed.month,
      day: Math.min(parsed.day, lastDayOfMonth(nextPartValue, parsed.month)),
    });
  }

  if (part === 'month') {
    if (nextPartValue < 1 || nextPartValue > 12) return value;
    return formatDate({
      year: parsed.year,
      month: nextPartValue,
      day: Math.min(parsed.day, lastDayOfMonth(parsed.year, nextPartValue)),
    });
  }

  const maxDay = lastDayOfMonth(parsed.year, parsed.month);
  if (nextPartValue < 1 || nextPartValue > maxDay) return value;
  return formatDate({ ...parsed, day: nextPartValue });
}

function optionLabel(part: DatePart, value: number): string {
  return part === 'year' ? String(value).padStart(4, '0') : String(value).padStart(2, '0');
}

function WheelList(props: WheelListProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const selectedIndex = Math.max(0, props.values.indexOf(props.selectedValue));
  const selectedValueRef = useRef(props.selectedValue);
  const [visibleStart, setVisibleStart] = useState(() => Math.max(0, selectedIndex - WHEEL_VISIBLE_ROWS));
  const visibleEnd = Math.min(props.values.length, visibleStart + WHEEL_VISIBLE_ROWS + WHEEL_BUFFER_ROWS * 2);

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = selectedIndex * WHEEL_ROW_HEIGHT;
  }, []);

  useEffect(() => {
    selectedValueRef.current = props.selectedValue;
  }, [props.selectedValue]);

  const updateFromScroll = (): void => {
    const node = scrollRef.current;
    if (!node) return;
    const centeredIndex = Math.min(
      props.values.length - 1,
      Math.max(0, Math.round(node.scrollTop / WHEEL_ROW_HEIGHT)),
    );
    const firstVisible = Math.max(
      0,
      Math.floor((node.scrollTop - WHEEL_PADDING) / WHEEL_ROW_HEIGHT) - WHEEL_BUFFER_ROWS,
    );
    setVisibleStart(firstVisible);
    const nextValue = props.values[centeredIndex];
    if (nextValue !== selectedValueRef.current) {
      selectedValueRef.current = nextValue;
      props.onSelect(nextValue);
    }
  };

  const centerOption = (index: number, value: number): void => {
    selectedValueRef.current = value;
    props.onSelect(value);
    const node = scrollRef.current;
    if (!node) return;
    if (typeof node.scrollTo === 'function') {
      node.scrollTo({ top: index * WHEEL_ROW_HEIGHT, behavior: 'smooth' });
    } else {
      node.scrollTop = index * WHEEL_ROW_HEIGHT;
    }
  };

  return (
    <div
      ref={scrollRef}
      role="listbox"
      tabIndex={0}
      className="backtest-date-wheel-scroll"
      aria-label={props.label}
      onScroll={updateFromScroll}
    >
      <div
        className="backtest-date-wheel-spacer"
        style={{ height: props.values.length * WHEEL_ROW_HEIGHT + WHEEL_PADDING * 2 }}
      >
        {props.values.slice(visibleStart, visibleEnd).map((value, offset) => {
          const index = visibleStart + offset;
          const selected = value === props.selectedValue;
          return (
            <button
              key={value}
              type="button"
              role="option"
              aria-selected={selected}
              className={`backtest-date-wheel-option${selected ? ' is-selected' : ''}`}
              style={{ top: WHEEL_PADDING + index * WHEEL_ROW_HEIGHT }}
              onClick={() => centerOption(index, value)}
            >
              {String(value).padStart(value > 31 ? 4 : 2, '0')}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DateControl(props: DateControlProps) {
  const parsed = parseDate(props.value);
  const parts = parsed || { year: 0, month: 0, day: 0 };
  const endpointLabel = `${props.label}日期`;

  const renderPart = (part: DatePart, value: number) => {
    const partLabel = part === 'year' ? '年份' : part === 'month' ? '月份' : '日期';
    const expanded = props.activePart === part;
    return (
      <button
        type="button"
        className={`backtest-date-segment${expanded ? ' is-active' : ''}`}
        aria-label={`${endpointLabel}${partLabel}`}
        aria-haspopup="listbox"
        aria-expanded={expanded}
        aria-controls={expanded ? props.wheelId : undefined}
        title={`点击选择${endpointLabel}${partLabel}`}
        onClick={(event) => props.onOpenWheel(props.endpoint, part, event.currentTarget)}
      >
        {optionLabel(part, value)}
      </button>
    );
  };

  return (
    <div className="backtest-date-trigger" role="group" aria-label={`${endpointLabel} ${props.value}`}>
      <span className="backtest-date-trigger-label">{props.label}</span>
      <strong className="backtest-date-value">
        {renderPart('year', parts.year)}
        <span className="backtest-date-separator" aria-hidden="true">-</span>
        {renderPart('month', parts.month)}
        <span className="backtest-date-separator" aria-hidden="true">-</span>
        {renderPart('day', parts.day)}
      </strong>
    </div>
  );
}

export default function BacktestDateRangePicker(props: Props) {
  const [draft, setDraft] = useState({ startDate: props.startDate, endDate: props.endDate });
  const [activeWheel, setActiveWheel] = useState<ActiveWheel | null>(null);
  const [wheelPosition, setWheelPosition] = useState({ left: 0, top: 0, width: 68 });
  const wheelRef = useRef<HTMLDivElement | null>(null);
  const activationSequenceRef = useRef(0);
  const wheelId = useId();
  const hasChanges = draft.startDate !== props.startDate || draft.endDate !== props.endDate;

  useEffect(() => {
    setDraft({ startDate: props.startDate, endDate: props.endDate });
    setActiveWheel(null);
  }, [props.endDate, props.startDate]);

  const updateWheelPosition = useCallback((): void => {
    if (!activeWheel?.anchor.isConnected) return;
    const anchorRect = activeWheel.anchor.getBoundingClientRect();
    const width = activeWheel.part === 'year' ? 84 : 68;
    const left = Math.min(
      Math.max(anchorRect.left + anchorRect.width / 2 - width / 2, VIEWPORT_MARGIN),
      window.innerWidth - width - VIEWPORT_MARGIN,
    );
    const top = Math.min(
      Math.max(anchorRect.top + anchorRect.height / 2 - WHEEL_HEIGHT / 2, VIEWPORT_MARGIN),
      window.innerHeight - WHEEL_HEIGHT - VIEWPORT_MARGIN,
    );
    setWheelPosition({ left, top, width });
  }, [activeWheel]);

  useLayoutEffect(() => {
    if (activeWheel) updateWheelPosition();
  }, [activeWheel, updateWheelPosition]);

  useEffect(() => {
    if (!activeWheel) return undefined;
    const closeOnOutsidePointer = (event: PointerEvent): void => {
      const target = event.target as Node | null;
      if (target && (activeWheel.anchor.contains(target) || wheelRef.current?.contains(target))) return;
      setActiveWheel(null);
    };
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setActiveWheel(null);
    };
    const repositionOnScroll = (event: Event): void => {
      const target = event.target as Node | null;
      if (target && wheelRef.current?.contains(target)) return;
      updateWheelPosition();
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    document.addEventListener('keydown', closeOnEscape);
    window.addEventListener('resize', updateWheelPosition);
    window.addEventListener('scroll', repositionOnScroll, true);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer);
      document.removeEventListener('keydown', closeOnEscape);
      window.removeEventListener('resize', updateWheelPosition);
      window.removeEventListener('scroll', repositionOnScroll, true);
    };
  }, [activeWheel, updateWheelPosition]);

  const openWheel = (endpoint: DateEndpoint, part: DatePart, anchor: HTMLButtonElement): void => {
    if (activeWheel?.endpoint === endpoint && activeWheel.part === part) {
      setActiveWheel(null);
      return;
    }
    activationSequenceRef.current += 1;
    setActiveWheel({ endpoint, part, anchor, activationId: activationSequenceRef.current });
  };

  const applyEndpointValue = (endpoint: DateEndpoint, nextValue: string): void => {
    setDraft((current) => ({ ...current, [endpoint]: nextValue }));
  };

  const activeDate = activeWheel?.endpoint === 'startDate' ? draft.startDate : draft.endDate;
  const activeParts = parseDate(activeDate);
  const values = useMemo(() => {
    if (!activeWheel || !activeParts) return [];
    if (activeWheel.part === 'year') return YEAR_OPTIONS;
    if (activeWheel.part === 'month') return MONTH_OPTIONS;
    return Array.from(
      { length: lastDayOfMonth(activeParts.year, activeParts.month) },
      (_, index) => index + 1,
    );
  }, [activeParts?.month, activeParts?.year, activeWheel?.part]);
  const selectedValue = activeWheel && activeParts ? activeParts[activeWheel.part] : 0;

  const selectWheelValue = (value: number): void => {
    if (!activeWheel) return;
    const currentValue = activeWheel.endpoint === 'startDate' ? draft.startDate : draft.endDate;
    applyEndpointValue(activeWheel.endpoint, replaceDatePart(currentValue, activeWheel.part, value));
  };

  const applyPreset = (key: 'one_month' | 'three_months' | 'year_to_date' | 'all'): void => {
    const range = buildPresetRange(key, props.dataStartDate, props.dataEndDate);
    if (!range) return;
    setDraft(range);
    setActiveWheel(null);
  };

  const useEarliestDate = (): void => {
    if (!props.dataStartDate || !parseDate(props.dataStartDate)) return;
    setDraft((current) => ({
      startDate: props.dataStartDate!,
      endDate: current.endDate,
    }));
    setActiveWheel(null);
  };

  const cancelChanges = (): void => {
    setDraft({ startDate: props.startDate, endDate: props.endDate });
    setActiveWheel(null);
  };

  const applyChanges = (): void => {
    props.onApplyRange(draft.startDate, draft.endDate);
    setActiveWheel(null);
  };

  const rangeLabel = props.dataStartDate && props.dataEndDate
    ? `${props.dataStartDate} — ${props.dataEndDate}`
    : '正在读取';

  return (
    <div className="backtest-date-range-picker">
      <div className="backtest-date-coverage">
        <span>可回测范围：</span>
        <strong>{rangeLabel}</strong>
      </div>
      <div className="backtest-date-presets" aria-label="快捷回测区间">
        {PRESETS.map((preset) => (
          <button
            key={preset.key}
            type="button"
            disabled={!buildPresetRange(preset.key, props.dataStartDate, props.dataEndDate)}
            onClick={() => applyPreset(preset.key)}
          >
            {preset.label}
          </button>
        ))}
        <button
          type="button"
          disabled={!buildPresetRange('year_to_date', props.dataStartDate, props.dataEndDate)}
          onClick={() => applyPreset('year_to_date')}
        >
          今年
        </button>
        <button
          type="button"
          disabled={!buildPresetRange('all', props.dataStartDate, props.dataEndDate)}
          onClick={() => applyPreset('all')}
        >
          全部数据
        </button>
      </div>
      <div className="backtest-date-trigger-grid">
        <DateControl
          endpoint="startDate"
          label="开始"
          value={draft.startDate}
          wheelId={wheelId}
          activePart={activeWheel?.endpoint === 'startDate' ? activeWheel.part : null}
          onOpenWheel={openWheel}
        />
        <DateControl
          endpoint="endDate"
          label="结束"
          value={draft.endDate}
          wheelId={wheelId}
          activePart={activeWheel?.endpoint === 'endDate' ? activeWheel.part : null}
          onOpenWheel={openWheel}
        />
      </div>
      <div className="backtest-date-footer">
        <button
          type="button"
          className="backtest-date-earliest"
          disabled={!props.dataStartDate || !parseDate(props.dataStartDate)}
          onClick={useEarliestDate}
        >
          从最早日期开始
        </button>
        <div className="backtest-date-actions">
          <button type="button" className="app-button is-quiet" disabled={!hasChanges} onClick={cancelChanges}>取消</button>
          <button type="button" className="app-button is-primary" disabled={!hasChanges} onClick={applyChanges}>应用</button>
        </div>
      </div>
      {activeWheel && activeParts && createPortal(
        <div
          key={activeWheel.activationId}
          id={wheelId}
          ref={wheelRef}
          className="backtest-date-wheel"
          style={wheelPosition}
        >
          <div className="backtest-date-wheel-selection" aria-hidden="true" />
          <WheelList
            label={`${activeWheel.endpoint === 'startDate' ? '开始日期' : '结束日期'}${activeWheel.part === 'year' ? '年份' : activeWheel.part === 'month' ? '月份' : '日期'}选项`}
            selectedValue={selectedValue}
            values={values}
            onSelect={selectWheelValue}
          />
          <div className="backtest-date-wheel-fade" aria-hidden="true" />
        </div>,
        document.body,
      )}
    </div>
  );
}
