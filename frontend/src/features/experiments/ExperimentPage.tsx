import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  Archive,
  Ban,
  ChevronRight,
  Copy,
  FlaskConical,
  LockKeyhole,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react';
import { ApiResponseError, fetchStrategyConfigPage } from '../../api/client';
import type { StrategyConfig, StrategyConfigSummary } from '../../types';
import { createBrowserUuid } from '../../utils/browserUuid';
import {
  archiveExperiment,
  cancelExperiment,
  createExperiment,
  fetchAdminArchivedExperiments,
  fetchExperimentDetail,
  fetchExperimentRevisions,
  fetchExperiments,
  fetchExperimentSubject,
  fetchExperimentTrials,
  findCreatedExperiment,
  pauseExperiment,
  previewExperiment,
  previewRetryFailedTrials,
  retryFailedTrials,
  requestExperimentPurge,
  resumeExperiment,
  restoreArchivedExperiment,
} from './api';
import type {
  AdminArchivedExperimentItem,
  DateScenarioDraft,
  ExperimentCreateResponse,
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentDraft,
  ExperimentListItem,
  ExperimentPreview,
  ExperimentRevision,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
  TunableInput,
} from './contracts';
import {
  buildPreviewRequest,
  clearExperimentDraft,
  createCopiedExperimentDraft,
  createExperimentDraft,
  loadExperimentDraft,
  saveExperimentDraft,
} from './draftStore';
import EvaluationBuilder from './EvaluationBuilder';
import ExperimentBacktestRunDrawer from './ExperimentBacktestRunDrawer';
import ExperimentCatalogDrawer from './ExperimentCatalogDrawer';
import { frozenScenarioNames } from './comparisonData';
import {
  dateRoleLabel,
  executionAlgorithmLabel,
  fillModelLabel,
  resourceClassLabel,
  statusLabel,
  systemDimensionValueLabel,
} from './displayLabels';

type Props = {
  userId: string;
  isAdmin?: boolean;
  enabled?: boolean | null;
  subject: ExperimentSubjectRef | null;
  experimentId?: string | null;
  onExperimentChange?: (experimentId: string | null) => void;
  onSubjectChange: (subject: ExperimentSubjectRef | null) => void;
  onBackToStrategy: () => void;
  onOpenRun?: (runId: string, symbol: string, timeframe: string, date: string) => void;
  onOpenBacktestDetail?: (runId: string) => void;
};

type DraftChange = (draft: ExperimentDraft) => ExperimentDraft;

type ExperimentCopyTarget = {
  experimentId: string;
  revisionId: string;
  revisionNo: number;
  experimentName: string;
  subjectRef: ExperimentSubjectRef;
  initialDraft: ExperimentDraft;
};

type CancelConfirmation = {
  revisionId: string;
  lifecycleVersion: number;
  idempotencyKey: string;
  reason: string;
};

const ACTIVE_EXPERIMENT_STATUSES = new Set(['preparing', 'queued', 'running', 'cancelling']);
const RECOVERABLE_BLOCK_CODES = new Set([
  'experiment.execution_blocked',
]);
const TERMINAL_EXPERIMENT_STATUSES = new Set([
  'completed', 'partially_completed', 'failed', 'cancelled',
]);

function shortDigest(value: unknown): string {
  return typeof value === 'string' && value ? `${value.slice(0, 12)}…` : '未提供';
}

function trialParameterValue(value: unknown): string {
  if (value == null) return '未记录';
  if (typeof value === 'boolean') return value ? '启用' : '停用';
  if (typeof value === 'string') return value || '空文本';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function trialParameterEntries(assignment: Record<string, unknown>): Array<{
  label: string;
  value: string;
}> {
  const strategyParameters = assignment.strategyParameters as Record<string, unknown> | undefined;
  const costScenario = assignment.costScenario as Record<string, unknown> | undefined;
  const executionScenario = assignment.executionScenario as Record<string, unknown> | undefined;
  const entries = Object.entries(strategyParameters ?? {}).map(([label, value]) => ({
    label,
    value: trialParameterValue(value),
  }));
  if ('initialCapital' in assignment) {
    entries.push({ label: '初始资金', value: trialParameterValue(assignment.initialCapital) });
  }
  if (costScenario) {
    entries.push({
      label: '成本场景',
      value: trialParameterValue(
        costScenario.name
        ?? systemDimensionValueLabel('costScenarioId', costScenario.scenarioId)
        ?? costScenario.scenarioId,
      ),
    });
    entries.push({ label: '滑点', value: trialParameterValue(costScenario.slippage) });
  }
  if (executionScenario) {
    const algorithm = executionScenario.algorithm == null
      ? null
      : executionAlgorithmLabel(executionScenario.algorithm);
    entries.push({
      label: '执行场景',
      value: trialParameterValue(
        executionScenario.name
        ?? systemDimensionValueLabel('executionScenarioId', executionScenario.scenarioId)
        ?? algorithm
        ?? executionScenario.scenarioId,
      ),
    });
  }
  return entries;
}

function trialExecutionReasonLabel(reason: string): string {
  if (reason === 'manual_retry') return '人工重试';
  if (reason === 'automatic_retry') return '系统重试';
  return '首次执行';
}

function TrialDetailDrawer({
  trial,
  onClose,
}: {
  trial: ExperimentTrial;
  onClose: () => void;
}) {
  const dateScenario = trial.dimensionAssignment.dateScenario as Record<string, unknown>
    | undefined;
  const parameters = trialParameterEntries(trial.dimensionAssignment);
  const attempts = trial.executionAttempts ?? [];
  return (
    <div className="experiment-trial-drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        aria-label={`Trial #${trial.trialIndex + 1} 详情`}
        className="experiment-trial-drawer"
        role="dialog"
        aria-modal="true"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="experiment-trial-drawer-header">
          <div>
            <span>Trial 详情</span>
            <h2>Trial #{trial.trialIndex + 1}</h2>
            <small>{statusLabel(trial.summaryStatus)}</small>
          </div>
          <button className="app-button is-quiet" type="button" onClick={onClose}>
            <X size={15} /> 关闭
          </button>
        </header>
        <div className="experiment-trial-drawer-body">
          <section>
            <h3>日期场景</h3>
            <div className="experiment-trial-drawer-scenario">
              <strong>{String(dateScenario?.name ?? dateScenario?.scenarioId ?? '未记录')}</strong>
              <span>
                {dateRoleLabel(dateScenario?.role)} · {String(dateScenario?.startDate ?? '—')}
                {' 至 '}{String(dateScenario?.endDate ?? '—')}
              </span>
            </div>
          </section>
          <section>
            <h3>冻结参数</h3>
            {parameters.length > 0 ? (
              <dl className="experiment-trial-drawer-parameters">
                {parameters.map((entry) => (
                  <div key={entry.label}>
                    <dt>{entry.label}</dt>
                    <dd>{entry.value}</dd>
                  </div>
                ))}
              </dl>
            ) : <div className="experiment-empty">未记录参数。</div>}
          </section>
          <section>
            <h3>执行记录（{attempts.length}）</h3>
            {attempts.length > 0 ? (
              <div className="experiment-trial-drawer-attempts">
                {attempts.map((execution) => (
                  <article key={execution.executionId}>
                    <div>
                      <strong>执行 #{execution.executionNo}</strong>
                      <span>{statusLabel(execution.status)}</span>
                    </div>
                    <small>{trialExecutionReasonLabel(execution.createdReason)}</small>
                    {execution.errorSummary && (
                      <p>
                        {String(execution.errorSummary.message
                          ?? execution.errorSummary.code
                          ?? '执行失败')}
                      </p>
                    )}
                  </article>
                ))}
              </div>
            ) : <div className="experiment-empty">尚无执行记录。</div>}
          </section>
        </div>
      </aside>
    </div>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return `${error.message}${error.requestId ? `（请求 ${error.requestId}）` : ''}`;
  }
  return error instanceof Error ? error.message : '请求未完成';
}

async function retryUncertain<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    return operation();
  }
}

export function isStandardBarConfig(
  config: StrategyConfig | StrategyConfigSummary,
): boolean {
  const params = 'paramsJson' in config ? config.paramsJson.params : null;
  const fillModel = 'fillModel' in config
    ? config.fillModel
    : params && typeof params === 'object'
    ? (params as Record<string, unknown>).fillModel
    : null;
  return config.enabled
    && config.published
    && Boolean(config.publishedVersionId)
    && config.mode === 'backtest'
    && fillModel === 'bar_next_open';
}

