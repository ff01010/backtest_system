import { useEffect, useRef, useCallback, useState } from 'react';
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type SeriesMarker,
  type Time,
  ColorType,
} from 'lightweight-charts';
import type {
  BacktestChartTrade,
  ChartResponse,
  TradeMarker,
  DisplayModule,
  DisplaySide,
} from '../types';
import { formatChartTimestamp, toChartTimestamp } from '../utils/chartTime';

interface Props {
  data: ChartResponse | null;
  modules: DisplayModule[];
  sides: DisplaySide[];
  showPrices: boolean;
  markerSize: 'small' | 'normal' | 'large';
  signalSource?: 'actual' | 'theoretical';
  label?: string;
  labelColor?: string;
  backtestTrades?: BacktestChartTrade[];
  backtestDisplayNo?: number;
  backtestStrategyCategory?: 'core_position_overlay' | 'directional_holding';
}

// markerSize → lightweight-charts 原生 size 值 (Circle/Arrow 尺寸基数)
const MARKER_SIZE_MAP = { small: 1, normal: 2, large: 3 };

const A_COLOR = '#f23645';
const B_COLOR = '#089981';
const BACKTEST_BUY_COLOR = '#f23645';
const BACKTEST_SELL_COLOR = '#089981';
const BACKTEST_T_COLOR = '#f59e0b';
const BACKTEST_MARKER_MIN_VERTICAL_GAP = 24;

const BACKTEST_ACTION_LABELS: Record<BacktestChartTrade['action'], string> = {
  buy_open: '买开',
  sell_close: '卖平',
  sell_open: '卖开',
  buy_close: '买平',
  buy: '买入',
  sell: '卖出',
};

interface BacktestTradeMarkerLayout {
  key: string;
  time: number;
  x: number;
  anchorX: number;
  y: number;
  anchorY: number;
  radius: number;
  color: string;
  label: string | null;
  trades: BacktestChartTrade[];
}

function isBacktestBuyAction(action: BacktestChartTrade['action']): boolean {
  return action.startsWith('buy');
}

function isBacktestCloseAction(action: BacktestChartTrade['action']): boolean {
  return action === 'buy_close' || action === 'sell_close';
}

function getBacktestMarkerLabel(
  action: BacktestChartTrade['action'],
  strategyCategory: Props['backtestStrategyCategory'],
): string {
  if (isBacktestCloseAction(action)) {
    return strategyCategory === 'core_position_overlay' ? 'T' : '平';
  }
  return isBacktestBuyAction(action) ? 'B' : 'S';
}

function getBacktestMarkerColor(
  action: BacktestChartTrade['action'],
  strategyCategory: Props['backtestStrategyCategory'],
): string {
  if (isBacktestCloseAction(action) && strategyCategory === 'core_position_overlay') {
    return BACKTEST_T_COLOR;
  }
  return isBacktestBuyAction(action) ? BACKTEST_BUY_COLOR : BACKTEST_SELL_COLOR;
}

function getBacktestTradeLabel(
  action: BacktestChartTrade['action'],
  strategyCategory: Props['backtestStrategyCategory'],
): string {
  if (isBacktestCloseAction(action) && strategyCategory === 'core_position_overlay') {
    return `T（${BACKTEST_ACTION_LABELS[action]}）`;
  }
  return BACKTEST_ACTION_LABELS[action];
}

function isSameBacktestMarkerLayout(
  current: BacktestTradeMarkerLayout[],
  next: BacktestTradeMarkerLayout[]
): boolean {
  return current.length === next.length && current.every((item, index) => {
    const candidate = next[index];
    return item.key === candidate.key
      && Math.abs(item.x - candidate.x) < 0.1
      && Math.abs(item.anchorX - candidate.anchorX) < 0.1
      && Math.abs(item.y - candidate.y) < 0.1
      && Math.abs(item.anchorY - candidate.anchorY) < 0.1
      && item.color === candidate.color
      && item.label === candidate.label;
  });
}

function compactTradeTime(value: string): string {
  return value.replace(/\D/g, '').slice(0, 12);
}

function spreadBacktestMarkerYPositions(positions: number[]): number[] {
  const result = [...positions];
  let start = 0;
  for (let index = 1; index <= positions.length; index += 1) {
    if (index < positions.length && positions[index] - positions[index - 1] <= BACKTEST_MARKER_MIN_VERTICAL_GAP) continue;
    const count = index - start;
    if (count > 1) {
      const center = positions.slice(start, index).reduce((sum, value) => sum + value, 0) / count;
      for (let offset = 0; offset < count; offset += 1) {
        result[start + offset] = center + (offset - (count - 1) / 2) * BACKTEST_MARKER_MIN_VERTICAL_GAP;
      }
    }
    start = index;
  }
  return result;
}

