import { useEffect, useRef, useCallback } from 'react';
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
import type { ChartResponse, TradeMarker, DisplayModule, DisplaySide } from '../types';
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
}

// markerSize → lightweight-charts 原生 size 值 (Circle/Arrow 尺寸基数)
const MARKER_SIZE_MAP = { small: 1, normal: 2, large: 3 };

const A_COLOR = '#FF6B6B';
const B_COLOR = '#0f766e';

export default function KlineChart({ data, modules, sides, showPrices, markerSize, signalSource, label, labelColor }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const closeSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);

  // 用 ref 存储最新值，避免 crosshair handler 因闭包过期而反复订阅
  const dataRef = useRef(data);
  dataRef.current = data;
  const modulesRef = useRef(modules);
  modulesRef.current = modules;
  const sidesRef = useRef(sides);
  sidesRef.current = sides;

  const scale = MARKER_SIZE_MAP[markerSize];

  // ── 初始化图表（仅一次）─────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#64748b',
      },
      grid: {
        vertLines: { color: '#edf2f5' },
        horzLines: { color: '#edf2f5' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: {
        borderColor: '#d7e0e8',
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
        borderColor: '#d7e0e8',
        timeVisible: true,
        secondsVisible: false,
        barSpacing: 12,
      },
      width: containerRef.current.clientWidth,
      height: Math.max(containerRef.current.clientHeight, 300),
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#ef4444',
      downColor: '#22c55e',
      borderUpColor: '#ef4444',
      borderDownColor: '#22c55e',
      wickUpColor: '#ef4444',
      wickDownColor: '#22c55e',
    });
    candleSeriesRef.current = candleSeries;

    const closeSeries = chart.addLineSeries({
      color: '#40556a',
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    closeSeriesRef.current = closeSeries;

    const volumeSeries = chart.addHistogramSeries({
      color: '#c7d5e0',
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
      background: #ffffff; color: #14263a; padding: 8px 12px;
      border-radius: 6px; font-size: 12px; font-family: Consolas, monospace;
      border: 1px solid #d7e0e8; box-shadow: 0 8px 22px rgba(26, 53, 77, .12); pointer-events: none; max-width: 300px;
      line-height: 1.5;
    `;
    containerRef.current.appendChild(tooltip);
    tooltipRef.current = tooltip;

    chart.subscribeCrosshairMove((param) => {
      const d = dataRef.current;
      const mods = modulesRef.current;
      const sds = sidesRef.current;

      if (!param.time || !param.point || !d) {
        tooltip.style.display = 'none';
        return;
      }

      if (typeof param.time !== 'number') {
        tooltip.style.display = 'none';
        return;
      }
      const barTime = param.time;
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
      tooltip.style.left = `${Math.min(param.point.x + 15, rect.width - 310)}px`;
      tooltip.style.top = `${Math.max(param.point.y - 60, 10)}px`;
    });

    return () => {
      window.removeEventListener('resize', handleResize);
      ro.disconnect();
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
      color: b.close >= b.open ? '#ef444466' : '#22c55e66',
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

  // ── 更新 marker ────────────────────────────────────────
  const updateMarkers = useCallback(() => {
    const candle = candleSeriesRef.current;
    if (!candle) return;

    const displayMarkers: SeriesMarker<Time>[] = [];

    // ── 实际信号 markers（默认）──
    if (signalSource !== 'theoretical' && data?.markers.length) {
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
    if (signalSource === 'theoretical' && data?.theoretical) {
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
  }, [data, modules, sides, showPrices, scale]);

  useEffect(() => {
    updateMarkers();
  }, [updateMarkers]);

  return (
    <div
      style={{ width: '100%', height: '100%', minHeight: '300px', position: 'relative' }}
    >
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      {label && (
        <div style={{
          position: 'absolute', top: 6, left: 8, zIndex: 5,
          color: labelColor || '#64748b', fontSize: 12, fontWeight: 'bold',
          background: 'rgba(255,255,255,.88)', border: '1px solid #d7e0e8', padding: '2px 8px', borderRadius: 3,
          pointerEvents: 'none',
        }}>
          {label}
        </div>
      )}
      <div style={{
        position: 'absolute', top: 6, right: 56, zIndex: 5,
        display: 'flex', flexDirection: 'column', gap: 2,
        padding: '3px 6px', borderRadius: 3,
        background: 'rgba(255, 255, 255, .9)', color: '#64748b', border: '1px solid #d7e0e8',
        fontSize: 10, lineHeight: 1.4, pointerEvents: 'none',
      }}>
        <span style={{ color: '#40556a' }}>━ 收盘</span>
        <span style={{ color: A_COLOR }}>▲▼ A</span>
        <span style={{ color: B_COLOR }}>▲▼ B</span>
      </div>
    </div>
  );
}
