import type { BacktestErrorDetail } from '../types';

export interface BacktestErrorPresentation {
  title: string;
  isUserActionable: boolean;
  location: string;
  message: string;
  hint: string | null;
}

export function backtestErrorPresentation(
  detail: BacktestErrorDetail,
): BacktestErrorPresentation {
  const code = typeof detail.code === 'string' ? detail.code : '';
  const isRuntimePlatformError = code === 'runtime.contract_missing'
    || code === 'runtime.version_mismatch'
    || code === 'runtime.dependency_missing'
    || code === 'runtime.dependency_load_failed';
  const isDataError = detail.category === 'data'
    || code.startsWith('data.')
    || code.startsWith('clickhouse.')
    || code.startsWith('l2.config.')
    || code.startsWith('cache.redis.');
  const locationParts = [
    detail.callback,
    detail.strategyLine ? `第 ${detail.strategyLine} 行` : null,
    detail.missingName ? `名称：${detail.missingName}` : null,
    detail.requestedModule ? `模块：${detail.requestedModule}` : null,
    detail.symbol,
    detail.tradeDate,
    detail.eventTime,
  ].filter((item): item is string => Boolean(item));
  return {
    title: isDataError
      ? '行情数据异常'
      : isRuntimePlatformError
        ? '平台运行环境异常'
        : detail.category === 'strategy_error' ? '策略执行失败' : '回测未完成',
    isUserActionable: detail.category === 'strategy_error' && !isRuntimePlatformError && !isDataError,
    location: isDataError ? '' : locationParts.join(' · '),
    message: isDataError ? '行情数据异常，请联系管理员' : detail.message,
    hint: isDataError ? null : detail.hint || null,
  };
}
