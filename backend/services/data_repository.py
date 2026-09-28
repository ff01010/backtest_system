# -*- coding: utf-8 -*-
"""数据仓库 — 扫描标的、加载 K 线和信号 CSV"""

import sys
import os

# 确保 backend 包可导入
_viewer_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

import csv
import re
from typing import Any

import pandas as pd

from backend.services import market_data, signal_parser, time_mapper, zigzag_detector
from backend.services.backtest_faults import BacktestFault
from backend import settings
from backend.date_codec import format_api_date, format_legacy_date
from backend.time_utils import format_epoch_as_shanghai


def list_instruments() -> list[dict[str, Any]]:
  """扫描同时有 K 线和信号数据的标的"""
  kline_root = settings.KLINE_ROOT
  signal_root = settings.SIGNAL_ROOT

  if not os.path.isdir(kline_root):
    return []

  # 从各周期目录收集标的，避免把 "1m"、"5m" 当成标的目录。
  kline_symbols: set[str] = set()
  for timeframe in settings.TIMEFRAMES:
    timeframe_root = os.path.join(kline_root, timeframe)
    if not os.path.isdir(timeframe_root):
      continue
    for entry in os.listdir(timeframe_root):
      path = os.path.join(timeframe_root, entry)
      if not os.path.isdir(path):
        continue
      for sep in ["_", "."]:
        if sep in entry:
          parts = entry.split(sep)
          if len(parts) == 2 and parts[0].isdigit() and parts[1] in ("SH", "SZ"):
            kline_symbols.add(f"{parts[0]}.{parts[1]}")
            break
      else:
        if entry.isdigit() and len(entry) == 6:
          kline_symbols.add(f"{entry}.SH")

  # 从信号目录收集有数据的标的
  signal_symbols: set[str] = set()
  if os.path.isdir(signal_root):
    for fname in os.listdir(signal_root):
      if not fname.startswith("signal_") or not fname.endswith(".csv"):
        continue
      fpath = os.path.join(signal_root, fname)
      try:
        df = pd.read_csv(fpath)
        if "code" in df.columns:
          signal_symbols.update(df["code"].dropna().unique())
      except Exception:
        pass

  # 返回共有标的
  all_symbols = kline_symbols | signal_symbols
  result = []
  for sym in sorted(all_symbols):
    result.append({
      "symbol": sym,
      "hasKline": sym in kline_symbols,
      "hasSignal": sym in signal_symbols,
    })
  return result


def _read_kline_dates(path: str) -> set[str]:
  """从 K 线 CSV 提取日期集合"""
  try:
    df = pd.read_csv(path)
    if "time" in df.columns and "datetime" not in df.columns:
      df.rename(columns={"time": "datetime"}, inplace=True)
    df["datetime"] = df["datetime"].astype(str)
    return set(df["datetime"].str[:8].unique())
  except Exception:
    return set()


def list_dates(symbol: str, timeframe: str) -> list[dict[str, Any]]:
  """返回有 K 线和/或信号的日期列表"""
  dates_map: dict[str, dict[str, Any]] = {}

  # 信号日期（扫描所有策略 + 旧格式）
  signal_root = settings.SIGNAL_ROOT
  signal_dates: set[str] = set()
  if os.path.isdir(signal_root):
    for fname in os.listdir(signal_root):
      if not fname.startswith("signal_") or not fname.endswith(".csv"):
        continue
      # 提取日期（兼容 signal_{date}.csv 和 signal_{strategy}_{date}.csv）
      core = fname.replace("signal_", "").replace(".csv", "")
      parts = core.split("_")
      date_str = parts[-1] if len(parts) >= 2 and len(parts[-1]) == 8 else (core if len(core) == 8 else "")
      if not date_str:
        continue
      fpath = os.path.join(signal_root, fname)
      try:
        df = pd.read_csv(fpath)
        if "code" in df.columns and symbol in df["code"].values:
          signal_dates.add(date_str)
          dates_map.setdefault(date_str, {"date": date_str, "hasKline": False, "hasSignal": False})
          dates_map[date_str]["hasSignal"] = True
      except Exception:
        pass

  # K 线日期从 ClickHouse 的 QMT 覆盖记录读取。
  all_kline_dates = {item["date"] for item in market_data.list_dates(symbol, timeframe)}

  # 合并
  for d in all_kline_dates:
    dates_map.setdefault(d, {"date": d, "hasKline": False, "hasSignal": False})
    dates_map[d]["hasKline"] = True
  for d in signal_dates:
    dates_map.setdefault(d, {"date": d, "hasKline": False, "hasSignal": False})

  return sorted(dates_map.values(), key=lambda x: x["date"], reverse=True)


def get_backtest_timeframes(
  symbol: str,
  start_date: str | None = None,
  end_date: str | None = None,
) -> list[dict[str, Any]]:
  """返回引擎支持周期及 QMT 历史数据覆盖情况。"""
  return market_data.timeframe_status(symbol, start_date, end_date)


