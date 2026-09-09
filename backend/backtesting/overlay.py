"""底仓增强成交归属、周期和收益对账。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.backtesting.models import OrderSide


_EPSILON = 1e-8


class ResultReconciliationError(ValueError):
  """结果收益无法与账户事实对账。"""

  def __init__(self, message: str, trade_date: str | None = None) -> None:
    super().__init__(message)
    self.trade_date = trade_date


@dataclass(frozen=True)
class OverlayFill:
  """进入增强归属账的标准化实际成交。"""

  fill_id: str
  time: str
  symbol: str
  side: OrderSide
  quantity: int
  price: float
  fee: float
  account_realized_pnl: float = 0.0


@dataclass
class _OpenCycle:
  cycle_id: str
  symbol: str
  direction: str
  opened_at: str
  quantity: int = 0
  entry_value: float = 0.0
  opened_quantity: int = 0
  closed_quantity: int = 0
  realized_pnl: float = 0.0
  fill_ids: list[str] = field(default_factory=list)


class OverlayAttributionLedger:
  """以目标底仓偏离量解释成交，不修改账户事实。"""

  def __init__(self) -> None:
    self._quantity = 0
    self._cycle_sequence = 0
    self._open_cycle: _OpenCycle | None = None
    self._completed_cycles: list[dict[str, Any]] = []
    self._realized_pnl = 0.0

  @property
  def quantity(self) -> int:
    return self._quantity

  @property
  def realized_pnl(self) -> float:
    return self._realized_pnl

  def apply_fill(self, fill: OverlayFill) -> dict[str, Any]:
    """应用一条实际成交，返回该成交的确定性归属说明。"""
    delta = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
    before = self._quantity
    close_quantity = min(abs(before), fill.quantity) if before * delta < 0 else 0
    open_quantity = fill.quantity - close_quantity
    close_fee = self._allocated_fee(fill.fee, close_quantity, fill.quantity)
    open_fee = fill.fee - close_fee
    segments: list[dict[str, Any]] = []
    realized = 0.0

    if close_quantity:
      segment, realized = self._close(fill, close_quantity, close_fee)
      segments.append(segment)
    if open_quantity:
      segments.append(self._open(fill, open_quantity, open_fee))

    after = self._quantity
    effect = "reverse" if len(segments) == 2 else segments[0]["cycleEffect"]
    return {
      "fillId": fill.fill_id,
      "inventorySource": "base_delivery" if fill.side == OrderSide.SELL else "overlay_inventory",
      "cycleId": segments[-1]["cycleId"] if segments else None,
      "cycleEffect": effect,
      "overlayQuantityBefore": before,
      "overlayQuantityAfter": after,
      "accountRealizedPnl": round(fill.account_realized_pnl, 8),
      "realizedOverlayPnl": round(realized, 8),
      "totalFee": round(fill.fee, 8),
      "attributionSegments": segments,
    }

  def unrealized_pnl(self, mark_price: float) -> float:
    cycle = self._open_cycle
    if cycle is None or cycle.quantity == 0:
      return 0.0
    market_value = cycle.quantity * mark_price
    if cycle.direction == "long":
      return market_value - cycle.entry_value
    return cycle.entry_value - market_value

  def cycles(self, mark_price: float) -> list[dict[str, Any]]:
    items = [dict(item) for item in self._completed_cycles]
    cycle = self._open_cycle
    if cycle is not None:
      unrealized = self.unrealized_pnl(mark_price)
      items.append(self._cycle_record(cycle, None, unrealized, "open"))
    return items

  def _open(self, fill: OverlayFill, quantity: int, fee: float) -> dict[str, Any]:
    direction = "long" if fill.side == OrderSide.BUY else "short"
    if self._open_cycle is None:
      self._cycle_sequence += 1
      self._open_cycle = _OpenCycle(
        cycle_id=f"overlay-{self._cycle_sequence}",
        symbol=fill.symbol,
        direction=direction,
        opened_at=fill.time,
      )
      effect = "open"
    else:
      if self._open_cycle.direction != direction:
        raise ValueError("增强仓跨零拆分失败")
      effect = "increase"
    cycle = self._open_cycle
    value = fill.price * quantity + fee if direction == "long" else fill.price * quantity - fee
    cycle.quantity += quantity
    cycle.entry_value += value
    cycle.opened_quantity += quantity
    self._quantity += quantity if direction == "long" else -quantity
    self._append_fill_id(cycle, fill.fill_id)
    return self._segment(cycle.cycle_id, effect, quantity, fee, 0.0)

  def _close(self, fill: OverlayFill, quantity: int, fee: float) -> tuple[dict[str, Any], float]:
    cycle = self._open_cycle
    if cycle is None or quantity > cycle.quantity:
      raise ValueError("增强仓恢复数量超过未完成周期")
    entry_value = cycle.entry_value * quantity / cycle.quantity
    if cycle.direction == "long":
      realized = fill.price * quantity - fee - entry_value
      self._quantity -= quantity
    else:
      realized = entry_value - fill.price * quantity - fee
      self._quantity += quantity
    cycle.quantity -= quantity
    cycle.entry_value -= entry_value
    cycle.closed_quantity += quantity
    cycle.realized_pnl += realized
    self._realized_pnl += realized
    self._append_fill_id(cycle, fill.fill_id)
    effect = "close" if cycle.quantity == 0 else "partial_close"
    cycle_id = cycle.cycle_id
    if cycle.quantity == 0:
      cycle.entry_value = 0.0
      self._completed_cycles.append(self._cycle_record(cycle, fill.time, 0.0, "completed"))
      self._open_cycle = None
    return self._segment(cycle_id, effect, quantity, fee, realized), realized

  @staticmethod
  def _allocated_fee(total_fee: float, quantity: int, total_quantity: int) -> float:
    if quantity <= 0:
      return 0.0
    if quantity >= total_quantity:
      return total_fee
    return total_fee * quantity / total_quantity

  @staticmethod
  def _append_fill_id(cycle: _OpenCycle, fill_id: str) -> None:
    if not cycle.fill_ids or cycle.fill_ids[-1] != fill_id:
      cycle.fill_ids.append(fill_id)

  @staticmethod
  def _segment(
    cycle_id: str,
    effect: str,
    quantity: int,
    fee: float,
    realized_pnl: float,
  ) -> dict[str, Any]:
    return {
      "cycleId": cycle_id,
      "cycleEffect": effect,
      "quantity": quantity,
      "fee": round(fee, 8),
      "realizedOverlayPnl": round(realized_pnl, 8),
    }

  @staticmethod
  def _cycle_record(
    cycle: _OpenCycle,
    closed_at: str | None,
    unrealized_pnl: float,
    status: str,
  ) -> dict[str, Any]:
    return {
      "cycleId": cycle.cycle_id,
      "symbol": cycle.symbol,
      "direction": cycle.direction,
      "openedAt": cycle.opened_at,
      "closedAt": closed_at,
      "openedQuantity": cycle.opened_quantity,
      "closedQuantity": cycle.closed_quantity,
      "remainingQuantity": cycle.quantity,
      "realizedPnl": round(cycle.realized_pnl, 8),
      "unrealizedPnl": round(unrealized_pnl, 8),
      "netPnl": round(cycle.realized_pnl + unrealized_pnl, 8),
      "fillIds": list(cycle.fill_ids),
      "status": status,
    }


def build_overlay_report(
  fills: Iterable[OverlayFill],
  daily_holdings: list[dict[str, Any]],
  initial_cash: float,
  target_base_quantity: int,
  base_cost_price: float,
) -> dict[str, Any]:
  """生成成交归属、每日增强收益、周期与摘要指标。"""
  ordered_fills = sorted(enumerate(fills), key=lambda item: (_time_key(item[1].time), item[0]))
  ledger = OverlayAttributionLedger()
  attributions: dict[str, dict[str, Any]] = {}
  fill_index = 0
  previous_base_pnl = 0.0
  previous_overlay_pnl = 0.0
  daily: list[dict[str, Any]] = []

  for holding in daily_holdings:
    date_key = _date_key(holding.get("date"))
    realized_before = ledger.realized_pnl
    while fill_index < len(ordered_fills) and _date_key(ordered_fills[fill_index][1].time) <= date_key:
      fill = ordered_fills[fill_index][1]
      attributions[fill.fill_id] = ledger.apply_fill(fill)
      fill_index += 1
    close = _number(holding.get("close"))
    actual_quantity = int(holding.get("longQty", 0)) - int(holding.get("shortQty", 0))
    expected_overlay = actual_quantity - target_base_quantity
    if ledger.quantity != expected_overlay:
      raise ValueError("增强归属仓位与账户实际持仓无法对账")
    initial_equity = initial_cash + target_base_quantity * base_cost_price
    account_pnl = _number(holding.get("totalEquity")) - initial_equity
    base_pnl = target_base_quantity * (close - base_cost_price)
    overlay_pnl = account_pnl - base_pnl
    unrealized = ledger.unrealized_pnl(close)
    if abs(overlay_pnl - ledger.realized_pnl - unrealized) > 0.05:
      trade_date = str(holding.get("date") or "") or None
      raise ResultReconciliationError("增强收益与账户收益无法对账", trade_date)
    daily.append({
      "targetBaseQuantity": target_base_quantity,
      "actualQuantity": actual_quantity,
      "overlayQuantity": ledger.quantity,
      "dailyAccountPnl": round(_number(holding.get("dailyPnl")), 2),
      "dailyBaseBenchmarkPnl": round(base_pnl - previous_base_pnl, 2),
      "dailyOverlayPnl": round(overlay_pnl - previous_overlay_pnl, 2),
      "dailyRealizedOverlayPnl": round(ledger.realized_pnl - realized_before, 2),
      "unrealizedOverlayPnl": round(unrealized, 2),
    })
    previous_base_pnl = base_pnl
    previous_overlay_pnl = overlay_pnl

  if fill_index != len(ordered_fills):
    raise ValueError("存在无法归属到交易日的增强成交")
  final_close = _number(daily_holdings[-1].get("close")) if daily_holdings else base_cost_price
  cycles = ledger.cycles(final_close)
  completed = [item for item in cycles if item["status"] == "completed"]
  wins = [item for item in completed if item["netPnl"] > _EPSILON]
  losses = [item for item in completed if item["netPnl"] < -_EPSILON]
  breakeven = len(completed) - len(wins) - len(losses)
  win_pnl = [float(item["netPnl"]) for item in wins]
  loss_pnl = [float(item["netPnl"]) for item in losses]
  unrealized = ledger.unrealized_pnl(final_close)
  return {
    "attributions": attributions,
    "daily": daily,
    "cycles": cycles,
    "metrics": {
      "realizedOverlayPnl": round(ledger.realized_pnl, 2),
      "unrealizedOverlayPnl": round(unrealized, 2),
      "completedCyclePnl": round(sum(item["netPnl"] for item in completed), 2),
      "completedCycleCount": len(completed),
      "openCycleCount": len(cycles) - len(completed),
      "winningCycles": len(wins),
      "losingCycles": len(losses),
      "breakevenCycles": breakeven,
      "cycleWinRate": round(len(wins) / (len(wins) + len(losses)), 4) if wins or losses else 0.0,
      "avgWin": round(sum(win_pnl) / len(win_pnl), 2) if win_pnl else None,
      "avgLoss": round(sum(loss_pnl) / len(loss_pnl), 2) if loss_pnl else None,
      "profitLossRatio": round(abs(sum(win_pnl) / sum(loss_pnl)), 4) if win_pnl and loss_pnl else None,
    },
  }


def _number(value: object, fallback: float = 0.0) -> float:
  try:
    return float(value)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return fallback


def _date_key(value: object) -> str:
  digits = "".join(char for char in str(value or "") if char.isdigit())
  return digits[:8]


def _time_key(value: object) -> str:
  digits = "".join(char for char in str(value or "") if char.isdigit())
  return digits[:14].ljust(14, "0")
