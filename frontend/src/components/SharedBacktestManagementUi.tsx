import type { ReactNode } from 'react';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';

export const MANAGEMENT_PAGE_SIZE = 20;

export const managementFieldStyle = {
  width: '100%',
  boxSizing: 'border-box',
  border: '1px solid #cbd5e1',
  borderRadius: 6,
  padding: '8px 10px',
} as const;

export const managementCardStyle = {
  border: '1px solid #d7dde8',
  borderRadius: 8,
  background: '#ffffff',
  padding: 18,
} as const;

export const managementStatusLabels: Record<SharedBacktestStatus, string> = {
  active: '已上架',
  withdrawn: '已撤回',
  removed: '已下架',
};

export function managementErrorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export function SharedBacktestStatusFilter(props: {
  ariaLabel: string;
  value: SharedBacktestStatus | '';
  onChange: (value: SharedBacktestStatus | '') => void;
}) {
  return (
    <label style={{ maxWidth: 220 }}>
      状态
      <select
        aria-label={props.ariaLabel}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value as SharedBacktestStatus | '')}
        style={managementFieldStyle}
      >
        <option value="">全部状态</option>
        <option value="active">已上架</option>
        <option value="withdrawn">已撤回</option>
        <option value="removed">已下架</option>
      </select>
    </label>
  );
}

export function SharedBacktestManagementPagination(props: {
  total: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  trailingAction?: ReactNode;
}) {
  if (props.total <= MANAGEMENT_PAGE_SIZE) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <button
        type="button"
        disabled={props.offset === 0}
        onClick={() => props.onOffsetChange(Math.max(0, props.offset - MANAGEMENT_PAGE_SIZE))}
      >上一页</button>
      <span>
        {Math.floor(props.offset / MANAGEMENT_PAGE_SIZE) + 1}
        {' / '}
        {Math.ceil(props.total / MANAGEMENT_PAGE_SIZE)}
      </span>
      <button
        type="button"
        disabled={props.offset + MANAGEMENT_PAGE_SIZE >= props.total}
        onClick={() => props.onOffsetChange(props.offset + MANAGEMENT_PAGE_SIZE)}
      >下一页</button>
      {props.trailingAction}
    </div>
  );
}

export function nextManagementItems(
  items: SharedBacktestManagementItem[],
  updated: SharedBacktestManagementItem,
  status: SharedBacktestStatus | '',
): SharedBacktestManagementItem[] {
  if (status && updated.status !== status) {
    return items.filter((item) => item.id !== updated.id);
  }
  return items.map((item) => item.id === updated.id ? updated : item);
}
