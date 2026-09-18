import type { SignalDetailExportTask } from '../types';
import { signalDetailExportDownloadUrl } from '../api/client';


interface Props {
  open: boolean;
  tasks: SignalDetailExportTask[];
  loading: boolean;
  onClose: () => void;
  onCancel: (exportId: string) => Promise<unknown>;
  onRetry: (exportId: string) => Promise<unknown>;
  onRefresh: () => Promise<unknown>;
}

const STATUS_LABELS: Record<string, string> = {
  queued: '等待生成',
  preparing: '正在准备',
  running: '正在生成',
  succeeded: '已完成',
  failed: '失败',
  cancelling: '正在取消',
  cancelled: '已取消',
  expired: '信号明细文件已过期',
};

const EXCLUSION_LABELS: Record<string, string> = {
  run_not_succeeded: '非成功回测',
  result_missing: '结果缺失',
  result_incompatible: '结果格式不兼容',
  no_signal_details: '没有信号明细',
};

function taskProgress(task: SignalDetailExportTask): string {
  if (task.status === 'preparing') {
    return `正在准备信号明细：${task.preparedRunCount} / ${task.selectedRunCount}`;
  }
  if (task.status === 'running') {
    return `正在生成信号明细：${task.processedRunCount} / ${task.totalRunCount ?? 0}`;
  }
  if (task.status === 'expired' && task.errorCode === 'artifact_missing') {
    return '信号明细文件不可用';
  }
  if (task.status === 'succeeded' && !canDownload(task)) {
    return '信号明细文件已过期';
  }
  return STATUS_LABELS[task.status] || task.status;
}

function formatBytes(value: number | null): string {
  if (value == null) return '--';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function formatTime(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString('zh-CN') : '--';
}

function canRetry(task: SignalDetailExportTask): boolean {
  if (task.status === 'cancelled' || task.status === 'expired') return true;
  return task.status === 'failed' && [
    'storage_unavailable', 'archive_failed', 'runner_interrupted',
  ].includes(task.errorCode || '');
}

function canDownload(task: SignalDetailExportTask): boolean {
  if (task.status !== 'succeeded' || !task.expiresAt) return false;
  const expiresAt = Date.parse(task.expiresAt);
  return Number.isFinite(expiresAt) && expiresAt > Date.now();
}

export default function SignalDetailExportDrawer({
  open,
  tasks,
  loading,
  onClose,
  onCancel,
  onRetry,
  onRefresh,
}: Props) {
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="信号明细导出记录"
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(15, 23, 42, 0.24)',
        display: 'flex', justifyContent: 'flex-end',
      }}
      onClick={onClose}
    >
      <section
        style={{
          width: 'min(760px, 92vw)', height: '100%', background: '#ffffff',
          borderLeft: '1px solid #cbd5e1', boxShadow: '-8px 0 24px rgba(15,23,42,.12)',
          display: 'flex', flexDirection: 'column',
        }}
        onClick={(event) => event.stopPropagation()}
      >
        <header style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '16px 18px', borderBottom: '1px solid #e2e8f0',
        }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18 }}>信号明细导出记录</h2>
            <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
              生成完成后在这里下载信号明细 ZIP
            </div>
          </div>
          <span className="signal-export-actions">
            <button
              type="button"
              className="app-button is-quiet"
              onClick={() => void onRefresh()}
              disabled={loading}
            >
              刷新
            </button>
            <button
              type="button"
              className="app-button is-quiet"
              onClick={onClose}
              aria-label="关闭信号明细导出记录"
            >
              关闭
            </button>
          </span>
        </header>
        <div style={{ padding: 16, overflow: 'auto', display: 'grid', gap: 12 }}>
          {tasks.length === 0 && (
            <div style={{ color: '#64748b', padding: 24, textAlign: 'center' }}>
              暂无信号明细导出记录
            </div>
          )}
          {tasks.map((task) => (
            <article key={task.id} style={{
              border: '1px solid #e2e8f0', borderRadius: 6, padding: 14,
              display: 'grid', gap: 8,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                <strong>{task.selectionMode === 'filter' ? '当前筛选全部' : '手动选择'}</strong>
                <span style={{ color: '#64748b', fontSize: 12 }}>{formatTime(task.createdAt)}</span>
              </div>
              <div style={{ fontSize: 13 }}>{taskProgress(task)}</div>
              <div style={{ color: '#64748b', fontSize: 12 }}>
                选中 {task.selectedRunCount} 条 · 导出 {task.totalRunCount ?? '计算中'} 条
                {task.excludedRunCount != null ? ` · 排除 ${task.excludedRunCount} 条` : ''}
                {task.byteSize != null ? ` · ${formatBytes(task.byteSize)}` : ''}
              </div>
              {task.exclusionSummary && Object.keys(task.exclusionSummary).length > 0 && (
                <div style={{ color: '#92400e', fontSize: 12 }}>
                  {Object.entries(task.exclusionSummary).map(([reason, count]) => (
                    `${EXCLUSION_LABELS[reason] || reason} ${count} 条`
                  )).join('，')}
                </div>
              )}
              {task.errorMessage && (
                <div style={{ color: '#b91c1c', fontSize: 12 }}>{task.errorMessage}</div>
              )}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#64748b', fontSize: 12 }}>
                  {task.status === 'succeeded' ? `有效期至 ${formatTime(task.expiresAt)}` : ''}
                </span>
                <span className="signal-export-actions">
                  {['queued', 'preparing', 'running'].includes(task.status) && (
                    <button
                      type="button"
                      className="app-button is-danger is-compact"
                      onClick={() => void onCancel(task.id)}
                    >
                      取消生成
                    </button>
                  )}
                  {canDownload(task) && (
                    <a
                      className="app-button is-primary is-compact"
                      href={signalDetailExportDownloadUrl(task.id)}
                      download={task.downloadFilename || undefined}
                    >
                      下载信号明细 ZIP
                    </a>
                  )}
                  {canRetry(task) && (
                    <button
                      type="button"
                      className="app-button is-primary is-compact"
                      onClick={() => void onRetry(task.id)}
                    >
                      重新导出
                    </button>
                  )}
                </span>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