def validate_backtest_coverage(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> None:
  """在回测前确认 QMT 历史数据已完整覆盖请求区间。"""
  if not settings.validate_backtest_timeframe(timeframe):
    raise ValueError(f"当前回测引擎不支持 {timeframe}，仅支持 {', '.join(settings.BACKTEST_TIMEFRAMES)}")
  market_data.ensure_cached(symbol, timeframe, start_date, end_date)


def load_chart_bars(symbol: str, date: str, timeframe: str) -> list[dict[str, Any]]:
  """读取单日行情，并转换为前端图表统一时间格式。"""
  try:
    frame = market_data.load_bars(symbol, timeframe, date, date)
    bars: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
      timestamp = str(row["time"])
      bars.append({
        "time": f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}T{timestamp[8:10]}:{timestamp[10:12]}:00",
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(row.get("volume", 0)),
      })
    return bars
  except BacktestFault:
    raise
  except (KeyError, TypeError, ValueError, OverflowError, market_data.MarketDataError) as error:
    raise BacktestFault(
      "data.bar_load_failed",
      "图表 Bar 行情读取失败",
      {"symbol": symbol, "timeframe": timeframe},
    ) from error


def load_chart_data(symbol: str, date: str, timeframe: str, strategy: str = "trend_grid",
                    signal_file: str | None = None) -> dict[str, Any]:
  """加载图表数据: bars + markers + summary

  signal_file: 指定信号文件名（仅限 SIGNAL_ROOT 下的 .csv），为 None 时按策略名自动解析
  """
  warnings: list[str] = []
  markers: list[dict[str, Any]] = []
  date = format_api_date(date)
  legacy_date = format_legacy_date(date)

  # — 从 ClickHouse 加载 K 线 —
  kline_mtime = "database"
  try:
    bars = load_chart_bars(symbol, date, timeframe)
  except ValueError as error:
    return {"error": str(error)}

  # 构建 bar datetime → index 映射
  bar_time_set = {b["time"] for b in bars}

  # — 加载信号 —
  if signal_file:
    # 安全校验：仅允许 SIGNAL_ROOT 下的 .csv，防止路径穿越
    # signal_file 可以是相对路径如 "bar/688981_SH_bar_20260623.csv"
    signal_path = os.path.normpath(os.path.join(settings.SIGNAL_ROOT, signal_file))
    if not signal_path.startswith(os.path.normpath(settings.SIGNAL_ROOT)):
      return {"error": f"不允许的信号文件路径: {signal_file}"}
    if not os.path.isfile(signal_path):
      return {"error": f"信号文件不存在: {signal_file}"}
  else:
    signal_path = settings.resolve_signal_path(legacy_date, symbol, strategy)
  signal_mtime = None
  if os.path.exists(signal_path):
    signal_mtime = _format_mtime(signal_path)
    try:
      with open(signal_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
          if row.get("code", "").strip() != symbol:
            continue

          parsed = signal_parser.parse_signal_row(row)
          if parsed is None:
            continue

          if "_warning" in parsed:
            warnings.append(f"[行{i}] {parsed['_warning']}")
            continue
          if not signal_parser.should_display_marker(parsed):
            continue

          # 时间映射
          bar_dt = time_mapper.map_signal_to_bar(parsed["time"], legacy_date)
          if bar_dt is None:
            continue

          # 构造 bar ISO 时间
          bar_ts = f"{bar_dt[:4]}-{bar_dt[4:6]}-{bar_dt[6:8]}T{bar_dt[8:10]}:{bar_dt[10:12]}:00"
          if bar_ts not in bar_time_set:
            warnings.append(f"[{parsed['time']}] 信号时间 {parsed['time']} 映射到 {bar_ts}，但无对应 K 线")
            continue

          markers.append({
            "id": f"{symbol}_{parsed['module']}_{parsed['side']}_{parsed['action']}_{i}",
            "barTime": bar_ts,
            "sourceTime": parsed["time"],
            "module": parsed["module"],
            "side": parsed["side"],
            "action": parsed["action"],
            "price": parsed["exec_price"],
            "quantity": parsed["quantity"],
            "pnl": parsed["pnl"],
            "detail": parsed["detail"],
          })
    except Exception as e:
      warnings.append(f"信号文件读取失败: {e}")
  else:
    warnings.append(f"信号文件不存在: {signal_path}")

  # — 汇总 —
  a_buy = sum(1 for m in markers if m["module"] == "A" and m["side"] == "BUY")
  a_sell = sum(1 for m in markers if m["module"] == "A" and m["side"] == "SELL")
  b_buy = sum(1 for m in markers if m["module"] == "B" and m["side"] == "BUY")
  b_sell = sum(1 for m in markers if m["module"] == "B" and m["side"] == "SELL")

  # — 重建 zigzag 状态 → per-bar 注解 —
  annotations = _build_annotations(bars, date, symbol, strategy)

  # — 纯算法理论信号（zigzag_detector）—
  theoretical = zigzag_detector.detect(bars)

  return {
    "symbol": symbol,
    "date": date,
    "timeframe": timeframe,
    "bars": bars,
    "markers": markers,
    "annotations": annotations,
    "theoretical": theoretical,
    "summary": {
      "aBuy": a_buy, "aSell": a_sell,
      "bBuy": b_buy, "bSell": b_sell,
      "barCount": len(bars),
      "warnings": warnings,
    },
    "dataFreshness": {
      "klineMtime": kline_mtime,
      "signalMtime": signal_mtime,
    },
  }


def _build_annotations(bars: list[dict], date: str, symbol: str, strategy: str = "trend_grid") -> list[dict]:
  """重建 zigzag 状态，返回每根 bar 的注解数据"""
  n = len(bars)
  if n == 0:
    return []

  ann = [{
    "aPeakCand": False, "aPeakConfirmed": None,
    "aValleyCand": False, "aValleyConfirmed": None,
    "bPeakCand": False, "bPeakConfirmed": None,
    "bValleyCand": False, "bValleyConfirmed": None,
    "aPeakChain": 0.0, "aValleyChain": 0.0,
    "aCloseLineHit": None, "bStopHit": None,
  } for _ in range(n)]

  # prev_close 判定候选
  for i in range(1, n):
    prev_c = bars[i - 1]["close"]
    cur_c = bars[i]["close"]
    if prev_c < cur_c:
      ann[i]["aPeakCand"] = True
      ann[i]["bPeakCand"] = True
    elif prev_c > cur_c:
      ann[i]["aValleyCand"] = True
      ann[i]["bValleyCand"] = True

  # 解析信号 CSV
  bar_time_index = {b["time"]: i for i, b in enumerate(bars)}
  legacy_date = format_legacy_date(date)
  signal_path = settings.resolve_signal_path(legacy_date, symbol, strategy)
  if not os.path.exists(signal_path):
    return ann

  a_peak_chain = 0.0
  a_valley_chain = 0.0
  try:
    with open(signal_path, "r", encoding="utf-8") as f:
      for row in csv.DictReader(f):
        if row.get("code", "").strip() != symbol:
          continue
        t = row["time"]
        bar_dt = time_mapper.map_signal_to_bar(t, legacy_date)
        if not bar_dt:
          continue
        bar_ts = f"{bar_dt[:4]}-{bar_dt[4:6]}-{bar_dt[6:8]}T{bar_dt[8:10]}:{bar_dt[10:12]}:00"
        if bar_ts not in bar_time_index:
          continue
        bi = bar_time_index[bar_ts]
        typ = row["type"]
        mod = row["module"]
        detail = row.get("detail", "")

        if mod == "A":
          if "尖点-首峰" in typ:
            m = re.search(r"peak=([\d.]+)", detail)
            if m:
              ann[bi]["aPeakConfirmed"] = float(m.group(1))
              a_peak_chain = float(m.group(1))
          elif "尖点-卖出" in typ:
            m = re.search(r"peak=([\d.]+)", detail)
            if m:
              ann[bi]["aPeakConfirmed"] = float(m.group(1))
              a_peak_chain = float(m.group(1))
          elif "谷点-首谷" in typ:
            m = re.search(r"valley=([\d.]+)", detail)
            if m:
              ann[bi]["aValleyConfirmed"] = float(m.group(1))
              a_valley_chain = float(m.group(1))
          elif "谷点-买入" in typ:
            m = re.search(r"valley=([\d.]+)", detail)
            if m:
              ann[bi]["aValleyConfirmed"] = float(m.group(1))
              a_valley_chain = float(m.group(1))
          elif "平仓成交" in typ:
            ann[bi]["aCloseLineHit"] = typ

        elif mod == "B":
          if "尖点-卖出" in typ or "尖点-首峰" in typ:
            m = re.search(r"peak=([\d.]+)", detail)
            if m:
              ann[bi]["bPeakConfirmed"] = float(m.group(1))
          elif "谷点-买入" in typ or "谷点-首谷" in typ:
            m = re.search(r"valley=([\d.]+)", detail)
            if m:
              ann[bi]["bValleyConfirmed"] = float(m.group(1))
          elif "止损平" in typ:
            ann[bi]["bStopHit"] = typ

        ann[bi]["aPeakChain"] = a_peak_chain
        ann[bi]["aValleyChain"] = a_valley_chain

    # 前向填充链值
    for i in range(1, n):
      if ann[i]["aPeakChain"] == 0:
        ann[i]["aPeakChain"] = ann[i - 1]["aPeakChain"]
      if ann[i]["aValleyChain"] == 0:
        ann[i]["aValleyChain"] = ann[i - 1]["aValleyChain"]
  except Exception:
    pass

  return ann


def _format_mtime(filepath: str) -> str | None:
  """返回文件的 ISO 修改时间"""
  try:
    mtime = os.path.getmtime(filepath)
    return format_epoch_as_shanghai(mtime).replace(" ", "T")
  except OSError:
    return None
