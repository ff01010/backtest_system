import { Activity, Clock3, RefreshCw, ServerCog } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { BacktestResourceStatus, BacktestRunnerResourceStatus } from '../api/client';

type DiagnosisTone = 'ok' | 'warning' | 'danger';

interface Diagnosis {
  tone: DiagnosisTone;
  label: string;
  message: string;
}

interface Props {
  status: BacktestResourceStatus | null;
  loading: boolean;
  error: string;
  updatedAt: Date | null;
  onRefresh: () => void;
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return `${minutes} 分 ${remainder} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function formatOverrideExpiry(value: string | null): string {
  if (!value) return '';
  const expiresAt = new Date(value);
  if (Number.isNaN(expiresAt.getTime())) return value;
  return expiresAt.toLocaleString('zh-CN', { hour12: false });
}

function saturatedRunner(status: BacktestResourceStatus): 'Bar' | 'L2' | null {
  const pools: Array<['Bar' | 'L2', number, BacktestRunnerResourceStatus]> = [
    ['Bar', status.barActiveCount, status.runners.bar],
    ['L2', status.l2ActiveCount, status.runners.l2],
  ];
  return pools.find(([, active, runner]) => (
    runner.queued > 0 && runner.healthy > 0 && active >= runner.healthy
  ))?.[0] ?? null;
}

export function diagnoseBacktestResources(status: BacktestResourceStatus): Diagnosis {
  const queued = status.runners.bar.queued + status.runners.l2.queued;
  const errors = status.runners.bar.errors + status.runners.l2.errors;
  const saturated = saturatedRunner(status);
  if (status.runnerStatus === 'degraded' || errors > 0) {
    const message = errors > 0
      ? `发现 ${errors} 个异常 Runner，请先检查容器和心跳。`
      : '至少一类 Runner 没有有效心跳，请检查对应执行容器。';
    return { tone: 'danger', label: '执行器异常', message };
  }
  if (status.activeCount >= status.burstLimit && queued > 0) {
    return { tone: 'danger', label: '突发已满', message: '公平突发容量已经用尽，新增任务将继续排队。' };
  }
  if (saturated) {
    return { tone: 'warning', label: `${saturated} 执行池已满`, message: `${saturated} 任务仍在排队，但当前没有空闲的对应 Runner。` };
  }
  if (status.overrideLimit != null && status.activeCount >= status.effectiveLimit && queued > 0) {
    return { tone: 'warning', label: '临时容量已满', message: '当前受管理员临时容量限制，新增任务需要等待槽位释放。' };
  }
  if (status.overrideLimit != null && status.activeCount > status.effectiveLimit) {
    return { tone: 'warning', label: '容量回落中', message: '当前运行数高于临时容量，系统不会终止任务，将停止新领取并自然回落。' };
  }
  if (status.activeCount > status.baseLimit) {
    return { tone: 'warning', label: '公平突发中', message: '系统已超过基础容量，仅为尚未获得槽位的用户分配突发槽位。' };
  }
  if (status.activeCount >= status.baseLimit && queued > 0) {
    return { tone: 'warning', label: '基础容量已满', message: '零运行用户可按公平规则获得突发槽位，其他任务继续排队。' };
  }
  if (queued > 0) {
    return { tone: 'warning', label: '存在排队任务', message: '仍有基础槽位，任务正在等待对应执行器领取。' };
  }
  return { tone: 'ok', label: '运行正常', message: '当前没有任务积压，统一调度与执行器状态正常。' };
}

function RunnerRow({
  label, active, runner,
}: {
  label: string;
  active: number;
  runner: BacktestRunnerResourceStatus;
}) {
  return (
    <div className="backtest-resource-runner-row">
      <strong>{label}</strong>
      <span><small>运行中</small>{active}</span>
      <span><small>排队</small>{runner.queued}</span>
      <span><small>健康 Runner</small>{runner.healthy}</span>
      <span><small>异常 Runner</small>{runner.errors}</span>
      <span><small>最长等待</small>{formatDuration(runner.oldestQueuedSeconds)}</span>
    </div>
  );
}

function EmptyPanel({ loading, error, onRefresh }: Pick<Props, 'loading' | 'error' | 'onRefresh'>) {
  return (
    <section className="backtest-resource-panel is-unavailable" aria-label="回测资源状态">
      <div>
        <div className="backtest-resource-eyebrow">BACKTEST CAPACITY</div>
        <h3>回测资源状态</h3>
        <p>{error || '正在读取统一调度和 Runner 状态…'}</p>
      </div>
      {error && <button type="button" onClick={onRefresh}><RefreshCw size={14} />重新读取</button>}
      {loading && <span className="backtest-resource-loading">读取中…</span>}
    </section>
  );
}

function ResourceOverview({ status }: { status: BacktestResourceStatus }) {
  const queued = status.runners.bar.queued + status.runners.l2.queued;
  const healthy = status.runners.bar.healthy + status.runners.l2.healthy;
  const longestWait = Math.max(
    status.runners.bar.oldestQueuedSeconds ?? 0,
    status.runners.l2.oldestQueuedSeconds ?? 0,
  );
  const isBurst = status.overrideLimit == null && status.activeCount > status.baseLimit;
  const progressLimit = isBurst ? status.burstLimit : status.effectiveLimit;
  const progress = Math.min(100, (status.activeCount / Math.max(1, progressLimit)) * 100);
  return (
    <div className="backtest-resource-overview">
      <div className="backtest-resource-capacity">
        <span><Activity size={15} />全局执行槽位</span>
        <strong>{status.activeCount} <small>/ {status.effectiveLimit}{isBurst ? ` 基础，突发上限 ${status.burstLimit}` : ''}</small></strong>
        <div className="backtest-resource-progress" role="progressbar" aria-valuenow={status.activeCount} aria-valuemin={0} aria-valuemax={progressLimit}>
          <i style={{ width: `${progress}%` }} />
          {isBurst && <b style={{ left: `${(status.baseLimit / status.burstLimit) * 100}%` }} />}
        </div>
      </div>
      <div className="backtest-resource-stat"><ServerCog size={16} /><span>健康 Runner<strong>{healthy}</strong></span></div>
      <div className="backtest-resource-stat"><Activity size={16} /><span>待执行任务<strong>{queued}</strong></span></div>
      <div className="backtest-resource-stat"><Clock3 size={16} /><span>最长等待<strong>{longestWait ? formatDuration(longestWait) : '无排队'}</strong></span></div>
    </div>
  );
}

function RunnerDetails({ status, diagnosis, updatedAt }: {
  status: BacktestResourceStatus;
  diagnosis: Diagnosis;
  updatedAt: Date | null;
}) {
  const queued = status.runners.bar.queued + status.runners.l2.queued;
  const shouldAutoOpen = queued > 0 || diagnosis.tone === 'danger';
  const [isOpen, setIsOpen] = useState(shouldAutoOpen);
  useEffect(() => {
    if (shouldAutoOpen) setIsOpen(true);
  }, [shouldAutoOpen]);
  return (
    <details
      className="backtest-resource-details"
      open={isOpen}
      onToggle={(event) => setIsOpen(event.currentTarget.open)}
    >
      <summary>查看 Bar / L2 执行池</summary>
      <RunnerRow label="Bar" active={status.barActiveCount} runner={status.runners.bar} />
      <RunnerRow label="L2" active={status.l2ActiveCount} runner={status.runners.l2} />
      <footer>
        基础容量 {status.baseLimit} · 公平突发上限 {status.burstLimit}
        {status.overrideLimit != null
          ? ` · 临时容量 ${status.overrideLimit}，至 ${formatOverrideExpiry(status.overrideExpiresAt)}`
          : ''}
        {updatedAt ? ` · 更新于 ${updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}` : ''}
      </footer>
    </details>
  );
}

export default function BacktestResourceStatusPanel(props: Props) {
  if (!props.status) return <EmptyPanel {...props} />;
  const { status } = props;
  const diagnosis = diagnoseBacktestResources(status);
  return (
    <section className={`backtest-resource-panel is-${diagnosis.tone}`} aria-label="回测资源状态">
      <header className="backtest-resource-header">
        <div>
          <div className="backtest-resource-eyebrow">BACKTEST CAPACITY</div>
          <h3>回测资源状态</h3>
          <p>统一槽位控制实际任务并发；Runner 数量表示候选执行能力。</p>
        </div>
        <div className="backtest-resource-actions">
          <span className={`backtest-resource-state is-${diagnosis.tone}`}>{diagnosis.label}</span>
          <button type="button" onClick={props.onRefresh} disabled={props.loading} aria-label="刷新回测资源状态">
            <RefreshCw size={14} />{props.loading ? '读取中' : '刷新'}
          </button>
        </div>
      </header>
      <ResourceOverview status={status} />
      <div className={`backtest-resource-diagnosis is-${diagnosis.tone}`}>{diagnosis.message}</div>
      {props.error && <div className="backtest-resource-stale">刷新失败，当前保留上一次状态：{props.error}</div>}
      <RunnerDetails status={status} diagnosis={diagnosis} updatedAt={props.updatedAt} />
    </section>
  );
}
