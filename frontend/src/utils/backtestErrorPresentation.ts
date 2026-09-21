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
    || detail.code === 'runtime.version_mismatch'
    || detail.code === 'runtime.dependency_missing'
    || detail.code === 'runtime.dependency_load_failed';
  const isOperatorDataError = detail.code === 'data.l2_quote_order_ambiguous'
    || detail.code === 'data.l2_bar_partial'
    || detail.code === 'data.l2_bar_price_invalid';
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
    title: isRuntimePlatformError
      ? '平台运行环境异常'
      : isOperatorDataError
        ? '行情数据异常'
        : detail.category === 'strategy_error' ? '策略执行失败' : '回测未完成',
    isUserActionable: detail.category === 'strategy_error' && !isRuntimePlatformError,
    location: locationParts.join(' · '),
  };
}
