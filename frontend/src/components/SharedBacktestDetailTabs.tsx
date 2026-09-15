import { useEffect, useState } from 'react';
import {
  fetchSharedBacktestChart,
  fetchSharedBacktestResultPage,
} from '../api/client';
import type {
  SharedBacktestChart,
  SharedBacktestDetail,
  SharedBacktestResultPage,
  SharedBacktestResultSection,
} from '../types';

type DetailTab = 'overview' | 'code' | 'context' | 'returns' | 'activity';

const cardStyle = {
  border: '1px solid #d7dde8', borderRadius: 8, background: '#ffffff', padding: 18,
} as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '--';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function ContextGrid({ detail }: { detail: SharedBacktestDetail }) {
  const configuration = detail.configuration;
  const strategy = detail.strategy;
  const resultData = asRecord(detail.data.resultData);
  const marketData = asRecord(detail.data.marketData);
  const manifest = asRecord(detail.result.experimentManifest);
  const barRange = asRecord(manifest.barRange);
  const formalDateRange = asRecord(marketData.formalDateRange);
  const dataStart = barRange.start ?? resultData.startDate ?? resultData.actualStartDate
    ?? formalDateRange.start ?? detail.startDate;
  const dataEnd = barRange.end ?? resultData.endDate ?? resultData.actualEndDate
    ?? formalDateRange.end ?? detail.endDate;
  const items: Array<[string, unknown]> = [
    ['回测区间', `${detail.startDate} 至 ${detail.endDate}`],
    ['初始资金', configuration.initialCapital],
    ['手续费率', configuration.commissionRate ?? configuration.feeProfile],
    ['滑点', configuration.slippage],
    ['成交模型', configuration.fillModel],
    ['执行算法', configuration.executionAlgorithm ?? asRecord(configuration.execution).algorithm],
    ['策略类别', detail.strategyCategory],
    ['策略入口', strategy.entryType],
    ['运行时版本', strategy.runtimeVersion],
    ['数据范围', `${displayValue(dataStart)} 至 ${displayValue(dataEnd)}`],
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
      {items.map(([label, value]) => (
        <div key={String(label)} style={{ padding: 10, background: '#f8fafc', borderRadius: 6 }}>
          <small style={{ color: '#64748b' }}>{label}</small>
          <strong style={{ display: 'block', marginTop: 4 }}>{displayValue(value)}</strong>
        </div>
      ))}
    </div>
  );
}

function ChartPanel({ publicationId }: { publicationId: string }) {
  const [chart, setChart] = useState<SharedBacktestChart | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    fetchSharedBacktestChart(publicationId).then((value) => {
      if (!cancelled) setChart(value);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '收益曲线加载失败');
    });
    return () => { cancelled = true; };
  }, [publicationId]);
  if (error) return <div role="alert">{error}</div>;
  if (!chart) return <div>正在加载收益曲线...</div>;
  return (
    <section style={cardStyle}>
      <h3>收益与基准曲线</h3>
      <p>策略 {chart.equity.length} 个点 · 基准 {chart.benchmarkSymbol || '--'} {chart.benchmarkEquity.length} 个点</p>
      <JsonBlock value={chart} />
    </section>
  );
}

function PagedResultPanel(props: {
  publicationId: string;
  section: SharedBacktestResultSection;
  title: string;
}) {
  const [cursor, setCursor] = useState('0');
  const [history, setHistory] = useState<string[]>([]);
  const [page, setPage] = useState<SharedBacktestResultPage | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setError('');
    fetchSharedBacktestResultPage(props.publicationId, props.section, cursor).then((value) => {
      if (!cancelled) setPage(value);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : `${props.title}加载失败`);
    });
    return () => { cancelled = true; };
  }, [cursor, props.publicationId, props.section, props.title]);
  return (
    <section style={cardStyle}>
      <h3>{props.title}</h3>
      {error && <div role="alert">{error}</div>}
      {!error && !page && <div>正在加载{props.title}...</div>}
      {page && (
        <>
          <p>共 {page.total} 条，当前显示 {page.items.length} 条</p>
          {page.items.length > 0 ? <JsonBlock value={page.items} /> : <div>暂无数据</div>}
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button
              type="button"
              disabled={history.length === 0}
              onClick={() => {
                const previous = history[history.length - 1] ?? '0';
                setHistory((values) => values.slice(0, -1));
                setCursor(previous);
              }}
            >上一页</button>
            <button
              type="button"
              disabled={!page.nextCursor}
              onClick={() => {
                if (!page.nextCursor) return;
                setHistory((values) => [...values, cursor]);
                setCursor(page.nextCursor);
              }}
            >下一页</button>
          </div>
        </>
      )}
    </section>
  );
}

export default function SharedBacktestDetailTabs({ detail }: { detail: SharedBacktestDetail }) {
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');
  const result = detail.result;
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  const tabs: Array<[DetailTab, string]> = [
    ['overview', '概览'],
    ['code', '策略代码'],
    ['context', '参数与数据'],
    ['returns', '收益与持仓'],
    ['activity', '成交与信号'],
  ];
  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div role="tablist" aria-label="共享回测详情" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {tabs.map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={activeTab === value}
            onClick={() => setActiveTab(value)}
          >{label}</button>
        ))}
      </div>
      {activeTab === 'overview' && (
        <section style={cardStyle}>
          <h2>判断上下文</h2>
          <ContextGrid detail={detail} />
          <h3>收益摘要与基准</h3>
          <JsonBlock value={{
            summary: result.summary,
            strategyMetrics: result.strategyMetrics,
            benchmarkSymbol: result.benchmarkSymbol,
            benchmarkReturn: result.benchmarkReturn,
            excessReturn: result.excessReturn,
          }} />
          <h3>运行警告</h3>
          {warnings.length > 0
            ? <ul>{warnings.map((warning, index) => <li key={index}>{displayValue(warning)}</li>)}</ul>
            : <div>无运行警告</div>}
        </section>
      )}
      {activeTab === 'code' && (
        <section style={cardStyle}>
          <h2>策略代码</h2>
          <p>入口：{displayValue(detail.strategy.entryType)} · 运行时：{displayValue(detail.strategy.runtimeVersion)}</p>
          <p>代码校验值（SHA-256）：{displayValue(detail.strategy.codeHash)}</p>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {displayValue(detail.strategy.code)}
          </pre>
        </section>
      )}
      {activeTab === 'context' && (
        <section style={cardStyle}>
          <h2>参数与数据</h2>
          <ContextGrid detail={detail} />
          <JsonBlock value={{ configuration: detail.configuration, data: detail.data }} />
        </section>
      )}
      {activeTab === 'returns' && (
        <>
          <ChartPanel publicationId={detail.id} />
          <PagedResultPanel publicationId={detail.id} section="holdings" title="每日持仓" />
          <PagedResultPanel publicationId={detail.id} section="cycles" title="闭环交易" />
        </>
      )}
      {activeTab === 'activity' && (
        <>
          <PagedResultPanel publicationId={detail.id} section="trades" title="成交记录" />
          <PagedResultPanel publicationId={detail.id} section="signals" title="跳过信号" />
          <PagedResultPanel publicationId={detail.id} section="execution-plans" title="执行计划" />
        </>
      )}
    </div>
  );
}
