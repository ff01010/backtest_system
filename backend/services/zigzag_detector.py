# -*- coding: utf-8 -*-
"""
纯 Python zigzag 拐点检测器 — 对齐 trend_grid_live copy.py 的 _on_bar_close 逻辑。
不依赖 xtquant，只接受 OHLC bar 列表，输出理论买卖信号。
"""

from dataclasses import dataclass, field


@dataclass
class ZigzagSignal:
  bar_index: int       # 信号所属 bar 序号
  module: str          # "A" | "B"
  signal_type: str     # "first_peak" | "sell_peak" | "first_valley" | "buy_valley"
  price: float         # 拐点价格
  chain: float = 0.0   # 当前链值（仅 A 有）


def detect(bars: list[dict]) -> list[dict]:
  """检测 zigzag 拐点，返回理论信号列表。

  bars: [{"time": str, "open": float, "high": float, "low": float, "close": float}, ...]

  返回: [{"barIndex": int, "module": "A"|"B", "type": str, "price": float, "chain": float}, ...]
  """
  n = len(bars)
  if n < 2:
    return []

  signals: list[dict] = []

  # A 状态 — 开盘价作为初始链值
  a_pending_peak = 0.0
  a_pending_valley = 0.0
  a_peak_chain = bars[0]["open"]
  a_valley_chain = bars[0]["open"]

  # B 状态
  b_pending_peak = 0.0
  b_pending_valley = 0.0

  prev_close = bars[0]["close"]

  for i in range(1, n):
    bar_close = bars[i]["close"]
    bar_open = bars[i]["open"]

    # ── 追认上一根的候选 ──
    # A 尖点
    if a_pending_peak > 0 and bar_close < a_pending_peak:
      peak_price = a_pending_peak
      if a_peak_chain > 0 and peak_price < a_peak_chain:
        signals.append({
          "barIndex": i, "module": "A", "type": "sell_peak",
          "price": peak_price, "chain": a_peak_chain,
        })
      elif a_peak_chain == 0:
        signals.append({
          "barIndex": i, "module": "A", "type": "first_peak",
          "price": peak_price, "chain": 0,
        })
      a_peak_chain = peak_price
      a_pending_peak = 0

    # A 谷点
    if a_pending_valley > 0 and bar_close > a_pending_valley:
      valley_price = a_pending_valley
      if a_valley_chain > 0 and valley_price > a_valley_chain:
        signals.append({
          "barIndex": i, "module": "A", "type": "buy_valley",
          "price": valley_price, "chain": a_valley_chain,
        })
      elif a_valley_chain == 0:
        signals.append({
          "barIndex": i, "module": "A", "type": "first_valley",
          "price": valley_price, "chain": 0,
        })
      a_valley_chain = valley_price
      a_pending_valley = 0

    # B 尖点
    if b_pending_peak > 0 and bar_close < b_pending_peak:
      signals.append({
        "barIndex": i, "module": "B", "type": "sell_peak",
        "price": b_pending_peak, "chain": 0,
      })
      b_pending_peak = 0

    # B 谷点
    if b_pending_valley > 0 and bar_close > b_pending_valley:
      signals.append({
        "barIndex": i, "module": "B", "type": "buy_valley",
        "price": b_pending_valley, "chain": 0,
      })
      b_pending_valley = 0

    # ── 登记当前柱为候选 ──
    if prev_close < bar_close:
      a_pending_peak = bar_close
      b_pending_peak = bar_close
    if prev_close > bar_close:
      a_pending_valley = bar_close
      b_pending_valley = bar_close

    prev_close = bar_close

  return signals