function alignTradeToBarTime(trade: BacktestChartTrade, bars: ChartResponse['bars']): number | null {
  const target = compactTradeTime(trade.executionTime);
  if (target.length !== 12 || bars.length === 0) return null;
  const targetTime = toChartTimestamp(
    `${target.slice(0, 4)}-${target.slice(4, 6)}-${target.slice(6, 8)}T${target.slice(8, 10)}:${target.slice(10, 12)}:00`
  );
  const aligned = bars.find((bar) => toChartTimestamp(bar.time) >= targetTime) || bars[bars.length - 1];
  return toChartTimestamp(aligned.time);
}

function escapeTooltipText(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char] || char));
}

export default function KlineChart({
  data,
  modules,
  sides,
  showPrices,
  markerSize,
  signalSource,
  label,
  labelColor,
  backtestTrades,
  backtestDisplayNo,
  backtestStrategyCategory,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const closeSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const [backtestMarkerLayouts, setBacktestMarkerLayouts] = useState<BacktestTradeMarkerLayout[]>([]);
  const backtestMarkerLayoutsRef = useRef<BacktestTradeMarkerLayout[]>([]);
  const requestBacktestMarkerLayoutRef = useRef<() => void>(() => {});

  // 用 ref 存储最新值，避免 crosshair handler 因闭包过期而反复订阅
  const dataRef = useRef(data);
  dataRef.current = data;
  const modulesRef = useRef(modules);
  modulesRef.current = modules;
  const sidesRef = useRef(sides);
  sidesRef.current = sides;
  const backtestTradesRef = useRef(backtestTrades);
  backtestTradesRef.current = backtestTrades;
  const backtestDisplayNoRef = useRef(backtestDisplayNo);
  backtestDisplayNoRef.current = backtestDisplayNo;
  const backtestStrategyCategoryRef = useRef(backtestStrategyCategory);
  backtestStrategyCategoryRef.current = backtestStrategyCategory;
  backtestMarkerLayoutsRef.current = backtestMarkerLayouts;

  const scale = MARKER_SIZE_MAP[markerSize];

  const updateBacktestMarkerLayouts = useCallback(() => {
    const chart = chartRef.current;
    const candle = candleSeriesRef.current;
    if (!chart || !candle || !data || backtestTrades === undefined) {
      setBacktestMarkerLayouts((current) => current.length === 0 ? current : []);
      return;
    }

    const barsByTime = new Map(data.bars.map((bar) => [toChartTimestamp(bar.time), bar]));
    const grouped = new Map<number, BacktestChartTrade[]>();
    for (const trade of backtestTrades) {
      const time = alignTradeToBarTime(trade, data.bars);
      if (time === null) continue;
      grouped.set(time, [...(grouped.get(time) || []), trade]);
    }

    const next: BacktestTradeMarkerLayout[] = [];
    for (const [time, timeTrades] of grouped) {
      const bar = barsByTime.get(time);
      const x = chart.timeScale().timeToCoordinate(time as Time);
      if (!bar || x === null) continue;
      const actionGroups = new Map<BacktestChartTrade['action'], BacktestChartTrade[]>();
      for (const trade of timeTrades) {
        actionGroups.set(trade.action, [...(actionGroups.get(trade.action) || []), trade]);
      }
      const groups = Array.from(actionGroups.entries()).map(([action, actionTrades]) => {
        const quantity = actionTrades.reduce((total, trade) => total + trade.quantity, 0);
        const price = quantity > 0
          ? actionTrades.reduce((total, trade) => total + trade.price * trade.quantity, 0) / quantity
          : actionTrades[0].price;
        return { action, price, trades: actionTrades };
      });
      const markerGroups = groups.map((group) => {
        const y = candle.priceToCoordinate(group.price);
        const anchorPrice = isBacktestBuyAction(group.action) ? bar.low : bar.high;
        return { ...group, anchorY: candle.priceToCoordinate(anchorPrice), y };
      }).filter((group): group is typeof group & { y: number; anchorY: number } => (
        group.y !== null && group.anchorY !== null
      ));
      markerGroups.sort((left, right) => left.y - right.y || left.action.localeCompare(right.action));
      const displayYs = spreadBacktestMarkerYPositions(markerGroups.map((group) => group.y));
      for (const [index, group] of markerGroups.entries()) {
        const { action, trades: actionTrades, anchorY } = group;
        next.push({
          key: `${time}:${action}`,
          time,
          x,
          anchorX: x,
          y: displayYs[index],
          anchorY,
          radius: 10,
          color: getBacktestMarkerColor(action, backtestStrategyCategory),
          label: getBacktestMarkerLabel(action, backtestStrategyCategory),
          trades: actionTrades,
        });
      }
    }

    next.sort((left, right) => left.time - right.time || left.y - right.y);
    setBacktestMarkerLayouts((current) => isSameBacktestMarkerLayout(current, next) ? current : next);
  }, [backtestStrategyCategory, backtestTrades, data]);

  requestBacktestMarkerLayoutRef.current = updateBacktestMarkerLayouts;

  // ── 初始化图表（仅一次）─────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#6b7688',
      },
      grid: {
        vertLines: { color: '#eef2f8' },
        horzLines: { color: '#eef2f8' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: {
        borderColor: '#e3e9f2',
        scaleMargins: { top: 0.05, bottom: 0.22 },
      },
      localization: {
        timeFormatter: (time: Time) => {
          if (typeof time === 'number') {
            return formatChartTimestamp(time);
          }
          if (typeof time === 'string') {
            return time.slice(11, 16);  // "2026-06-22T09:35:00" → "09:35"
          }
          return String(time);
        },
      },
      timeScale: {
        borderColor: '#e3e9f2',
        timeVisible: true,
        secondsVisible: false,
        barSpacing: 12,
      },
      width: containerRef.current.clientWidth,
      height: Math.max(containerRef.current.clientHeight, 300),
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#f23645',
      downColor: '#089981',
      borderUpColor: '#f23645',
      borderDownColor: '#089981',
      wickUpColor: '#f23645',
      wickDownColor: '#089981',
    });
    candleSeriesRef.current = candleSeries;

    const closeSeries = chart.addLineSeries({
      color: '#5b6579',
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    closeSeriesRef.current = closeSeries;

    const volumeSeries = chart.addHistogramSeries({
      color: '#c9cdd4',
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    volumeSeriesRef.current = volumeSeries;
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;

    // ResizeObserver — 自适应容器宽高
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          chart.applyOptions({ width, height });
        }
      }
    });
    ro.observe(containerRef.current);

    // window resize 兜底
    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    // ── crosshair tooltip（仅订阅一次，通过 ref 读最新数据）───
    const tooltip = document.createElement('div');
    tooltip.style.cssText = `
      display: none; position: absolute; z-index: 1000;
      background: #ffffff; color: #111827; padding: 8px 12px;
      border-radius: 8px; font-size: 12px; font-family: "Cascadia Code", Consolas, monospace;
      border: 1px solid #dee5f0; box-shadow: 0 18px 40px -24px rgba(15, 23, 42, .5); pointer-events: none; max-width: 300px;
      line-height: 1.5;
    `;
    containerRef.current.appendChild(tooltip);
    tooltipRef.current = tooltip;

    let layoutFrame = 0;
    const scheduleBacktestMarkerLayout = () => {
      if (layoutFrame !== 0) return;
      layoutFrame = window.requestAnimationFrame(() => {
        layoutFrame = 0;
        requestBacktestMarkerLayoutRef.current();
      });
    };

    chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleBacktestMarkerLayout);
    chart.subscribeCrosshairMove((param) => {
      scheduleBacktestMarkerLayout();
      const d = dataRef.current;
      const mods = modulesRef.current;
      const sds = sidesRef.current;
      const runTrades = backtestTradesRef.current;
      const displayNo = backtestDisplayNoRef.current;
      const strategyCategory = backtestStrategyCategoryRef.current;

      const point = param.point;
      if (!param.time || !point || !d) {
        tooltip.style.display = 'none';
        return;
      }

      if (typeof param.time !== 'number') {
        tooltip.style.display = 'none';
        return;
      }
      const barTime = param.time;
      if (runTrades !== undefined) {
        const hoveredMarker = backtestMarkerLayoutsRef.current.find((marker) => (
          marker.time === barTime
          && Math.hypot(marker.x - point.x, marker.y - point.y) <= marker.radius + 6
        ));
        if (!hoveredMarker) {
          tooltip.style.display = 'none';
          return;
        }
        const relatedTrades = runTrades.filter(
          (trade) => alignTradeToBarTime(trade, d.bars) === barTime
        );
        if (relatedTrades.length === 0) {
          tooltip.style.display = 'none';
          return;
        }
        const title = displayNo ? `<strong>#${displayNo}</strong> ` : '';
        const lines = relatedTrades.map((trade) => {
          const color = getBacktestMarkerColor(trade.action, strategyCategory);
          const note = trade.note ? ` · ${escapeTooltipText(trade.note)}` : '';
          return `<span style="color:${color}">${getBacktestTradeLabel(trade.action, strategyCategory)}</span> ${trade.price.toFixed(2)} ×${trade.quantity}${note}`;
        });
        tooltip.innerHTML = `${title}${lines.join('<br/>')}`;
        tooltip.style.display = 'block';
        const rect = containerRef.current!.getBoundingClientRect();
        tooltip.style.left = `${Math.min(point.x + 15, rect.width - 310)}px`;
        tooltip.style.top = `${Math.max(point.y - 60, 10)}px`;
        return;
      }
      const related = d.markers.filter(
        (m) => toChartTimestamp(m.barTime) === barTime
          && mods.includes(m.module) && sds.includes(m.side)
      );

      if (related.length === 0) {
        tooltip.style.display = 'none';
        return;
      }

      const lines = related.map((m) => {
        const dir = m.side === 'BUY' ? '↑' : '↓';
        const pnlStr = m.pnl != null ? ` PnL=${m.pnl >= 0 ? '+' : ''}${m.pnl.toFixed(2)}` : '';
        const clr = m.module === 'A' ? A_COLOR : B_COLOR;
        return `<span style="color:${clr}">${m.module} ${dir} ${m.action}</span> ${m.price} ×${m.quantity}${pnlStr}`;
      });

      tooltip.innerHTML = lines.join('<br/>');
      tooltip.style.display = 'block';
      const rect = containerRef.current!.getBoundingClientRect();
      tooltip.style.left = `${Math.min(point.x + 15, rect.width - 310)}px`;
      tooltip.style.top = `${Math.max(point.y - 60, 10)}px`;
    });

    return () => {
      window.removeEventListener('resize', handleResize);
      ro.disconnect();
      if (layoutFrame !== 0) window.cancelAnimationFrame(layoutFrame);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(scheduleBacktestMarkerLayout);
      if (tooltip) tooltip.remove();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // ── 更新 K 线数据 ──────────────────────────────────────
  useEffect(() => {
    const candle = candleSeriesRef.current;
    const closeLine = closeSeriesRef.current;
    const volume = volumeSeriesRef.current;
    const chart = chartRef.current;
    if (!candle || !closeLine || !volume || !chart || !data) return;

    const candleData: CandlestickData[] = data.bars.map((b) => ({
      time: toChartTimestamp(b.time) as Time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));

    const volumeData: HistogramData[] = data.bars.map((b) => ({
      time: toChartTimestamp(b.time) as Time,
      value: b.volume,
      color: b.close >= b.open ? '#f2364555' : '#08998155',
    }));

    const closeData: LineData[] = data.bars.map((b) => ({
      time: toChartTimestamp(b.time) as Time,
      value: b.close,
    }));

    candle.setData(candleData);
    closeLine.setData(closeData);
    volume.setData(volumeData);
    chart.timeScale().fitContent();
  }, [data]);

  useEffect(() => {
    updateBacktestMarkerLayouts();
  }, [updateBacktestMarkerLayouts]);

  // ── 更新 marker ────────────────────────────────────────
  const updateMarkers = useCallback(() => {
    const candle = candleSeriesRef.current;
    if (!candle) return;

    const displayMarkers: SeriesMarker<Time>[] = [];

    // ── 实际信号 markers（默认）──
    if (backtestTrades === undefined && signalSource !== 'theoretical' && data?.markers.length) {
      const grouped = new Map<string, TradeMarker[]>();
      for (const m of data.markers) {
        if (!modules.includes(m.module)) continue;
        if (!sides.includes(m.side)) continue;
        const key = `${m.barTime}_${m.module}_${m.side}`;
        if (!grouped.has(key)) grouped.set(key, []);
        grouped.get(key)!.push(m);
      }

      for (const [, markers] of grouped) {
        const m0 = markers[0];
        const isBuy = m0.side === 'BUY';
        const color = m0.module === 'A' ? A_COLOR : B_COLOR;
        const time = toChartTimestamp(m0.barTime) as Time;

        const count = markers.length;
        const label = count >= 3
          ? `${m0.module}×${count}`
          : showPrices
            ? `${m0.module} ${m0.price.toFixed(2)}`
            : m0.module;

        displayMarkers.push({
          time, text: label,
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          color, position: isBuy ? 'belowBar' : 'aboveBar',
          size: scale,
        });
      }
    }

    // ── 理论信号 markers ──
    if (backtestTrades === undefined && signalSource === 'theoretical' && data?.theoretical) {
      for (const s of data.theoretical) {
        if (!modules.includes(s.module)) continue;
        if (s.barIndex >= data.bars.length) continue;
        // 首峰首谷只建立链值，不触发实际交易，跳过
        if (s.type === 'first_peak' || s.type === 'first_valley') continue;
        const time = toChartTimestamp(data.bars[s.barIndex].time) as Time;
        const color = s.module === 'A' ? A_COLOR : B_COLOR;
        const isBuy = s.type.includes('valley') || s.type.includes('buy');
        const isSell = s.type.includes('peak') || s.type.includes('sell');
        const side = isBuy ? 'BUY' : 'SELL';

        if (!sides.includes(side)) continue;

        const typeLabel = s.type === 'sell_peak' ? '卖' : '买';

        displayMarkers.push({
          time,
          text: `${s.module}${typeLabel} ${s.price.toFixed(1)}`,
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          color,
          position: isBuy ? 'belowBar' : 'aboveBar',
          size: scale,
        });
      }
    }

    displayMarkers.sort((a, b) => Number(a.time) - Number(b.time));
    candle.setMarkers(displayMarkers);
  }, [backtestTrades, data, modules, sides, showPrices, scale, signalSource]);

  useEffect(() => {
    updateMarkers();
  }, [updateMarkers]);

  return (
    <div
      style={{ width: '100%', height: '100%', minHeight: '300px', position: 'relative' }}
    >
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      {backtestTrades !== undefined && backtestMarkerLayouts.length > 0 && (
        <svg
          aria-hidden="true"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', zIndex: 4, pointerEvents: 'none' }}
        >
          {backtestMarkerLayouts.map((marker) => (
            <g key={marker.key}>
              <line
                x1={marker.anchorX}
                x2={marker.x}
                y1={marker.anchorY}
                y2={marker.y}
                stroke="#ffffff"
                strokeWidth={1.5}
                strokeDasharray="3 3"
              />
              <circle cx={marker.x} cy={marker.y} r={marker.radius} fill={marker.color} stroke="#ffffff" strokeWidth={1.5} />
              {marker.label && (
                <text
                  x={marker.x}
                  y={marker.y + 3.5}
                  fill="#ffffff"
                  fontSize={marker.label === '平' ? 9 : 11}
                  fontWeight={700}
                  textAnchor="middle"
                >
                  {marker.label}
                </text>
              )}
            </g>
          ))}
        </svg>
      )}
      {label && (
        <div style={{
          position: 'absolute', top: 6, left: 8, zIndex: 5,
          color: labelColor || '#5b6579', fontSize: 12, fontWeight: 'bold',
          background: 'rgba(255,255,255,.88)', border: '1px solid #e0e3eb', padding: '2px 8px', borderRadius: 3,
          pointerEvents: 'none',
        }}>
          {label}
        </div>
      )}
      <div style={{
        position: 'absolute', top: 6, right: 56, zIndex: 5,
        display: 'flex', flexDirection: 'column', gap: 2,
        padding: '3px 6px', borderRadius: 3,
        background: 'rgba(255, 255, 255, .9)', color: '#5b6579', border: '1px solid #e0e3eb',
        fontSize: 10, lineHeight: 1.4, pointerEvents: 'none',
      }}>
        {backtestTrades !== undefined ? (
          <>
            <span style={{ color: '#5b6579' }}>━ 收盘</span>
            <span style={{ color: BACKTEST_BUY_COLOR }}>▲ 买入</span>
            <span style={{ color: BACKTEST_SELL_COLOR }}>▼ 卖出</span>
            {backtestStrategyCategory === 'core_position_overlay' && <span style={{ color: BACKTEST_T_COLOR }}>● T</span>}
          </>
        ) : (
          <>
            <span style={{ color: '#5b6579' }}>━ 收盘</span>
            <span style={{ color: A_COLOR }}>▲▼ A</span>
            <span style={{ color: B_COLOR }}>▲▼ B</span>
          </>
        )}
      </div>
    </div>
  );
}
