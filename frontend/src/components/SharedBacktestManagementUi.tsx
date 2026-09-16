import type { ReactNode } from 'react';
import type { SharedBacktestManagementItem, SharedBacktestStatus } from '../types';

export const MANAGEMENT_PAGE_SIZE = 20;

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
    <label className="shared-field shared-management-filter">
      <span className="shared-field-label">状态</span>
      <select
        aria-label={props.ariaLabel}
        className="shared-select"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value as SharedBacktestStatus | '')}
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
    <div className="shared-pagination shared-management-pagination">
      <button
        type="button"
        className="shared-button is-quiet"
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
        className="shared-button is-quiet"
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
