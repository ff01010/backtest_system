from __future__ import annotations

import pytest

from backend.backtesting.models import OrderSide
from backend.backtesting.overlay import OverlayAttributionLedger, OverlayFill, build_overlay_report


def _fill(
  fill_id: str,
  time: str,
  side: OrderSide,
  quantity: int,
  price: float,
  fee: float = 0.0,
) -> OverlayFill:
  return OverlayFill(fill_id, time, "688449.SH", side, quantity, price, fee)


def test_cross_day_partial_restore_keeps_one_cycle_until_target_is_restored() -> None:
  fills = [
    _fill("f1", "2026-01-05 10:00:00", OrderSide.SELL, 120, 12, 1),
    _fill("f2", "2026-01-06 10:00:00", OrderSide.BUY, 60, 10, 1),
    _fill("f3", "2026-01-06 11:00:00", OrderSide.SELL, 30, 13, 1),
    _fill("f4", "2026-01-07 10:00:00", OrderSide.BUY, 80, 11, 1),
    _fill("f5", "2026-01-08 10:00:00", OrderSide.BUY, 10, 12, 1),
  ]
  holdings = [
    {"date": "2026-01-05", "close": 11, "longQty": 880, "shortQty": 0, "totalEquity": 21_119, "dailyPnl": 1_119},
    {"date": "2026-01-06", "close": 12, "longQty": 910, "shortQty": 0, "totalEquity": 22_147, "dailyPnl": 1_028},
    {"date": "2026-01-07", "close": 12.5, "longQty": 990, "shortQty": 0, "totalEquity": 22_721, "dailyPnl": 574},
    {"date": "2026-01-08", "close": 12, "longQty": 1_000, "shortQty": 0, "totalEquity": 22_225, "dailyPnl": -496},
  ]

  report = build_overlay_report(fills, holdings, 10_000, 1_000, 10)

  assert [item["overlayQuantity"] for item in report["daily"]] == [-120, -90, -10, 0]
  assert report["daily"][1]["dailyRealizedOverlayPnl"] == pytest.approx(118.5)
  assert report["daily"][2]["dailyRealizedOverlayPnl"] == pytest.approx(104.33)
  assert report["metrics"] == {
    "realizedOverlayPnl": 225.0,
    "unrealizedOverlayPnl": 0.0,
    "completedCyclePnl": 225.0,
    "completedCycleCount": 1,
    "openCycleCount": 0,
    "winningCycles": 1,
    "losingCycles": 0,
    "breakevenCycles": 0,
    "cycleWinRate": 1.0,
    "avgWin": 225.0,
    "avgLoss": None,
    "profitLossRatio": None,
  }
  assert report["cycles"][0]["fillIds"] == ["f1", "f2", "f3", "f4", "f5"]
  assert report["cycles"][0]["status"] == "completed"


def test_crossing_zero_splits_attribution_but_keeps_one_actual_fill() -> None:
  ledger = OverlayAttributionLedger()
  ledger.apply_fill(_fill("f1", "2026-01-05 10:00:00", OrderSide.BUY, 100, 10, 1))

  attribution = ledger.apply_fill(
    _fill("f2", "2026-01-05 11:00:00", OrderSide.SELL, 150, 12, 1),
  )

  assert attribution["cycleEffect"] == "reverse"
  assert attribution["overlayQuantityBefore"] == 100
  assert attribution["overlayQuantityAfter"] == -50
  assert len(attribution["attributionSegments"]) == 2
  assert attribution["attributionSegments"][0]["cycleEffect"] == "close"
  assert attribution["attributionSegments"][1]["cycleEffect"] == "open"
  assert ledger.cycles(11)[0]["status"] == "completed"
  assert ledger.cycles(11)[1]["status"] == "open"


def test_unfinished_cycle_is_marked_to_market_and_excluded_from_win_rate() -> None:
  report = build_overlay_report(
    [_fill("f1", "2026-01-05 10:00:00", OrderSide.SELL, 100, 12, 1)],
    [{"date": "2026-01-05", "close": 11, "longQty": 900, "shortQty": 0, "totalEquity": 21_099, "dailyPnl": 1_099}],
    10_000,
    1_000,
    10,
  )

  assert report["metrics"]["realizedOverlayPnl"] == 0
  assert report["metrics"]["unrealizedOverlayPnl"] == 99
  assert report["metrics"]["completedCycleCount"] == 0
  assert report["metrics"]["openCycleCount"] == 1
  assert report["metrics"]["cycleWinRate"] == 0


def test_report_rejects_account_and_overlay_position_mismatch() -> None:
  with pytest.raises(ValueError, match="增强归属仓位与账户实际持仓无法对账"):
    build_overlay_report(
      [_fill("f1", "2026-01-05 10:00:00", OrderSide.SELL, 100, 12)],
      [{"date": "2026-01-05", "close": 12, "longQty": 950, "shortQty": 0, "totalEquity": 22_000, "dailyPnl": 2_000}],
      10_000,
      1_000,
      10,
    )
