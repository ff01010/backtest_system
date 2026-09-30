import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  Ban,
  FlaskConical,
  LockKeyhole,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import { ApiResponseError, fetchStrategyConfigs } from '../../api/client';
import type { StrategyConfig } from '../../types';
import { createBrowserUuid } from '../../utils/browserUuid';
import {
  cancelExperiment,
  createExperiment,
  fetchExperimentDetail,
  fetchExperiments,
  fetchExperimentSubject,
  fetchExperimentTrials,
  findCreatedExperiment,
  previewExperiment,
} from './api';
import type {
  DateScenarioDraft,
  ExperimentCreateResponse,
  ExperimentCreateResult,
  ExperimentDetail,
  ExperimentDraft,
  ExperimentListItem,
  ExperimentPreview,
  ExperimentSubject,
  ExperimentSubjectRef,
  ExperimentTrial,
  TunableInput,
} from './contracts';
import {
  buildPreviewRequest,
  clearExperimentDraft,
  createExperimentDraft,
  loadExperimentDraft,
  saveExperimentDraft,
} from './draftStore';

type Props = {
  userId: string;
  subject: ExperimentSubjectRef | null;
  experimentId?: string | null;
  onExperimentChange?: (experimentId: string | null) => void;
  onSubjectChange: (subject: ExperimentSubjectRef | null) => void;
  onBackToStrategy: () => void;
  onOpenRun?: (runId: string, symbol: string, timeframe: string, date: string) => void;
};

type DraftChange = (draft: ExperimentDraft) => ExperimentDraft;

const ACTIVE_EXPERIMENT_STATUSES = new Set(['preparing', 'queued', 'running', 'cancelling']);
const CANCELLABLE_EXPERIMENT_STATUSES = new Set(['preparing', 'generation_blocked']);

function shortDigest(value: unknown): string {
  return typeof value === 'string' && value ? `${value.slice(0, 12)}…` : '未提供';
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return `${error.message}${error.requestId ? `（请求 ${error.requestId}）` : ''}`;
  }
  return error instanceof Error ? error.message : '请求未完成';
}

function isStandardBarConfig(config: StrategyConfig): boolean {
  const params = config.paramsJson.params;
  const fillModel = params && typeof params === 'object'
    ? (params as Record<string, unknown>).fillModel
    : null;
  return config.enabled
    && config.published
    && Boolean(config.publishedVersionId)
    && config.mode === 'backtest'
    && fillModel !== 'l2_quote';
}

function ExperimentLanding({
  onSelect,
  onOpenExperiment,
  onBack,
}: {
  onSelect: (subject: ExperimentSubjectRef) => void;
  onOpenExperiment: (experimentId: string) => void;
  onBack: () => void;
}) {
  const [configs, setConfigs] = useState<StrategyConfig[]>([]);
  const [experiments, setExperiments] = useState<ExperimentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    Promise.all([fetchStrategyConfigs(), fetchExperiments()])
      .then(([items, page]) => {
        setConfigs(items.filter(isStandardBarConfig));
        setExperiments(page.items);
      })
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <main className="experiment-page experiment-landing">
      <section className="experiment-hero">
        <div>
          <span className="experiment-eyebrow">AUTOMATION / FINITE GRID</span>
          <h1>选择一个发布版本</h1>
          <p>自动化预览仅使用当前用户可运行的标准 Bar 单标的策略，结构字段在整个实验中保持冻结。</p>
        </div>
        <button className="app-button is-quiet" type="button" onClick={onBack}>
          <ArrowLeft size={15} /> 返回策略回测
        </button>
      </section>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      {!loading && experiments.length > 0 && (
        <section className="experiment-preview-card">
          <h2>最近实验</h2>
          <div className="experiment-trial-table">
            {experiments.map((experiment) => (
              <button
                className="experiment-trial-row"
                key={experiment.experimentId}
                type="button"
                onClick={() => onOpenExperiment(experiment.experimentId)}
              >
                <strong>{experiment.name}</strong>
                <span>{experiment.symbol} · {experiment.timeframe}</span>
                <span>{experiment.status}</span>
                <span>
                  成功 {experiment.succeededTrialCount} / {experiment.plannedTrialCount}
                </span>
              </button>
            ))}
          </div>
        </section>
      )}
      <section className="experiment-subject-grid" aria-busy={loading}>
        {loading && <div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> 正在读取策略版本…</div>}
        {!loading && configs.length === 0 && (
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
            <span>{config.symbol} · {config.timeframe} · 脚本 v{config.scriptVersionNo}</span>
            <small>发布版本 {shortDigest(config.publishedVersionId)}</small>
          </button>
        ))}
      </section>
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
    ['成交模型', fixed.fillModel],
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
          <span>执行算法</span><strong>immediate</strong><small>标准 Bar 首期固定</small>
        </div>
      </div>
    </section>
  );
}

