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
import time
from typing import Any

import pandas as pd

from backend.services import signal_parser, time_mapper, zigzag_detector
from backend import settings


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

  # K 线日期：扫描 {code}_{market}/ 目录
  code, market = symbol.split(".")
  code_market = f"{code}_{market}"
  all_kline_dates: set[str] = set()
  dir_path = os.path.join(settings.KLINE_ROOT, timeframe, code_market)
  if os.path.isdir(dir_path):
    for fname in os.listdir(dir_path):
      if not fname.endswith(".csv") or timeframe not in fname:
        continue
      path = os.path.join(dir_path, fname)
      all_kline_dates |= _read_kline_dates(path)

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
  """返回引擎支持的周期及本地数据覆盖情况。"""
  items: list[dict[str, Any]] = []
  for timeframe in settings.BACKTEST_TIMEFRAMES:
    path = settings.resolve_kline_path(symbol, timeframe)
    if not path:
      items.append({
        "timeframe": timeframe,
        "available": False,
        "reason": f"本地没有 {symbol} 的 {timeframe} K 线文件",
        "dataStart": None,
        "dataEnd": None,
      })
      continue
    dates = sorted(_read_kline_dates(path))
    covered = bool(dates)
    reason = ""
    if covered and start_date and end_date:
      covered = dates[0] <= start_date and dates[-1] >= end_date
      if not covered:
        reason = f"数据范围为 {dates[0]} 至 {dates[-1]}，未覆盖 {start_date} 至 {end_date}"
    elif not covered:
      reason = f"{timeframe} K 线文件没有可识别日期"
    items.append({
      "timeframe": timeframe,
      "available": covered,
      "reason": reason,
      "dataStart": dates[0] if dates else None,
      "dataEnd": dates[-1] if dates else None,
    })
  return items


def validate_backtest_coverage(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> None:
  """在回测前拒绝不受支持、缺文件或日期范围不足的数据。"""
  if not settings.validate_backtest_timeframe(timeframe):
    raise ValueError(f"当前回测引擎不支持 {timeframe}，仅支持 {', '.join(settings.BACKTEST_TIMEFRAMES)}")
  item = next(
    (value for value in get_backtest_timeframes(symbol, start_date, end_date)
     if value["timeframe"] == timeframe),
    None,
  )
  if item is None or not item["available"]:
    reason = item["reason"] if item else "无法读取数据状态"
    raise ValueError(f"{symbol} {timeframe} K 线不可用于回测：{reason}")


def load_chart_data(symbol: str, date: str, timeframe: str, strategy: str = "trend_grid",
                    signal_file: str | None = None) -> dict[str, Any]:
  """加载图表数据: bars + markers + summary

  signal_file: 指定信号文件名（仅限 SIGNAL_ROOT 下的 .csv），为 None 时按策略名自动解析
  """
  warnings: list[str] = []
  bars: list[dict[str, Any]] = []
  markers: list[dict[str, Any]] = []

  # — 加载 K 线 —
  kline_path = settings.resolve_kline_path(symbol, timeframe, date)
  kline_mtime = None
  if not kline_path:
    return {"error": f"K 线数据不存在: {symbol} {timeframe}"}
  kline_mtime = _format_mtime(kline_path)

  try:
    df = pd.read_csv(kline_path)
    # 兼容旧格式 "datetime" 和新格式 "time" 列名
    if "time" in df.columns and "datetime" not in df.columns:
      df.rename(columns={"time": "datetime"}, inplace=True)
    df["datetime"] = df["datetime"].astype(str)
    df = df[df["datetime"].str.startswith(date)]
    df = df.drop_duplicates(subset="datetime", keep="last")
    df = df.sort_values("datetime")

    for _, row in df.iterrows():
      dt = row["datetime"]
      # "20260622093500" → ISO 格式
      ts = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}T{dt[8:10]}:{dt[10:12]}:00"
      bars.append({
        "time": ts,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(row.get("volume", 0)),
      })
  except Exception as e:
    return {"error": f"K 线读取失败: {e}"}

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
    signal_path = settings.resolve_signal_path(date, symbol, strategy)
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
          bar_dt = time_mapper.map_signal_to_bar(parsed["time"], date)
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
  signal_path = settings.resolve_signal_path(date, symbol, strategy)
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
        bar_dt = time_mapper.map_signal_to_bar(t, date)
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
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
  except OSError:
    return None
