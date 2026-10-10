const STATUS_LABELS: Record<string, string> = {
  preparing: '准备中',
  generation_blocked: '生成受阻',
  pending: '等待中',
  queued: '排队中',
  running: '运行中',
  cancelling: '取消中',
  cancel_requested: '取消中',
  completed: '完成',
  partially_completed: '部分完成',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  expired: '已过期',
  materialized: '已生成',
  cancelled_before_materialization: '生成前已取消',
};

const FILL_MODEL_LABELS: Record<string, string> = {
  bar_next_open: 'Bar 下一根开盘价',
  l2_quote: 'L2 行情撮合',
};

const DATE_ROLE_LABELS: Record<string, string> = {
  development: '开发',
  validation: '验证',
  reference: '参考',
};

export function statusLabel(value: unknown): string {
  const status = String(value ?? '');
  return (STATUS_LABELS[status] ?? status) || '—';
}

export function fillModelLabel(value: unknown): string {
  const fillModel = String(value ?? '');
  return (FILL_MODEL_LABELS[fillModel] ?? fillModel) || '—';
}

export function dateRoleLabel(value: unknown): string {
  const role = String(value ?? '');
  return (DATE_ROLE_LABELS[role] ?? role) || '—';
}
