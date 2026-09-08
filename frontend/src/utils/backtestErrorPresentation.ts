import type { BacktestErrorDetail } from '../types';

export interface BacktestErrorPresentation {
  title: string;
  isUserActionable: boolean;
  location: string;
}

export function backtestErrorPresentation(
  detail: BacktestErrorDetail,
): BacktestErrorPresentation {
  const isRuntimePlatformError = detail.code === 'runtime.contract_missing'
    || detail.code === 'runtime.version_mismatch';
  const locationParts = [
    detail.callback,
    detail.strategyLine ? `第 ${detail.strategyLine} 行` : null,
    detail.missingName ? `名称：${detail.missingName}` : null,
    detail.symbol,
    detail.eventTime,
  ].filter((item): item is string => Boolean(item));
  return {
    title: isRuntimePlatformError
      ? '平台运行环境异常'
      : detail.category === 'strategy_error' ? '策略执行失败' : '回测未完成',
    isUserActionable: detail.category === 'strategy_error' && !isRuntimePlatformError,
    location: locationParts.join(' · '),
  };
}