function PreviewResult({ preview }: { preview: ExperimentPreview }) {
  return (
    <section className={`experiment-preview ${preview.limitDecision.allowed ? 'is-allowed' : 'is-blocked'}`}>
      <div className="experiment-preview-metrics">
        <div><span>Trial 总数</span><strong>{preview.plannedTrialCount.toLocaleString()}</strong></div>
        <div><span>预计 Bar</span><strong>{preview.resourceEstimate.estimatedBarCount.toLocaleString()}</strong></div>
        <div><span>资源等级</span><strong>{preview.resourceEstimate.resourceClass}</strong></div>
        <div><span>维度数量</span><strong>{Object.keys(preview.dimensionCounts).length}</strong></div>
      </div>
      <div className="experiment-preview-decision">
        <ShieldCheck size={18} />
        <div>
          <strong>{preview.limitDecision.allowed ? '预览通过资源边界检查' : '预览存在阻塞项'}</strong>
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
  onClose,
  onOpenRun,
}: {
  experimentId: string;
  onClose: () => void;
  onOpenRun?: (runId: string, symbol: string, timeframe: string, date: string) => void;
}) {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [trials, setTrials] = useState<ExperimentTrial[]>([]);
  const [nextTrialCursor, setNextTrialCursor] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [cancelling, setCancelling] = useState(false);
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
        const trialPage = await fetchExperimentTrials(experimentId);
        if (stopped) return;
        setTrials(trialPage.items);
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
  }, [experimentId]);

  const handleCancel = async () => {
    setCancelling(true);
    setError('');
    try {
      await cancelExperiment(experimentId, createBrowserUuid());
      setDetail(await fetchExperimentDetail(experimentId));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setCancelling(false);
    }
  };

  const loadMoreTrials = async () => {
    if (!nextTrialCursor) return;
    try {
      const page = await fetchExperimentTrials(experimentId, nextTrialCursor);
      setTrials((current) => [...current, ...page.items]);
      setNextTrialCursor(page.nextCursor);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  if (!detail) {
    return <main className="experiment-page"><div className="experiment-empty"><RefreshCw className="is-spinning" size={20} /> {error || '正在读取实验进度…'}</div></main>;
  }
  const progress = Math.round(
    (detail.stage.generatedTrialCount / detail.stage.plannedTrialCount) * 100,
  );
  const fixed = detail.frozenDefinition.fixedStructure;
  return (
    <main className="experiment-page">
      <section className="experiment-hero">
        <div>
          <span className="experiment-eyebrow">EXPERIMENT / {detail.status.toUpperCase()}</span>
          <h1>{detail.name}</h1>
          <p>{detail.description || '冻结定义已创建；后续生成不依赖当前浏览器。'}</p>
        </div>
        <button className="app-button is-quiet" type="button" onClick={onClose}><ArrowLeft size={15} /> 返回实验入口</button>
      </section>
      {error && <div className="experiment-alert is-error" role="alert">{error}</div>}
      {detail.blockedReason && (
        <div className="experiment-alert is-error" role="alert">
          {detail.blockedReason.message}（{detail.blockedReason.code}）
        </div>
      )}
      <section className="experiment-preview-card">
        <div className="experiment-preview-heading">
          <div><span>当前阶段</span><strong>{detail.stage.status}</strong></div>
          <div><span>Trial 生成</span><strong>{detail.stage.generatedTrialCount} / {detail.stage.plannedTrialCount}</strong></div>
          <div><span>终态</span><strong>{detail.stage.terminalTrialCount} / {detail.stage.plannedTrialCount}</strong></div>
        </div>
        <div className="experiment-progress" aria-label="Trial 生成进度">
          <span style={{ width: `${progress}%` }} />
        </div>
        <div className="experiment-digest">Spec Hash <code>{detail.revision.specHash}</code></div>
        <div className="experiment-digest">Trial 集合 <code>{shortDigest(detail.stage.trialCollectionDigest)}</code></div>
      </section>
      <section className="experiment-structure-card">
        <h2>冻结定义</h2>
        <div className="experiment-structure-grid">
          <div><span>标的</span><strong>{String(fixed.symbol ?? '—')}</strong></div>
          <div><span>周期</span><strong>{String(fixed.timeframe ?? '—')}</strong></div>
          <div><span>成交模型</span><strong>{String(fixed.fillModel ?? '—')}</strong></div>
          <div><span>数据场景</span><strong>{detail.frozenDefinition.datasetSnapshots.length}</strong></div>
        </div>
      </section>
      <section className="experiment-preview-card">
        <h2>Trial 运行</h2>
        <div className="experiment-preview-heading">
          <div><span>排队</span><strong>{detail.stage.queuedTrialCount}</strong></div>
          <div><span>在途</span><strong>{detail.stage.inFlightTrialCount}</strong></div>
          <div><span>成功</span><strong>{detail.stage.succeededTrialCount}</strong></div>
          <div><span>失败</span><strong>{detail.stage.failedTrialCount}</strong></div>
          <div><span>取消</span><strong>{detail.stage.cancelledTrialCount}</strong></div>
        </div>
        <div className="experiment-trial-table">
          {trials.map((trial) => {
            const dateScenario = trial.dimensionAssignment.dateScenario as Record<string, unknown> | undefined;
            return (
              <div className="experiment-trial-row" key={trial.trialId}>
                <strong>#{trial.trialIndex + 1}</strong>
                <span>{trial.summaryStatus}</span>
                <span>{String(dateScenario?.name ?? dateScenario?.scenarioId ?? '—')}</span>
                {trial.runId ? (
                  <button
                    className="app-button is-quiet"
                    type="button"
                    onClick={() => onOpenRun?.(
                      trial.runId!,
                      String(fixed.symbol ?? ''),
                      String(fixed.timeframe ?? '5m'),
                      String(dateScenario?.startDate ?? '').replace(/-/g, ''),
                    )}
                  >
                    查看单次结果
                  </button>
                ) : <span>尚未物化</span>}
              </div>
            );
          })}
          {trials.length === 0 && <div className="experiment-empty">Trial 尚未生成。</div>}
        </div>
        {nextTrialCursor && (
          <button className="app-button is-quiet" type="button" onClick={() => { void loadMoreTrials(); }}>
            加载更多 Trial
          </button>
        )}
      </section>
      {CANCELLABLE_EXPERIMENT_STATUSES.has(detail.status) && (
        <section className="experiment-command-bar">
          <div><strong>停止生成</strong><span>当前仅支持在执行物化前取消，已生成内容和审计记录继续保留。</span></div>
          <button className="app-button is-quiet" type="button" disabled={cancelling} onClick={() => { void handleCancel(); }}>
            <Ban size={15} /> {cancelling ? '正在取消…' : '取消实验'}
          </button>
        </section>
      )}
    </main>
  );
}

function ExperimentCreator({
  userId,
  subjectRef,
  onChangeSubject,
  onCreated,
}: {
  userId: string;
  subjectRef: ExperimentSubjectRef;
  onChangeSubject: () => void;
  onCreated: (experimentId: string) => void;
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

  useEffect(() => {
    setLoading(true);
    setHydrated(false);
    fetchExperimentSubject(subjectRef).then((resolved) => {
      const restored = loadExperimentDraft(localStorage, userId, subjectRef);
      setSubject(resolved);
      setDraft(restored ?? createExperimentDraft(resolved));
      setMessage(restored ? '已恢复本机草稿；为防止数据漂移，提交前必须重新预览。' : '配置就绪，尚未生成预览。');
      setPreview(null);
      setHydrated(true);
    }).catch((reason: unknown) => setError(errorMessage(reason))).finally(() => setLoading(false));
  }, [subjectRef, userId]);

  useEffect(() => {
    if (hydrated && draft) saveExperimentDraft(localStorage, userId, subjectRef, draft);
  }, [draft, hydrated, subjectRef, userId]);

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
    clearExperimentDraft(localStorage, userId, subjectRef);
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
      const result = await createExperiment({
        previewToken: preview.previewToken,
        specHash: preview.specHash,
        experimentSpec: buildPreviewRequest(subjectRef, draft),
      }, idempotencyKey);
      acceptCreationResult(result);
    } catch (reason) {
      if (reason instanceof TypeError) {
        try {
          if (acceptCreationResult(await findCreatedExperiment(idempotencyKey))) return;
        } catch (lookupError) {
          setError(`创建结果暂不确定；再次提交将复用原请求键。${errorMessage(lookupError)}`);
        }
      } else {
        setError(errorMessage(reason));
        if (reason instanceof ApiResponseError && (
          reason.code === 'experiment.preview_stale'
          || reason.code === 'experiment.preview_expired'
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
          <span className="experiment-eyebrow">FINITE GRID / SERVER PREVIEW</span>
          <h1>{subject.displayName}</h1>
          <p>定义有限候选空间，由服务端规范化、展开并冻结数据与运行契约。</p>
        </div>
        <button className="app-button is-quiet" type="button" onClick={onChangeSubject}><ArrowLeft size={15} /> 更换策略</button>
      </section>
      {(error || message) && <div className={`experiment-alert ${error ? 'is-error' : ''}`} role={error ? 'alert' : 'status'}>{error || message}</div>}
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
          <div><strong>确认冻结并开始</strong><span>创建事务快速返回，关闭页面不会停止 Trial 生成。</span></div>
          <button className="app-button is-primary" type="button" disabled={submitting || previewing} onClick={() => { void handleCreate(); }}>
            <FlaskConical size={15} /> {submitting ? '正在创建…' : '确认创建并开始'}
          </button>
        </section>
      )}
    </main>
  );
}

export default function ExperimentPage({
  userId,
  subject,
  experimentId,
  onExperimentChange,
  onSubjectChange,
  onBackToStrategy,
  onOpenRun,
}: Props) {
  const selection = subject;
  const changeExperiment = onExperimentChange ?? (() => {});
  if (experimentId) {
    return (
      <ExperimentDetailView
        experimentId={experimentId}
        onClose={() => changeExperiment(null)}
        onOpenRun={onOpenRun}
      />
    );
  }
  if (!selection) {
    return (
      <ExperimentLanding
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