function formatExperimentUpdatedAt(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return '—';
  return new Date(timestamp).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function ExperimentLanding({
  isAdmin,
  onSelect,
  onOpenExperiment,
  onBack,
}: {
  isAdmin: boolean;
  onSelect: (subject: ExperimentSubjectRef) => void;
  onOpenExperiment: (experimentId: string) => void;
  onBack: () => void;
}) {
  const [configs, setConfigs] = useState<StrategyConfigSummary[]>([]);
  const [configQuery, setConfigQuery] = useState('');
  const [configTotal, setConfigTotal] = useState(0);
  const [configNextCursor, setConfigNextCursor] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [configLoadingMore, setConfigLoadingMore] = useState(false);
  const configRequestId = useRef(0);
  const [experiments, setExperiments] = useState<ExperimentListItem[]>([]);
  const [adminArchived, setAdminArchived] = useState<AdminArchivedExperimentItem[]>([]);
  const [nextAdminCursor, setNextAdminCursor] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [allExperimentsOpen, setAllExperimentsOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [purgingExperimentId, setPurgingExperimentId] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    const adminRequest = showArchived && isAdmin
      ? fetchAdminArchivedExperiments()
      : Promise.resolve({ items: [], nextCursor: null });
    Promise.all([
      fetchExperiments(undefined, showArchived, { limit: 5 }),
      adminRequest,
    ])
      .then(([page, adminPage]) => {
        if (!active) return;
        setExperiments(page.items);
        setAdminArchived(adminPage.items);
        setNextAdminCursor(adminPage.nextCursor);
      })
      .catch((reason: unknown) => {
        if (active) setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [isAdmin, showArchived]);

  useEffect(() => {
    const requestId = ++configRequestId.current;
    setConfigLoading(true);
    const timer = window.setTimeout(() => {
      fetchStrategyConfigPage({ limit: 50, query: configQuery })
        .then((page) => {
          if (requestId !== configRequestId.current) return;
          setConfigs(page.items.filter(isStandardBarConfig));
          setConfigNextCursor(page.nextCursor);
          setConfigTotal(page.total);
        })
        .catch((reason: unknown) => {
          if (requestId === configRequestId.current) setError(errorMessage(reason));
        })
        .finally(() => {
          if (requestId === configRequestId.current) setConfigLoading(false);
        });
    }, configQuery.trim() ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [configQuery]);

  const loadMoreConfigs = async () => {
    if (!configNextCursor || configLoadingMore) return;
    const requestId = configRequestId.current;
    setConfigLoadingMore(true);
    try {
      const page = await fetchStrategyConfigPage({
        limit: 50, cursor: configNextCursor, query: configQuery,
      });
      if (requestId !== configRequestId.current) return;
      setConfigs((current) => {
        const merged = new Map(current.map((item) => [item.id, item]));
        page.items.filter(isStandardBarConfig).forEach((item) => merged.set(item.id, item));
        return [...merged.values()];
      });
      setConfigNextCursor(page.nextCursor);
      setConfigTotal(page.total);
    } catch (reason) {
      if (requestId === configRequestId.current) setError(errorMessage(reason));
    } finally {
      if (requestId === configRequestId.current) setConfigLoadingMore(false);
    }
  };

  const loadMoreAdminArchived = async () => {
    if (!nextAdminCursor || loadingMore) return;
    setLoadingMore(true);
    setError('');
    try {
      const page = await fetchAdminArchivedExperiments(nextAdminCursor);
      setAdminArchived((current) => [...current, ...page.items]);
      setNextAdminCursor(page.nextCursor);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoadingMore(false);
    }
  };

  const handleAdminPurge = async (experiment: AdminArchivedExperimentItem) => {
    if (purgingExperimentId) return;
    const reason = window.prompt('请输入物理清理原因（将写入管理员审计）：')?.trim();
    if (!reason) return;
    if (!window.confirm('高风险操作：确认提交不可逆的实验物理清理任务？共享回测会保留。')) return;
    setPurgingExperimentId(experiment.experimentId);
    setError('');
    try {
      const idempotencyKey = createBrowserUuid();
      const purge = await retryUncertain(() => requestExperimentPurge(
        experiment.experimentId,
        experiment.currentRevisionId,
        experiment.snapshotVersion,
        reason,
        idempotencyKey,
      ));
      setAdminArchived((current) => current.map((item) => (
        item.experimentId === experiment.experimentId
          ? { ...item, purgeJobId: purge.purgeId, snapshotVersion: purge.snapshotVersion }
          : item
      )));
    } catch (reasonValue) {
      setError(errorMessage(reasonValue));
    } finally {
      setPurgingExperimentId('');
    }
  };

  return (
    <main className="experiment-page experiment-landing">
      <section className="experiment-hero">
        <div>
          <span className="experiment-eyebrow">自动化回测 / 有限网格</span>
          <h1>选择一个发布版本</h1>
          <p>自动化预览仅使用当前用户可运行的标准 Bar 单标的策略，结构字段在整个实验中保持冻结。</p>
        </div>
        <button className="app-button is-quiet" type="button" onClick={onBack}>
          <ArrowLeft size={15} /> 返回策略回测
        </button>
      </section>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      <section className="experiment-command-bar">
        <div>
          <strong>{showArchived ? '已归档实验' : '当前实验'}</strong>
          <span>归档只从默认列表隐藏实验，不会删除 Trial、回测结果或评价。</span>
        </div>
        <button
          className="app-button is-quiet"
          type="button"
          onClick={() => {
            setAllExperimentsOpen(false);
            setShowArchived((current) => !current);
          }}
        >
          {showArchived ? <RotateCcw size={15} /> : <Archive size={15} />}
          {showArchived ? '返回当前实验' : '查看已归档实验'}
        </button>
      </section>
      {!loading && experiments.length > 0 && (
        <section className="experiment-preview-card">
          <div className="experiment-recent-heading">
            <div>
              <h2>{showArchived ? '最近归档实验' : '最近实验'}</h2>
              <span>按最近更新时间显示 5 条</span>
            </div>
            <button
              className="app-button is-quiet"
              type="button"
              onClick={() => setAllExperimentsOpen(true)}
            >
              {showArchived ? '查看全部归档实验' : '查看全部实验'}
              <ChevronRight size={15} />
            </button>
          </div>
          <div className="experiment-trial-table">
            {experiments.map((experiment) => (
              <button
                className="experiment-trial-row is-experiment-summary"
                key={experiment.experimentId}
                type="button"
                onClick={() => onOpenExperiment(experiment.experimentId)}
              >
                <strong>{experiment.name}</strong>
                <span>{experiment.symbol} · {experiment.timeframe}</span>
                <span>{statusLabel(experiment.status)}</span>
                <span>
                  成功 {experiment.succeededTrialCount} / {experiment.plannedTrialCount}
                </span>
                <time dateTime={experiment.updatedAt}>
                  更新 {formatExperimentUpdatedAt(experiment.updatedAt)}
                </time>
              </button>
            ))}
          </div>
        </section>
      )}
      {showArchived && isAdmin && (
        <section className="experiment-preview-card">
          <h2>管理员待清理实验</h2>
          {adminArchived.length === 0 && !loading && (
            <div className="experiment-empty">暂无可执行物理清理的归档实验。</div>
          )}
          <div className="experiment-trial-table">
            {adminArchived.map((experiment) => {
              const retentionActive = Boolean(
                experiment.retentionUntil
                && Date.parse(experiment.retentionUntil) > Date.now(),
              );
              return (
                <div className="experiment-trial-row" key={experiment.experimentId}>
                  <strong>{experiment.name}</strong>
                  <span>{statusLabel(experiment.status)}</span>
                  <span>{retentionActive ? '保留期内' : '已到清理期限'}</span>
                  <button
                    className="app-button is-danger"
                    type="button"
                    disabled={Boolean(experiment.purgeJobId) || retentionActive || Boolean(purgingExperimentId)}
                    onClick={() => { void handleAdminPurge(experiment); }}
                  >
                    <Trash2 size={14} />
                    {experiment.purgeJobId ? '清理已提交' : `物理清理 ${experiment.name}`}
                  </button>
                </div>
              );
            })}
          </div>
          {nextAdminCursor && (
            <button
              className="app-button is-quiet"
              type="button"
              disabled={loadingMore}
              onClick={() => { void loadMoreAdminArchived(); }}
            >
              {loadingMore ? '正在加载…' : '加载更多待清理实验'}
            </button>
          )}
        </section>
      )}
      <section className="experiment-command-bar">
        <label className="experiment-trial-search">
          <Search size={15} />
          <input
            aria-label="搜索策略名称"
            value={configQuery}
            placeholder="搜索策略名称"
            onChange={(event) => setConfigQuery(event.target.value)}
          />
        </label>
        <span>共 {configTotal.toLocaleString()} 个策略，当前仅显示可用于标准 Bar 实验的摘要</span>
      </section>
      <section className="experiment-subject-grid" aria-busy={configLoading}>
        {configLoading && <div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> 正在读取策略版本…</div>}
        {!configLoading && configs.length === 0 && (
          <div className="experiment-empty">暂无可用的已发布标准 Bar 策略。请先在策略回测中完成保存与发布。</div>
        )}
        {configs.map((config) => (
          <button
            className="experiment-subject-card"
            key={config.id}
            type="button"
            onClick={() => onSelect({
              subjectType: 'legacy_single_strategy',
              subjectId: config.id,
              subjectVersion: config.publishedVersionId!,
            })}
          >
            <span className="experiment-subject-icon"><FlaskConical size={18} /></span>
            <strong>{config.name}</strong>
            <span>{config.symbol} · {config.timeframe} · 完整版本 V{config.versionNo}</span>
            <small>发布版本 {shortDigest(config.publishedVersionId)}</small>
          </button>
        ))}
      </section>
      {configNextCursor && (
        <button
          className="app-button is-quiet"
          type="button"
          disabled={configLoadingMore}
          onClick={() => { void loadMoreConfigs(); }}
        >
          {configLoadingMore ? '正在加载…' : '加载更多策略'}
        </button>
      )}
      <ExperimentCatalogDrawer
        archived={showArchived}
        open={allExperimentsOpen}
        onClose={() => setAllExperimentsOpen(false)}
        onOpenExperiment={(nextExperimentId) => {
          setAllExperimentsOpen(false);
          onOpenExperiment(nextExperimentId);
        }}
      />
    </main>
  );
}

function FixedStructure({ subject }: { subject: ExperimentSubject }) {
  const fixed = subject.fixedStructure;
  const feeProfile = fixed.feeProfile as Record<string, unknown> | undefined;
  const feeRates = feeProfile?.rates as Record<string, unknown> | undefined;
  const rows = [
    ['标的', fixed.symbol],
    ['周期', fixed.timeframe],
    ['成交模型', fillModelLabel(fixed.fillModel)],
    ['策略代码', shortDigest(fixed.scriptCodeDigest)],
    ['运行能力', shortDigest(fixed.runtimeCapabilityDigest)],
    ['费用档案', `${String(feeProfile?.profileId ?? '—')} · ${String(feeProfile?.version ?? '—')}`],
    ['佣金 / 最低佣金', `${String(feeRates?.commissionRate ?? '—')} / ${String(feeRates?.minimumCommission ?? '—')}`],
    ['过户费 / 印花税', `${String(feeRates?.transferFeeRate ?? '—')} / ${String(feeRates?.stampDutyRate ?? '—')}`],
  ];
  return (
    <section className="experiment-card experiment-fixed-card">
      <div className="experiment-card-heading">
        <div><span>01</span><h2>冻结结构</h2></div>
        <LockKeyhole size={17} />
      </div>
      <p className="experiment-card-copy">来自发布版本，不允许在网格中改写。</p>
      <dl className="experiment-fixed-grid">
        {rows.map(([label, value]) => (
          <div key={String(label)}><dt>{String(label)}</dt><dd>{String(value ?? '—')}</dd></div>
        ))}
      </dl>
      <div className="experiment-digest">模板摘要 <code>{subject.runTemplateDigest}</code></div>
    </section>
  );
}

function ParameterGrid({
  inputs,
  values,
  onChange,
}: {
  inputs: TunableInput[];
  values: Record<string, string>;
  onChange: (inputId: string, value: string) => void;
}) {
  return (
    <section className="experiment-card">
      <div className="experiment-card-heading"><div><span>02</span><h2>策略参数候选</h2></div></div>
      <p className="experiment-card-copy">仅列出策略代码声明的可调参数。使用英文逗号分隔候选值；留空表示沿用发布值。</p>
      <div className="experiment-fields">
        {inputs.length === 0 && <div className="experiment-inline-note">该策略没有声明可调参数。</div>}
        {inputs.map((input) => (
          <label className="experiment-field" key={input.inputId}>
            <span>{input.sourceName} <small>{input.type} · 当前 {String(input.defaultValue)}</small></span>
            <input
              value={values[input.inputId] ?? ''}
              placeholder={`例如 ${String(input.defaultValue)}`}
              onChange={(event) => onChange(input.inputId, event.target.value)}
            />
          </label>
        ))}
      </div>
    </section>
  );
}

function DateScenarios({
  scenarios,
  onChange,
}: {
  scenarios: DateScenarioDraft[];
  onChange: (scenarios: DateScenarioDraft[]) => void;
}) {
  const update = (index: number, patch: Partial<DateScenarioDraft>) => {
    onChange(scenarios.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  };
  const add = () => {
    const usedIds = new Set(scenarios.map((item) => item.scenarioId));
    let suffix = scenarios.length + 1;
    while (usedIds.has(`scenario-${suffix}`)) suffix += 1;
    onChange([...scenarios, {
      scenarioId: `scenario-${suffix}`,
      name: `场景 ${suffix}`,
      role: 'validation',
      startDate: scenarios[scenarios.length - 1]?.startDate ?? '',
      endDate: scenarios[scenarios.length - 1]?.endDate ?? '',
    }]);
  };
  return (
    <section className="experiment-card experiment-wide-card">
      <div className="experiment-card-heading">
        <div><span>03</span><h2>日期场景</h2></div>
        <button className="app-button is-quiet is-compact" type="button" onClick={add}><Plus size={14} /> 添加场景</button>
      </div>
      <p className="experiment-card-copy">日期区间是原子维度；服务端以实际 Bar 覆盖冻结数据快照。</p>
      <div className="experiment-scenarios">
        {scenarios.map((scenario, index) => (
          <div className="experiment-scenario-row" key={`${scenario.scenarioId}-${index}`}>
            <input aria-label="场景名称" value={scenario.name} onChange={(event) => update(index, { name: event.target.value })} />
            <select aria-label="场景角色" value={scenario.role} onChange={(event) => update(index, { role: event.target.value as DateScenarioDraft['role'] })}>
              <option value="development">开发</option>
              <option value="validation">验证</option>
              <option value="reference">参考</option>
            </select>
            <input aria-label="开始日期" type="date" value={scenario.startDate} onChange={(event) => update(index, { startDate: event.target.value })} />
            <span>至</span>
            <input aria-label="结束日期" type="date" value={scenario.endDate} onChange={(event) => update(index, { endDate: event.target.value })} />
            <button
              className="experiment-icon-button"
              aria-label="删除日期场景"
              type="button"
              disabled={scenarios.length === 1}
              onClick={() => onChange(scenarios.filter((_, itemIndex) => itemIndex !== index))}
            ><Trash2 size={15} /></button>
          </div>
        ))}
      </div>
    </section>
  );
}

function RuntimeDimensions({
  draft,
  onChange,
}: {
  draft: ExperimentDraft;
  onChange: (patch: Partial<ExperimentDraft>) => void;
}) {
  return (
    <section className="experiment-card experiment-wide-card">
      <div className="experiment-card-heading"><div><span>04</span><h2>运行场景</h2></div></div>
      <div className="experiment-runtime-grid">
        <label className="experiment-field">
          <span>初始资金候选 <small>定点十进制</small></span>
          <input value={draft.capitalValues} onChange={(event) => onChange({ capitalValues: event.target.value })} />
        </label>
        <label className="experiment-field">
          <span>滑点候选 <small>费用档案保持冻结</small></span>
          <input value={draft.slippageValues} onChange={(event) => onChange({ slippageValues: event.target.value })} />
        </label>
        <div className="experiment-readonly-field">
          <span>执行算法</span><strong>{executionAlgorithmLabel('immediate')}</strong><small>标准 Bar 首期固定</small>
        </div>
      </div>
    </section>
  );
}

function PreviewResult({ preview }: { preview: ExperimentPreview }) {
  const estimatedBars = preview.resourceEstimate.estimatedBarCountExact
    ?? preview.resourceEstimate.estimatedBarCount?.toLocaleString()
    ?? '暂时无法估算耗时';
  return (
    <section className={`experiment-preview ${preview.limitDecision.allowed ? 'is-allowed' : 'is-blocked'}`}>
      <div className="experiment-preview-metrics">
        <div><span>Trial 总数</span><strong>{preview.plannedTrialCount.toLocaleString()}</strong></div>
        <div><span>预计 Bar</span><strong>{estimatedBars}</strong></div>
        <div><span>资源等级（提示）</span><strong>{preview.resourceEstimate.estimateStatus === 'unavailable' ? '暂不可估算' : resourceClassLabel(preview.resourceEstimate.resourceClass)}</strong></div>
        <div><span>维度数量</span><strong>{Object.keys(preview.dimensionCounts).length}</strong></div>
      </div>
      <div className="experiment-preview-decision">
        <ShieldCheck size={18} />
        <div>
          <strong>{preview.limitDecision.allowed ? '预览完成，可创建实验' : '预览存在输入阻塞项'}</strong>
          <span>有效至 {new Date(preview.expiresAt).toLocaleString()} · 规范 {preview.canonicalizationVersion}</span>
        </div>
      </div>
      <div className="experiment-dimensions">
        {Object.entries(preview.dimensionCounts).map(([path, count]) => (
          <span key={path}><code>{path}</code><strong>× {count}</strong></span>
        ))}
      </div>
      {Object.entries(preview.dimensionNormalization).some(([, counts]) => counts.submitted !== counts.normalized) && (
        <div className="experiment-alert">
          重复候选已规范化去重：{Object.entries(preview.dimensionNormalization)
            .filter(([, counts]) => counts.submitted !== counts.normalized)
            .map(([path, counts]) => `${path} ${counts.submitted}→${counts.normalized}`)
            .join('；')}
        </div>
      )}
      {preview.limitDecision.blockingReasons.map((reason) => (
        <div className="experiment-alert is-error" key={reason.code}>{reason.message}</div>
      ))}
      {preview.limitDecision.warnings.map((warning) => (
        <div className="experiment-alert" key={warning.code}>{warning.message}</div>
      ))}
      {preview.limitDecision.allowed && (
        <div className="experiment-alert">
          Trial 总数和预计 Bar 仅描述逻辑规模与资源提示。大计划会滚动生成并受实际并发和存储压力控制，可能长时间排队，可随时暂停或取消。
        </div>
      )}
      <div className="experiment-samples">
        <h3>首尾 Trial 样例</h3>
        {preview.examples.map((example) => (
          <div key={example.trialFingerprint}>
            <span>#{example.trialIndex + 1}</span>
            <code>{JSON.stringify(example.dimensionAssignment)}</code>
            <small>{shortDigest(example.trialFingerprint)}</small>
          </div>
        ))}
      </div>
      <div className="experiment-digest">Spec Hash <code>{preview.specHash}</code></div>
      <div className="experiment-digest">Dataset Snapshot <code>{preview.datasetSnapshotDigest}</code></div>
    </section>
  );
}

function ExperimentDetailView({
  experimentId,
  isAdmin,
  onClose,
  onOpenRun,
  onOpenBacktestDetail,
  onCopyExperiment,
}: {
  experimentId: string;
  isAdmin: boolean;
  onClose: () => void;
  onOpenRun?: (runId: string, symbol: string, timeframe: string, date: string) => void;
  onOpenBacktestDetail?: (runId: string) => void;
  onCopyExperiment: (detail: ExperimentDetail) => void;
}) {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [trials, setTrials] = useState<ExperimentTrial[]>([]);
  const [revisions, setRevisions] = useState<ExperimentRevision[]>([]);
  const [viewRevisionId, setViewRevisionId] = useState<string | null>(null);
  const [selectedFailedIds, setSelectedFailedIds] = useState<Set<string>>(new Set());
  const [nextTrialCursor, setNextTrialCursor] = useState<string | null>(null);
  const [trialSearchInput, setTrialSearchInput] = useState('');
  const [trialQuery, setTrialQuery] = useState('');
  const [trialStatus, setTrialStatus] = useState<ExperimentTrial['summaryStatus'] | ''>('');
  const [trialDateScenarioId, setTrialDateScenarioId] = useState('');
  const [selectedTrialId, setSelectedTrialId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [lifecycleAction, setLifecycleAction] = useState<'' | 'archive' | 'restore' | 'purge'>('');
  const [lifecycleMessage, setLifecycleMessage] = useState('');
  const [relatedRunsOpen, setRelatedRunsOpen] = useState(false);
  const [cancelConfirmation, setCancelConfirmation] = useState<CancelConfirmation | null>(null);
  const cancelInFlight = useRef(false);
  const pauseInFlight = useRef(false);
  const resumeInFlight = useRef(false);
  const retryInFlight = useRef(false);
  const lifecycleInFlight = useRef(false);
  const unchangedPolls = useRef(0);
  const lastVersion = useRef<number | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      try {
        const result = await fetchExperimentDetail(experimentId);
        if (stopped) return;
        unchangedPolls.current = lastVersion.current === result.snapshotVersion
          ? unchangedPolls.current + 1
          : 0;
        lastVersion.current = result.snapshotVersion;
        setDetail(result);
        const [trialPage, revisionPage] = await Promise.all([
          fetchExperimentTrials(experimentId, undefined, {
            revisionId: viewRevisionId ?? undefined,
            status: trialStatus,
            dateScenarioId: trialDateScenarioId || undefined,
            query: trialQuery,
            limit: 20,
          }),
          fetchExperimentRevisions(experimentId),
        ]);
        if (stopped) return;
        setTrials(trialPage.items);
        setSelectedTrialId((current) => (
          current && trialPage.items.some((trial) => trial.trialId === current)
            ? current
            : null
        ));
        setRevisions(revisionPage.items);
        setSelectedFailedIds((current) => {
          const eligible = new Set(trialPage.items.filter((trial) => (
            trial.summaryStatus === 'failed' && !trial.activeExecution
          )).map((trial) => trial.trialId));
          return new Set([...current].filter((trialId) => eligible.has(trialId)));
        });
        setNextTrialCursor(trialPage.nextCursor);
        setError('');
        if (!ACTIVE_EXPERIMENT_STATUSES.has(result.status)) return;
        const delay = document.hidden
          ? 15000
          : unchangedPolls.current >= 6 ? 15000 : unchangedPolls.current >= 3 ? 5000 : 2000;
        timer = setTimeout(() => { void poll(); }, delay);
      } catch (reason) {
        if (!stopped) setError(errorMessage(reason));
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [experimentId, trialDateScenarioId, trialQuery, trialStatus, viewRevisionId]);

  useEffect(() => {
    if (!selectedTrialId) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedTrialId(null);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [selectedTrialId]);

  const handleCancel = async () => {
    if (!detail || !cancelConfirmation || cancelInFlight.current) return;
    if (
      detail.revision.revisionId !== cancelConfirmation.revisionId
      || detail.lifecycleVersion !== cancelConfirmation.lifecycleVersion
    ) {
      setCancelConfirmation(null);
      setError('实验生命周期已经变化，请刷新详情后重新确认取消。');
      return;
    }
    cancelInFlight.current = true;
    setCancelling(true);
    setError('');
    try {
      await cancelExperiment(
        experimentId,
        cancelConfirmation.revisionId,
        cancelConfirmation.lifecycleVersion,
        cancelConfirmation.reason,
        cancelConfirmation.idempotencyKey,
      );
      setCancelConfirmation(null);
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (reason) {
      if (reason instanceof TypeError) {
        setError('网络结果不明确；再次点击“重试取消”会复用同一幂等键。');
      } else {
        if (
          reason instanceof ApiResponseError
          && ['experiment.revision_conflict', 'experiment.lifecycle_conflict'].includes(reason.code)
        ) {
          setCancelConfirmation(null);
          setDetail(await fetchExperimentDetail(experimentId));
        }
        setError(errorMessage(reason));
      }
    } finally {
      cancelInFlight.current = false;
      setCancelling(false);
    }
  };

  const handlePause = async () => {
    if (!detail || pauseInFlight.current) return;
    pauseInFlight.current = true;
    setPausing(true);
    setError('');
    try {
      await pauseExperiment(
        experimentId,
        detail.revision.revisionId,
        detail.lifecycleVersion ?? 1,
        '用户在实验详情页暂停实验',
        createBrowserUuid(),
      );
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      pauseInFlight.current = false;
      setPausing(false);
    }
  };

  const handleResume = async (resumeMode: 'user_pause' | 'execution_circuit') => {
    if (!detail || resumeInFlight.current) return;
    resumeInFlight.current = true;
    setResuming(true);
    setError('');
    try {
      await resumeExperiment(
        experimentId,
        detail.revision.revisionId,
        detail.lifecycleVersion ?? 1,
        resumeMode,
        resumeMode === 'user_pause'
          ? '用户在实验详情页继续已暂停实验'
          : '用户在实验详情页解除执行熔断',
        createBrowserUuid(),
      );
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      resumeInFlight.current = false;
      setResuming(false);
    }
  };

  const handleRetryFailed = async () => {
    if (!detail || retryInFlight.current || selectedFailedIds.size === 0) return;
    retryInFlight.current = true;
    setRetrying(true);
    setError('');
    const trialIds = [...selectedFailedIds].sort();
    const idempotencyKey = createBrowserUuid();
    try {
      const summary = await previewRetryFailedTrials(
        experimentId, detail.revision.revisionId, trialIds,
      );
      const confirmed = window.confirm(
        `确认重试 ${summary.selectedTrialCount} 个失败 Trial？`
          + '系统会保留旧执行，并为每项创建新的执行序号、回测任务和审计记录。',
      );
      if (!confirmed) return;
      try {
        await retryFailedTrials(
          experimentId, detail.revision.revisionId, trialIds, summary, idempotencyKey,
        );
      } catch (reason) {
        if (!(reason instanceof TypeError)) throw reason;
        await retryFailedTrials(
          experimentId, detail.revision.revisionId, trialIds, summary, idempotencyKey,
        );
      }
      setSelectedFailedIds(new Set());
      const [nextDetail, trialPage] = await Promise.all([
        fetchExperimentDetail(experimentId),
        fetchExperimentTrials(experimentId, undefined, {
          revisionId: viewRevisionId ?? undefined,
          status: trialStatus,
          dateScenarioId: trialDateScenarioId || undefined,
          query: trialQuery,
          limit: 20,
        }),
      ]);
      setDetail(nextDetail);
      setTrials(trialPage.items);
      setNextTrialCursor(trialPage.nextCursor);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      retryInFlight.current = false;
      setRetrying(false);
    }
  };

  const handleArchive = async () => {
    if (!detail || lifecycleInFlight.current) return;
    if (!window.confirm('确认归档该实验？归档只会从默认列表隐藏，不会删除任何实验数据。')) return;
    lifecycleInFlight.current = true;
    setLifecycleAction('archive');
    setError('');
    const idempotencyKey = createBrowserUuid();
    try {
      await retryUncertain(() => archiveExperiment(
        experimentId,
        detail.revision.revisionId,
        detail.snapshotVersion,
        '用户在实验详情页确认归档终态实验',
        idempotencyKey,
      ));
      onClose();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      lifecycleInFlight.current = false;
      setLifecycleAction('');
    }
  };

  const handleRestoreArchive = async () => {
    if (!detail || lifecycleInFlight.current) return;
    if (!window.confirm('确认恢复该实验到默认列表？实验数据和 Revision 身份保持不变。')) return;
    lifecycleInFlight.current = true;
    setLifecycleAction('restore');
    setError('');
    try {
      const idempotencyKey = createBrowserUuid();
      await retryUncertain(() => restoreArchivedExperiment(
        experimentId, detail.revision.revisionId, detail.snapshotVersion,
        '用户在实验详情页确认恢复归档实验', idempotencyKey,
      ));
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      lifecycleInFlight.current = false;
      setLifecycleAction('');
    }
  };

  const handlePurge = async () => {
    if (!detail || lifecycleInFlight.current) return;
    const reason = window.prompt('请输入物理清理原因（将写入管理员审计）：')?.trim();
    if (!reason) return;
    if (!window.confirm('高风险操作：确认提交不可逆的实验物理清理任务？共享回测会保留。')) return;
    lifecycleInFlight.current = true;
    setLifecycleAction('purge');
    setError('');
    try {
      const idempotencyKey = createBrowserUuid();
      const purge = await retryUncertain(() => requestExperimentPurge(
        experimentId, detail.revision.revisionId, detail.snapshotVersion,
        reason, idempotencyKey,
      ));
      setLifecycleMessage(`物理清理任务已进入队列：${purge.purgeId}`);
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      lifecycleInFlight.current = false;
      setLifecycleAction('');
    }
  };

  const loadMoreTrials = async () => {
    if (!nextTrialCursor) return;
    try {
      const page = await fetchExperimentTrials(
        experimentId,
        nextTrialCursor,
        {
          revisionId: viewRevisionId ?? undefined,
          status: trialStatus,
          dateScenarioId: trialDateScenarioId || undefined,
          query: trialQuery,
          limit: 20,
        },
      );
      setTrials((current) => [...current, ...page.items]);
      setNextTrialCursor(page.nextCursor);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  if (!detail) {
    return <main className="experiment-page"><div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> {error || '正在读取实验进度…'}</div></main>;
  }
  const progress = detail.stage.plannedTrialCount > 0
    ? Math.round((detail.stage.generatedTrialCount / detail.stage.plannedTrialCount) * 100)
    : 0;
  const relatedBacktestRuns = detail.relatedBacktestRuns ?? {
    totalCount: 0,
    initialCount: 0,
    retryCount: 0,
  };
  const fixed = detail.frozenDefinition.fixedStructure;
  const dateScenarios = frozenScenarioNames(detail.frozenDefinition.plan);
  const viewingCurrentRevision = !viewRevisionId
    || viewRevisionId === detail.revision.revisionId;
  const selectedTrial = selectedTrialId
    ? trials.find((trial) => trial.trialId === selectedTrialId) ?? null
    : null;
  const hasTrialFilters = Boolean(trialQuery || trialStatus || trialDateScenarioId);
  const failedTrials = trials.filter((trial) => (
    trial.summaryStatus === 'failed' && !trial.activeExecution
  ));
  const retentionActive = Boolean(
    detail.retentionUntil && Date.parse(detail.retentionUntil) > Date.now(),
  );
  const focusFailedTrials = () => {
    setSelectedFailedIds(new Set(failedTrials.map((trial) => trial.trialId)));
    document.getElementById('experiment-trial-runs')?.scrollIntoView({
      behavior: 'smooth', block: 'start',
    });
  };
  return (
    <main className="experiment-page experiment-detail-page">
      <section className="experiment-hero experiment-detail-hero">
        <div>
          <span className="experiment-eyebrow">自动化实验 / {statusLabel(detail.status)}</span>
          <h1>{detail.name}</h1>
          <p>{detail.description || `Revision ${detail.revision.revisionNo}`}</p>
        </div>
        <div className="experiment-detail-actions">
          <button className="app-button is-quiet" type="button" onClick={onClose}>
            <ArrowLeft size={15} /> 返回实验入口
          </button>
          {(TERMINAL_EXPERIMENT_STATUSES.has(detail.status)
            || detail.lifecycleState === 'cancelling')
            && viewingCurrentRevision && !detail.archivedAt && (
            <button
              className="app-button is-primary"
              type="button"
              onClick={() => onCopyExperiment(detail)}
            >
              <Copy size={15} /> 复制实验并调整
            </button>
          )}
          {TERMINAL_EXPERIMENT_STATUSES.has(detail.status)
            && viewingCurrentRevision && !detail.archivedAt && (
            <button
              className="app-button is-quiet experiment-lifecycle-button"
              type="button"
              disabled={lifecycleAction !== ''}
              onClick={() => { void handleArchive(); }}
            >
              <Archive size={14} /> {lifecycleAction === 'archive' ? '正在归档…' : '归档实验'}
            </button>
          )}
          {detail.archivedAt && !detail.purgeJobId && retentionActive && (
            <button
              className="app-button is-quiet"
              type="button"
              disabled={lifecycleAction !== ''}
              onClick={() => { void handleRestoreArchive(); }}
            >
              <RotateCcw size={15} /> {lifecycleAction === 'restore' ? '正在恢复…' : '恢复归档实验'}
            </button>
          )}
        </div>
      </section>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      {lifecycleMessage && <div className="experiment-alert" role="status">{lifecycleMessage}</div>}
      {detail.source && (
        <div className="experiment-source-banner">
          <span>复制来源</span>
          <strong>{detail.source.experimentName}</strong>
          <small>来源实验的 Revision {detail.source.revisionNo}</small>
        </div>
      )}
      {detail.archivedAt && (
        <div className="experiment-alert is-warning" role="status">
          此实验已归档，当前内容保持只读。
          {detail.retentionUntil && ` 最早可清理时间：${new Date(detail.retentionUntil).toLocaleString()}。`}
          {detail.purgeJobId && ` 物理清理任务：${detail.purgeJobId}。`}
        </div>
      )}
      {detail.lifecycleState === 'pausing' && (
        <div className="experiment-alert is-warning" role="status">
          实验正在暂停：不再生成或物化新 Trial，已有排队和运行任务会自然结束。
        </div>
      )}
      {detail.lifecycleState === 'paused' && (
        <div className="experiment-alert is-warning" role="status">
          实验已由用户暂停，生成游标和已有结果均已保留。
        </div>
      )}
      {detail.systemBlockCode === 'storage_pressure' && (
        <div className="experiment-alert is-warning" role="status">
          系统存储压力门禁已关闭自动化实验的新生成和新物化；信号恢复后会自动继续，用户恢复命令不能绕过该门禁。
        </div>
      )}
      {detail.blockedReason && (
        <div
          className={`experiment-alert ${RECOVERABLE_BLOCK_CODES.has(detail.blockedReason.code) ? 'is-warning' : 'is-error'}`}
          role="alert"
        >
          {detail.blockedReason.message}（{detail.blockedReason.code}）
          {RECOVERABLE_BLOCK_CODES.has(detail.blockedReason.code) && '。已生成的 Trial 和运行中任务保持不变，条件恢复后会从当前 Revision 继续。'}
          {detail.blockedReason.scope && `，阻塞范围：${detail.blockedReason.scope}`}
          {detail.blockedReason.code === 'experiment.execution_blocked' && (
            <button
              className="app-button is-quiet"
              type="button"
              disabled={resuming}
              onClick={() => { void handleResume('execution_circuit'); }}
            >
              <Play size={15} /> {resuming ? '正在校验并恢复…' : '继续未完成 Trial'}
            </button>
          )}
        </div>
      )}
      {detail.errorAggregates.length > 0 && (
        <section className="experiment-preview-card">
          <h2>失败摘要</h2>
          <div className="experiment-trial-table">
            {detail.errorAggregates.map((failure) => (
              <div className="experiment-trial-row" key={failure.fingerprint}>
                <strong>{failure.code}</strong>
                <span>{failure.message || '回测执行失败'}</span>
                <span>{failure.failureStage} · {failure.component}</span>
                <span>{failure.count} 次</span>
              </div>
            ))}
          </div>
        </section>
      )}
      <section className="experiment-detail-kpis" aria-label="实验运行概览">
        <div>
          <span>当前阶段</span>
          <strong>{statusLabel(detail.stage.status)}</strong>
        </div>
        <div className="experiment-detail-progress-kpi">
          <span>Trial 生成</span>
          <strong>{detail.stage.generatedTrialCount} / {detail.stage.plannedTrialCount}</strong>
          <div className="experiment-progress" aria-label="Trial 生成进度">
            <span style={{ width: `${progress}%` }} />
          </div>
        </div>
        <div><span>成功</span><strong>{detail.stage.succeededTrialCount}</strong></div>
        <div><span>失败</span><strong>{detail.stage.failedTrialCount}</strong></div>
        <div><span>取消</span><strong>{detail.stage.cancelledTrialCount}</strong></div>
        <div><span>待物化</span><strong>{detail.stage.readyTrialCount}</strong></div>
        <div><span>回测排队</span><strong>{detail.stage.queuedTrialCount}</strong></div>
        <div><span>尚未生成</span><strong>{detail.stage.remainingLogicalTrialCount}</strong></div>
      </section>
      <div className="experiment-detail-workspace">
        <div className="experiment-detail-main">
          <section className="experiment-preview-card" id="experiment-trial-runs">
        <div className="experiment-card-heading-inline">
          <h2>Trial 运行</h2>
          {viewingCurrentRevision && !detail.archivedAt && failedTrials.length > 0 && (
            <div className="experiment-retry-actions">
              <button
                className="app-button is-quiet"
                type="button"
                onClick={() => setSelectedFailedIds(new Set(failedTrials.map((trial) => trial.trialId)))}
              >
                选择本页失败项
              </button>
              <button
                className="app-button is-primary"
                type="button"
                disabled={retrying || selectedFailedIds.size === 0}
                onClick={() => { void handleRetryFailed(); }}
              >
                <RefreshCw size={15} />
                {retrying ? '正在冻结选择并重试…' : `重试选中失败项（${selectedFailedIds.size}）`}
              </button>
            </div>
          )}
        </div>
        <div className="experiment-preview-heading">
          <div><span>待物化</span><strong>{detail.stage.readyTrialCount}</strong></div>
          <div><span>回测排队</span><strong>{detail.stage.queuedTrialCount}</strong></div>
          <div><span>尚未生成</span><strong>{detail.stage.remainingLogicalTrialCount}</strong></div>
          <div><span>在途</span><strong>{detail.stage.inFlightTrialCount}</strong></div>
          <div><span>成功</span><strong>{detail.stage.succeededTrialCount}</strong></div>
          <div><span>失败</span><strong>{detail.stage.failedTrialCount}</strong></div>
          <div><span>取消</span><strong>{detail.stage.cancelledTrialCount}</strong></div>
        </div>
        <form
          className="experiment-trial-filters"
          onSubmit={(event) => {
            event.preventDefault();
            setSelectedTrialId(null);
            setTrialQuery(trialSearchInput.trim());
          }}
        >
          <label className="experiment-trial-search">
            <Search size={15} />
            <input
              aria-label="搜索 Trial 编号"
              inputMode="numeric"
              maxLength={19}
              placeholder="Trial 编号，例如 12"
              value={trialSearchInput}
              onChange={(event) => setTrialSearchInput(event.target.value.replace(/\D/g, ''))}
            />
          </label>
          <button className="app-button is-quiet" type="submit">搜索</button>
          <select
            aria-label="筛选 Trial 状态"
            value={trialStatus}
            onChange={(event) => {
              setSelectedTrialId(null);
              setTrialStatus(event.target.value as ExperimentTrial['summaryStatus'] | '');
            }}
          >
            <option value="">全部状态</option>
            <option value="pending">等待生成</option>
            <option value="queued">排队中</option>
            <option value="running">运行中</option>
            <option value="cancelling">取消中</option>
            <option value="succeeded">成功</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
          <select
            aria-label="筛选日期场景"
            value={trialDateScenarioId}
            onChange={(event) => {
              setSelectedTrialId(null);
              setTrialDateScenarioId(event.target.value);
            }}
          >
            <option value="">全部日期场景</option>
            {dateScenarios.map((scenario) => (
              <option key={scenario.scenarioId} value={scenario.scenarioId}>{scenario.name}</option>
            ))}
          </select>
          {hasTrialFilters && (
            <button
              className="app-button is-quiet"
              type="button"
              onClick={() => {
                setTrialSearchInput('');
                setTrialQuery('');
                setTrialStatus('');
                setTrialDateScenarioId('');
                setSelectedTrialId(null);
              }}
            >
              重置
            </button>
          )}
        </form>
        <div className="experiment-trial-list-shell">
          <table className="experiment-trial-list">
            <thead>
              <tr>
                <th aria-label="选择" />
                <th>Trial</th>
                <th>状态</th>
                <th>日期场景</th>
                <th>参数</th>
                <th>执行</th>
                <th aria-label="操作" />
              </tr>
            </thead>
            <tbody>
              {trials.map((trial) => {
                const dateScenario = trial.dimensionAssignment.dateScenario as
                  Record<string, unknown> | undefined;
                const parameterCount = trialParameterEntries(trial.dimensionAssignment).length;
                const executionCount = (trial.executionAttempts ?? []).length;
                const retryable = viewingCurrentRevision && !detail.archivedAt
                  && trial.summaryStatus === 'failed' && !trial.activeExecution;
                return (
                  <tr
                    className={selectedTrialId === trial.trialId ? 'is-selected' : ''}
                    key={trial.trialId}
                    onClick={() => setSelectedTrialId(trial.trialId)}
                  >
                    <td>
                      {retryable ? (
                        <input
                          aria-label={`选择 Trial #${trial.trialIndex + 1}`}
                          type="checkbox"
                          checked={selectedFailedIds.has(trial.trialId)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) => setSelectedFailedIds((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(trial.trialId);
                            else next.delete(trial.trialId);
                            return next;
                          })}
                        />
                      ) : null}
                    </td>
                    <td><strong>#{trial.trialIndex + 1}</strong></td>
                    <td>
                      <span className="experiment-trial-status" data-status={trial.summaryStatus}>
                        {statusLabel(trial.summaryStatus)}
                      </span>
                    </td>
                    <td>
                      <span className="experiment-trial-scenario-cell">
                        <strong>{String(dateScenario?.name ?? dateScenario?.scenarioId ?? '—')}</strong>
                        <small>
                          {dateRoleLabel(dateScenario?.role)} · {String(dateScenario?.startDate ?? '—')}
                          {' 至 '}{String(dateScenario?.endDate ?? '—')}
                        </small>
                      </span>
                    </td>
                    <td>{parameterCount} 项</td>
                    <td>{executionCount} 次</td>
                    <td>
                      <button
                        aria-label={`查看 Trial #${trial.trialIndex + 1} 详情`}
                        className="experiment-trial-open"
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelectedTrialId(trial.trialId);
                        }}
                      >
                        <ChevronRight size={16} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {trials.length === 0 && (
            <div className="experiment-empty">
              {hasTrialFilters ? '没有符合筛选条件的 Trial。' : 'Trial 尚未生成。'}
            </div>
          )}
        </div>
        {nextTrialCursor && (
          <button
            className="app-button is-quiet experiment-trial-load-more"
            type="button"
            onClick={() => { void loadMoreTrials(); }}
          >
            加载更多（每次 20 条）
          </button>
        )}
      </section>
      {selectedTrial && (
        <TrialDetailDrawer trial={selectedTrial} onClose={() => setSelectedTrialId(null)} />
      )}
      {TERMINAL_EXPERIMENT_STATUSES.has(detail.status)
        && viewingCurrentRevision && !detail.archivedAt ? (
        <EvaluationBuilder
          experimentId={experimentId}
          scenarioNames={frozenScenarioNames(detail.frozenDefinition.plan)}
          symbol={String(fixed.symbol ?? '')}
          timeframe={String(fixed.timeframe ?? '5m')}
          trialSummary={{
            planned: detail.stage.plannedTrialCount,
            generated: detail.stage.generatedTrialCount,
            succeeded: detail.stage.succeededTrialCount,
            failed: detail.stage.failedTrialCount,
            cancelled: detail.stage.cancelledTrialCount,
          }}
          onRetryFailed={failedTrials.length > 0 ? focusFailedTrials : undefined}
          onOpenRun={onOpenRun}
        />
      ) : detail.archivedAt ? (
        <section className="experiment-preview-card evaluation-locked">
          <h2>结果评价</h2>
          <p>归档实验保持只读；恢复归档后可继续创建评价与导出。</p>
        </section>
      ) : (
        <section className="experiment-preview-card evaluation-locked">
          <h2>结果评价</h2>
          <p>{viewingCurrentRevision
            ? '评价将在当前阶段进入终态后开放；失败项可重试，已有成功结果可生成明确标记的临时评价。'
            : '历史修订评价保持只读，请返回当前 Revision 查看评价报告。'}</p>
        </section>
      )}
        </div>
        <aside className="experiment-detail-sidebar">
          <section className="experiment-structure-card">
            <div className="experiment-sidebar-heading">
              <h2>冻结定义</h2>
              <span>创建时快照</span>
            </div>
            <div className="experiment-structure-grid">
              <div><span>标的</span><strong>{String(fixed.symbol ?? '—')}</strong></div>
              <div><span>周期</span><strong>{String(fixed.timeframe ?? '—')}</strong></div>
              <div><span>成交模型</span><strong>{fillModelLabel(fixed.fillModel)}</strong></div>
              <div><span>数据场景</span><strong>{detail.frozenDefinition.datasetSnapshots.length}</strong></div>
            </div>
          </section>
          <section className="experiment-preview-card experiment-revision-card">
            <div className="experiment-sidebar-heading">
              <h2>修订历史</h2>
              <span>{revisions.length} 个版本</span>
            </div>
            <div className="experiment-revision-list">
              {revisions.map((revision) => (
                <button
                  className={`experiment-revision-item ${(
                    viewRevisionId === revision.revisionId
                    || (!viewRevisionId && revision.current)
                  ) ? 'is-selected' : ''}`}
                  key={revision.revisionId}
                  type="button"
                  onClick={() => setViewRevisionId(revision.current ? null : revision.revisionId)}
                >
                  <strong>Revision {revision.revisionNo}{revision.current ? ' · 当前' : ''}</strong>
                  <span>{statusLabel(revision.status)}</span>
                  <small>
                    成功 {revision.stage.succeededTrialCount} / {revision.stage.plannedTrialCount}
                    {' · '}失败 {revision.stage.failedTrialCount}
                  </small>
                </button>
              ))}
            </div>
            {!viewingCurrentRevision && (
              <div className="experiment-alert is-warning">
                当前正在查看历史修订；其 Trial、执行和评价保持只读，不参与当前 Revision 聚合。
              </div>
            )}
          </section>
          <button
            className="experiment-related-runs-card"
            type="button"
            disabled={relatedBacktestRuns.totalCount === 0}
            onClick={() => setRelatedRunsOpen(true)}
          >
            <span>
              <strong>关联回测任务</strong>
              <b>{relatedBacktestRuns.totalCount} 个</b>
              <ChevronRight size={16} />
            </span>
            <small>
              首次执行 {relatedBacktestRuns.initialCount}
              {' · '}重试 {relatedBacktestRuns.retryCount}
            </small>
          </button>
          <section className="experiment-preview-card experiment-technical-card">
            <details className="experiment-technical-details">
              <summary>技术信息与编号</summary>
              <div className="experiment-digest">Spec Hash <code>{detail.revision.specHash}</code></div>
              <div className="experiment-digest">Trial 集合 <code>{shortDigest(detail.stage.trialCollectionDigest)}</code></div>
            </details>
          </section>
        </aside>
      </div>
      {detail.lifecycleCommandsAvailable
        && detail.lifecycleState !== 'terminal'
        && viewingCurrentRevision
        && !detail.archivedAt && (
        <section className="experiment-command-bar">
          <div>
            <strong>实验生命周期</strong>
            <span>大计划采用滚动生成，可暂停后从原游标继续；取消当前 Revision 不可恢复，但已有结果继续保留。</span>
          </div>
          {detail.lifecycleState === 'active' && (
            <button className="app-button is-quiet" type="button" disabled={pausing} onClick={() => { void handlePause(); }}>
              <Pause size={15} /> {pausing ? '正在暂停…' : '暂停实验'}
            </button>
          )}
          {['pausing', 'paused'].includes(detail.lifecycleState ?? 'active') && (
            <button className="app-button is-primary" type="button" disabled={resuming} onClick={() => { void handleResume('user_pause'); }}>
              <Play size={15} /> {resuming ? '正在继续…' : '继续实验'}
            </button>
          )}
          <button
            className="app-button is-quiet"
            type="button"
            disabled={cancelling || detail.lifecycleState === 'cancelling'}
            onClick={() => setCancelConfirmation({
              revisionId: detail.revision.revisionId,
              lifecycleVersion: detail.lifecycleVersion ?? 1,
              idempotencyKey: createBrowserUuid(),
              reason: '用户在实验详情页确认取消当前 Revision',
            })}
          >
            <Ban size={15} /> {detail.lifecycleState === 'cancelling' ? '正在取消…' : '取消实验'}
          </button>
        </section>
      )}
      {isAdmin && detail.archivedAt && !detail.purgeJobId && !retentionActive && (
        <section className="experiment-command-bar">
          <div>
            <strong>管理员物理清理</strong>
            <span>保留期届满后才可提交；操作不可逆，清理范围和原因会被冻结并审计。</span>
          </div>
          <button
            className="app-button is-quiet"
            type="button"
            disabled={lifecycleAction !== ''}
            onClick={() => { void handlePurge(); }}
          >
            <Trash2 size={15} /> {lifecycleAction === 'purge' ? '正在提交…' : '物理清理实验'}
          </button>
        </section>
      )}
      {cancelConfirmation && (
        <div
          className="shared-modal-backdrop"
          role="presentation"
          onMouseDown={() => {
            if (!cancelling) setCancelConfirmation(null);
          }}
        >
          <div
            className="shared-modal is-compact"
            role="dialog"
            aria-modal="true"
            aria-label="确认取消实验"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="shared-modal-heading">
              <div>
                <span>不可恢复的 Revision 命令</span>
                <h2>确认取消实验？</h2>
              </div>
              <button className="app-icon-button" type="button" disabled={cancelling} onClick={() => setCancelConfirmation(null)} aria-label="关闭取消确认">
                <X size={16} />
              </button>
            </div>
            <p className="shared-modal-note">
              取消后停止生成和物化新 Trial；已提交任务按普通回测取消语义异步收敛；已有结果、参数快照和审计记录继续保留。
            </p>
            <div className="experiment-preview-heading">
              <div><span>计划</span><strong>{detail.stage.plannedTrialCount}</strong></div>
              <div><span>已生成</span><strong>{detail.stage.generatedTrialCount}</strong></div>
              <div><span>已完成</span><strong>{detail.stage.terminalTrialCount}</strong></div>
              <div><span>待物化</span><strong>{detail.stage.readyTrialCount}</strong></div>
              <div><span>排队</span><strong>{detail.stage.queuedTrialCount}</strong></div>
              <div><span>运行</span><strong>{detail.stage.inFlightTrialCount}</strong></div>
              <div><span>尚未生成</span><strong>{detail.stage.remainingLogicalTrialCount}</strong></div>
            </div>
            <label>
              <span>取消原因</span>
              <textarea
                maxLength={500}
                value={cancelConfirmation.reason}
                onChange={(event) => setCancelConfirmation((current) => (
                  current ? { ...current, reason: event.target.value } : current
                ))}
              />
            </label>
            <div className="shared-modal-actions">
              <button className="app-button is-quiet" type="button" disabled={cancelling} onClick={() => setCancelConfirmation(null)}>返回</button>
              <button className="app-button is-primary" type="button" disabled={cancelling || !cancelConfirmation.reason.trim()} onClick={() => { void handleCancel(); }}>
                <Ban size={15} /> {cancelling ? '正在提交…' : error.includes('网络结果不明确') ? '重试取消' : '确认取消'}
              </button>
            </div>
          </div>
        </div>
      )}
      <ExperimentBacktestRunDrawer
        experimentId={experimentId}
        open={relatedRunsOpen}
        summary={relatedBacktestRuns}
        onClose={() => setRelatedRunsOpen(false)}
        onOpenRunDetail={(runId) => onOpenBacktestDetail?.(runId)}
      />
    </main>
  );
}

function draftDateSummary(draft: ExperimentDraft): string {
  return draft.dateScenarios.map((scenario) => (
    `${scenario.name} ${scenario.startDate}～${scenario.endDate}`
  )).join('；');
}

function ExperimentCopySummary({
  source,
  current,
  subject,
}: {
  source: ExperimentCopyTarget;
  current: ExperimentDraft;
  subject: ExperimentSubject;
}) {
  const baseline = source.initialDraft;
  const labels = new Map(subject.tunableInputCatalog.inputs.map((input) => (
    [input.inputId, input.sourceName]
  )));
  const changes: Array<{ label: string; before: string; after: string }> = [];
  const parameterIds = new Set([
    ...Object.keys(baseline.parameterValues),
    ...Object.keys(current.parameterValues),
  ]);
  parameterIds.forEach((inputId) => {
    const before = baseline.parameterValues[inputId] ?? '';
    const after = current.parameterValues[inputId] ?? '';
    if (before !== after) changes.push({
      label: labels.get(inputId) ?? inputId,
      before: before || '沿用发布值',
      after: after || '沿用发布值',
    });
  });
  if (baseline.capitalValues !== current.capitalValues) changes.push({
    label: '初始资金', before: baseline.capitalValues, after: current.capitalValues,
  });
  if (baseline.slippageValues !== current.slippageValues) changes.push({
    label: '滑点', before: baseline.slippageValues, after: current.slippageValues,
  });
  const baselineDates = draftDateSummary(baseline);
  const currentDates = draftDateSummary(current);
  if (baselineDates !== currentDates) changes.push({
    label: '日期场景', before: baselineDates, after: currentDates,
  });
  return (
    <section className="experiment-copy-summary">
      <header>
        <div><span>复制来源</span><strong>{source.experimentName}</strong></div>
        <small>Revision {source.revisionNo} · 创建后成为独立实验</small>
      </header>
      {changes.length === 0 ? (
        <p>运行配置尚未变化。修改参数候选、日期、资金或滑点后，差异会显示在这里。</p>
      ) : (
        <div className="experiment-copy-diff-list">
          {changes.map((change) => (
            <div key={change.label}>
              <strong>{change.label}</strong>
              <span>{change.before}</span>
              <i>→</i>
              <span>{change.after}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ExperimentCreator({
  userId,
  subjectRef,
  onChangeSubject,
  onCreated,
  copyTarget,
}: {
  userId: string;
  subjectRef: ExperimentSubjectRef;
  onChangeSubject: () => void;
  onCreated: (experimentId: string) => void;
  copyTarget?: ExperimentCopyTarget;
}) {
  const [subject, setSubject] = useState<ExperimentSubject | null>(null);
  const [draft, setDraft] = useState<ExperimentDraft | null>(null);
  const [preview, setPreview] = useState<ExperimentPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [hydrated, setHydrated] = useState(false);
  const draftRevisionRef = useRef(0);
  const createKeyRef = useRef<string | null>(null);
  const draftScope = copyTarget ? `copy:${copyTarget.revisionId}` : undefined;

  useEffect(() => {
    setLoading(true);
    setHydrated(false);
    fetchExperimentSubject(subjectRef).then((resolved) => {
      const restored = loadExperimentDraft(localStorage, userId, subjectRef, draftScope);
      setSubject(resolved);
      setDraft(restored ?? copyTarget?.initialDraft ?? createExperimentDraft(resolved));
      setMessage(restored
        ? '已恢复本机草稿；为防止数据漂移，提交前必须重新预览。'
        : copyTarget
          ? '已继承来源实验的冻结配置；修改后请重新生成预览。'
          : '配置就绪，尚未生成预览。');
      setPreview(null);
      setHydrated(true);
    }).catch((reason: unknown) => setError(errorMessage(reason))).finally(() => setLoading(false));
  }, [copyTarget, draftScope, subjectRef, userId]);

  useEffect(() => {
    if (hydrated && draft) {
      saveExperimentDraft(localStorage, userId, subjectRef, draft, draftScope);
    }
  }, [draft, draftScope, hydrated, subjectRef, userId]);

  const changeDraft = useCallback((change: DraftChange) => {
    draftRevisionRef.current += 1;
    setDraft((current) => current ? change(current) : current);
    setPreview(null);
    createKeyRef.current = null;
    setError('');
    setMessage('配置已变化，之前的预览令牌已失效。');
  }, []);

  const handlePreview = async () => {
    if (!draft) return;
    const previewRevision = draftRevisionRef.current;
    setPreviewing(true);
    setError('');
    try {
      const result = await previewExperiment(buildPreviewRequest(subjectRef, draft));
      if (previewRevision !== draftRevisionRef.current) return;
      setPreview(result);
      createKeyRef.current = null;
      setMessage(result.limitDecision.allowed ? '服务端预览完成。' : '服务端预览完成，但当前配置不可进入后续创建阶段。');
    } catch (reason) {
      if (previewRevision !== draftRevisionRef.current) return;
      setPreview(null);
      setError(errorMessage(reason));
    } finally {
      setPreviewing(false);
    }
  };

  const finishCreation = (result: ExperimentCreateResult) => {
    clearExperimentDraft(localStorage, userId, subjectRef, draftScope);
    setMessage('实验已创建，Trial 将由后台协调器继续生成。');
    onCreated(result.experimentId);
  };

  const acceptCreationResult = (result: ExperimentCreateResponse): boolean => {
    if (result.status === 'processing') {
      setMessage('创建请求仍在处理中；再次提交会继续使用同一请求键。');
      return false;
    }
    finishCreation(result);
    return true;
  };

  const handleCreate = async () => {
    if (!draft || !preview || !preview.limitDecision.allowed || submitting) return;
    const idempotencyKey = createKeyRef.current ?? createBrowserUuid();
    createKeyRef.current = idempotencyKey;
    setSubmitting(true);
    setError('');
    try {
      const createRequest = {
        previewToken: preview.previewToken,
        specHash: preview.specHash,
        experimentSpec: buildPreviewRequest(subjectRef, draft),
        source: copyTarget ? {
          experimentId: copyTarget.experimentId,
          revisionId: copyTarget.revisionId,
        } : undefined,
      };
      const result = await createExperiment(createRequest, idempotencyKey);
      acceptCreationResult(result);
    } catch (reason) {
      if (reason instanceof TypeError) {
        try {
          const recovered = await findCreatedExperiment(idempotencyKey);
          if (acceptCreationResult(recovered)) return;
        } catch (lookupError) {
          setError(`创建结果暂不确定；再次提交将复用原请求键。${errorMessage(lookupError)}`);
        }
      } else {
        setError(errorMessage(reason));
        if (reason instanceof ApiResponseError && (
          reason.code === 'experiment.preview_stale'
          || reason.code === 'experiment.preview_expired'
          || reason.code === 'experiment.source_not_found'
        )) {
          setPreview(null);
          createKeyRef.current = null;
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <main className="experiment-page"><div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> 正在解析发布版本…</div></main>;
  if (!subject || !draft) return <main className="experiment-page"><div className="experiment-alert is-error">{error || '发布版本无法解析'}</div><button className="app-button is-quiet" onClick={onChangeSubject}>重新选择</button></main>;
  return (
    <main className="experiment-page">
      <section className="experiment-hero">
        <div>
          <span className="experiment-eyebrow">有限网格 / 服务端预览</span>
          <h1>{subject.displayName}</h1>
          <p>{copyTarget
            ? '已复制来源实验的冻结配置；确认后将创建独立实验，不改写来源结果。'
            : '定义有限候选空间，由服务端规范化、展开并冻结数据与运行契约。'}</p>
        </div>
        <button className="app-button is-quiet" type="button" onClick={onChangeSubject}><ArrowLeft size={15} /> {copyTarget ? '返回来源实验' : '更换策略'}</button>
      </section>
      {(error || message) && <div className={`experiment-alert ${error ? 'is-error' : ''}`} role={error ? 'alert' : 'status'}>{error || message}</div>}
      {copyTarget && <ExperimentCopySummary source={copyTarget} current={draft} subject={subject} />}
      <section className="experiment-name-row">
        <label className="experiment-field"><span>实验名称</span><input value={draft.name} onChange={(event) => changeDraft((current) => ({ ...current, name: event.target.value }))} /></label>
        <label className="experiment-field"><span>说明</span><input value={draft.description} placeholder="可选" onChange={(event) => changeDraft((current) => ({ ...current, description: event.target.value }))} /></label>
      </section>
      <div className="experiment-layout">
        <FixedStructure subject={subject} />
        <ParameterGrid inputs={subject.tunableInputCatalog.inputs} values={draft.parameterValues} onChange={(inputId, value) => changeDraft((current) => ({ ...current, parameterValues: { ...current.parameterValues, [inputId]: value } }))} />
        <DateScenarios scenarios={draft.dateScenarios} onChange={(dateScenarios) => changeDraft((current) => ({ ...current, dateScenarios }))} />
        <RuntimeDimensions draft={draft} onChange={(patch) => changeDraft((current) => ({ ...current, ...patch }))} />
      </div>
      <section className="experiment-command-bar">
        <div><strong>服务端权威预览</strong><span>不会创建 Experiment、Trial 或回测任务。</span></div>
        <button className="app-button is-primary" type="button" disabled={previewing} onClick={() => { void handlePreview(); }}>
          <FlaskConical size={15} /> {previewing ? '正在展开网格…' : preview ? '重新生成预览' : '生成预览'}
        </button>
      </section>
      {preview && <PreviewResult preview={preview} />}
      {preview && preview.limitDecision.allowed && (
        <section className="experiment-command-bar is-confirm">
          <div>
            <strong>{copyTarget ? '确认创建独立实验并开始' : '确认冻结并开始'}</strong>
            <span>创建事务快速返回，关闭页面不会停止 Trial 生成。</span>
          </div>
          <button className="app-button is-primary" type="button" disabled={submitting || previewing} onClick={() => { void handleCreate(); }}>
            <FlaskConical size={15} /> {submitting
              ? '正在创建…'
              : copyTarget ? '创建新实验并开始' : '确认创建并开始'}
          </button>
        </section>
      )}
    </main>
  );
}

export default function ExperimentPage({
  userId,
  isAdmin = false,
  enabled = true,
  subject,
  experimentId,
  onExperimentChange,
  onSubjectChange,
  onBackToStrategy,
  onOpenRun,
  onOpenBacktestDetail,
}: Props) {
  const [copyTarget, setCopyTarget] = useState<ExperimentCopyTarget | null>(null);
  if (enabled === null) {
    return (
      <main className="experiment-page">
        <div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> 正在读取自动化回测能力…</div>
      </main>
    );
  }
  if (!enabled) {
    return (
      <main className="experiment-page">
        <div className="experiment-alert is-warning" role="alert">
          自动化回测能力尚未启用。完成统一调度和服务器容量门禁验证后，管理员才能开放普通用户入口。
        </div>
        <button className="app-button is-quiet" type="button" onClick={onBackToStrategy}>
          <ArrowLeft size={15} /> 返回策略回测
        </button>
      </main>
    );
  }
  const selection = subject;
  const changeExperiment = onExperimentChange ?? (() => {});
  if (copyTarget) {
    return (
      <ExperimentCreator
        key={`copy:${copyTarget.revisionId}`}
        userId={userId}
        subjectRef={copyTarget.subjectRef}
        copyTarget={copyTarget}
        onChangeSubject={() => setCopyTarget(null)}
        onCreated={(createdExperimentId) => {
          setCopyTarget(null);
          changeExperiment(createdExperimentId);
        }}
      />
    );
  }
  if (experimentId) {
    return (
      <ExperimentDetailView
        experimentId={experimentId}
        isAdmin={isAdmin}
        onClose={() => changeExperiment(null)}
        onOpenRun={onOpenRun}
        onOpenBacktestDetail={onOpenBacktestDetail}
        onCopyExperiment={(detail) => setCopyTarget({
          experimentId: detail.experimentId,
          revisionId: detail.revision.revisionId,
          revisionNo: detail.revision.revisionNo,
          experimentName: detail.name,
          subjectRef: detail.frozenDefinition.subjectRef,
          initialDraft: createCopiedExperimentDraft(detail),
        })}
      />
    );
  }
  if (!selection) {
    return (
      <ExperimentLanding
        isAdmin={isAdmin}
        onSelect={onSubjectChange}
        onOpenExperiment={changeExperiment}
        onBack={onBackToStrategy}
      />
    );
  }
  return (
    <ExperimentCreator
      key={`${selection.subjectId}:${selection.subjectVersion}`}
      userId={userId}
      subjectRef={selection}
      onChangeSubject={() => onSubjectChange(null)}
      onCreated={changeExperiment}
    />
  );
}
