from __future__ import annotations

import gzip
import json
import threading
from dataclasses import asdict, replace
from typing import Any

import pytest

from backend.backtesting.models import Bar, OrderIntent, OrderSide, PortfolioSnapshot, PositionEffect
from backend.l2.engine import L2ExecutionEngine
from backend.l2.models import InstrumentRules, NormalizedBar, QuoteLevel, QuoteSnapshot, SourceMappingProfile, TradeEvent, parse_timestamp
from backend.l2.config import load_temporary_quote_profile
from backend.l2.providers import ClickHouseL2Adapter, L2DataCoverageError, L2DataError, TemporaryQuoteBarProvider, VersionedBarCache
from backend.l2.result_adapter import normalize_l2_result
from backend.l2.strategy_session import L2StrategySession, _redact_diagnostic_text
from backend import settings
from backend.l2_runner import L2Runner, _JobLeaseKeeper, _continuous_stream_chunks, _day_chunks, _expected_chunk_count, _new_performance, _quote_mode, _replay_day, _validate_quote_coverage
from backend.services.backtest_errors import RemoteBacktestError, error_detail
from backend.services.system_error_catalog import classify_system_error
from backend.services import clickhouse_client
from backend.worker_app import (
  L2WorkerSession,
  L2StreamDecoder,
  WorkerL2BarsRequest,
  WorkerL2ChunkRequest,
  WorkerL2DayRequest,
  WorkerL2QuoteRequest,
  WorkerL2SessionCreateRequest,
)


class BuyStrategy:
  def __init__(self, count: int = 1, limit_price: float | None = None, quantity: int = 100) -> None:
    self._count = count
    self._limit_price = limit_price
    self._quantity = quantity

  def on_bar(self, bar, portfolio):
    return [OrderIntent(
      symbol=bar.symbol,
      side=OrderSide.BUY,
      quantity=self._quantity,
      reason="测试订单",
      limit_price=self._limit_price,
      position_effect=PositionEffect.OPEN_LONG,
    ) for _ in range(self._count)]


class EventRecordingBuyStrategy(BuyStrategy):
  def __init__(self, quantity: int = 100) -> None:
    super().__init__(quantity=quantity)
    self.order_events: list[dict[str, Any]] = []

  def on_order_event(self, event: dict[str, Any]) -> None:
    self.order_events.append(event)


class SellBaseOnceStrategy:
  def __init__(self, quantity: int = 100) -> None:
    self._quantity = quantity
    self._submitted = False

  def on_bar(self, bar: Bar, portfolio: PortfolioSnapshot) -> list[OrderIntent]:
    if self._submitted:
      return []
    self._submitted = True
    return [OrderIntent(
      symbol=bar.symbol,
      side=OrderSide.SELL,
      quantity=self._quantity,
      reason="卖出底仓做增强",
      position_effect=PositionEffect.OPEN_SHORT,
    )]


def _bar() -> NormalizedBar:
  return NormalizedBar(
    symbol="688981.SH", trade_date="20260102", period="1m",
    start_time="2026-01-02 09:30:00", end_time="2026-01-02 09:31:00",
    open=10, high=10.2, low=9.9, close=10, volume=1000, amount=10_000,
    quality="complete", source_version="v1",
  )


def _quote(time: str, ask_volume: int = 100) -> QuoteSnapshot:
  bids = tuple(QuoteLevel(9.9 - index * 0.01, 1000) for index in range(10))
  asks = (QuoteLevel(10, ask_volume),) + tuple(QuoteLevel(10.1 + index * 0.01, 0) for index in range(9))
  return QuoteSnapshot("688981.SH", "20260102", time, int(time[-2:]), bids, asks, 10, "v1")


def test_l2_order_uses_strictly_later_quote() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_chunk([_bar()], [_quote("2026-01-02 09:31:00"), _quote("2026-01-02 09:31:03")], [])

  fills = engine.result()["fills"]

  assert len(fills) == 1
  assert fills[0]["time"] == "2026-01-02 09:31:03"
  assert fills[0]["quantity"] == 100


def test_l2_incremental_processing_only_needs_quote_after_order() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")

  engine.process_bar(_bar())

  assert engine.has_pending_orders()
  assert engine.next_quote_after() == "2026-01-02 09:31:00"
  assert engine.result()["equity"] == []

  engine.process_quote(_quote("2026-01-02 09:31:03"))
  engine.finish_day("20260102")

  assert not engine.has_pending_orders()
  assert len(engine.result()["equity"]) == 1
  assert engine.result()["fills"][0]["time"] == "2026-01-02 09:31:03"


def test_l2_snapshot_depth_is_shared_by_orders() -> None:
  engine = L2ExecutionEngine(BuyStrategy(count=2), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_chunk([_bar()], [_quote("2026-01-02 09:31:03")], [])

  fills = engine.result()["fills"]

  assert sum(item["quantity"] for item in fills) == 100


def test_l2_open_long_uses_affordable_lot_quantity_and_records_metadata() -> None:
  engine = L2ExecutionEngine(BuyStrategy(quantity=1_200), 1_999, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03", ask_volume=1_200))
  engine.finish_day("20260102")

  result = engine.result()
  fill = result["fills"][0]
  cancelled = next(item for item in result["orders"] if item["type"] == "cancelled")
  diagnostic = next(item for item in result["orders"] if item["type"] == "funds_insufficient")

  assert fill["quantity"] == 100
  assert fill["metadata"]["fundingLimited"] is True
  assert fill["metadata"]["affordableQuantity"] == 100
  assert fill["metadata"]["availableCashBefore"] == 1_999
  assert diagnostic["remaining"] == 1_100
  assert cancelled["reason"] == "日终撤单；期间受可用资金限制"


def test_l2_funds_insufficient_keeps_order_pending_without_strategy_callback() -> None:
  strategy = EventRecordingBuyStrategy()
  engine = L2ExecutionEngine(strategy, 999, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03"))

  result = engine.result()
  diagnostic = next(item for item in result["orders"] if item["type"] == "funds_insufficient")

  assert result["fills"] == []
  assert diagnostic["remaining"] == 100
  assert diagnostic["reason"] == "可用资金不足，暂无法成交"
  assert [item["type"] for item in strategy.order_events] == ["accepted"]
  assert engine._orders[0].status == "pending"


def test_l2_unaffordable_order_does_not_consume_depth_budget() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 999, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  quote = _quote("2026-01-02 09:31:03")
  budget = engine._depth_budget(quote)

  engine._fill_against_depth(engine._orders[0], quote, budget)

  assert budget[("ask", 0)] == 100


def test_l2_depth_odd_lot_can_fill_when_cash_covers_full_depth() -> None:
  engine = L2ExecutionEngine(BuyStrategy(quantity=200), 1_371, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03", ask_volume=137))

  assert engine.result()["fills"][0]["quantity"] == 137


def test_l2_allows_one_sided_limit_up_quote_for_long_day_end_close() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03"))
  limit_up_quote = QuoteSnapshot(
    "688981.SH", "20260102", "2026-01-02 14:56:57", 1,
    (QuoteLevel(11, 1_000),), (), 11, "v1",
  )
  engine.process_quote(limit_up_quote)
  engine.finish_day("20260102")

  fills = engine.result()["fills"]

  assert [(item["side"], item["price"]) for item in fills] == [("BUY", 10), ("SELL", 11)]
  assert engine.result()["warnings"] == []


def test_l2_keeps_position_open_when_limit_down_quote_has_no_bid() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03"))
  limit_down_quote = QuoteSnapshot(
    "688981.SH", "20260102", "2026-01-02 14:56:57", 1,
    (), (QuoteLevel(9, 1_000),), 9, "v1",
  )
  engine.process_quote(limit_down_quote)
  engine.finish_day("20260102")

  fills = engine.result()["fills"]

  assert len(fills) == 1
  assert any("尾盘强平剩余 100 股未成交" in warning for warning in engine.result()["warnings"])


def test_l2_result_adapter_builds_overview_and_signal_details() -> None:
  raw_result = {
    "fills": [
      {"time": "2026-01-02 09:31:03", "side": "BUY", "quantity": 100, "price": 10,
       "realized_pnl": 0, "reason": "开多", "position_effect": "open_long",
       "metadata": {"direction": "BUY_OPEN", "signal_time": "20260102093100", "price_source": "十档盘口主动成交"}},
      {"time": "2026-01-03 09:31:03", "side": "SELL", "quantity": 100, "price": 11,
       "realized_pnl": 100, "reason": "平多", "position_effect": "close_long",
       "metadata": {"direction": "SELL_CLOSE", "signal_time": "20260103093100", "price_source": "十档盘口主动成交"}},
    ],
    "equity": [
      {"time": "2026-01-02 14:57:00", "equity": 900, "cash": 0, "close": 9},
      {"time": "2026-01-03 14:57:00", "equity": 1100, "cash": 1100, "close": 11},
    ],
    "orders": [
      {"type": "accepted", "orderId": "order-1", "signalTime": "20260103094000", "side": "BUY",
       "direction": "BUY_OPEN", "action": "BUY_OPEN", "quantity": 100, "reason": "未成交限价"},
      {"type": "cancelled", "orderId": "order-1", "remaining": 100, "reason": "日终撤单"},
      {"type": "rejected", "signalTime": "20260103095000", "side": "SELL", "direction": "SELL_OPEN",
       "action": "SELL_OPEN", "quantity": 100, "reason": "卖空", "rejectionReason": "标的不允许卖空"},
    ],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {"initialCapital": 1000, "holdingPolicy": "intraday"})

  assert result["summary"]["totalReturn"] == 0.1
  assert result["summary"]["maxDrawdownRate"] == 0.1
  assert result["summary"]["tradeCount"] == 2
  assert result["summary"]["orderCount"] == 1
  assert result["summary"]["fillCount"] == 2
  assert result["trades"][0]["action"] == "BUY_OPEN"
  assert result["trades"][0].get("fundingLimited") is None
  assert result["dailyHoldings"][0]["longQty"] == 100
  assert len(result["skippedSignals"]) == 2
  assert "日终撤单" in result["skippedSignals"][0]["note"]
  assert "标的不允许卖空" in result["skippedSignals"][1]["note"]


def test_l2_result_adapter_exposes_funding_limit_detail() -> None:
  raw_result = {
    "fills": [{
      "time": "2026-01-02 09:31:03", "side": "BUY", "quantity": 100, "price": 10,
      "reason": "开多", "position_effect": "open_long",
      "metadata": {
        "fundingLimited": True, "depthQuantity": 1_200, "affordableQuantity": 100,
        "remainingQuantity": 1_100, "availableCashBefore": 1_999,
      },
    }],
    "equity": [], "orders": [], "warnings": [],
  }

  result = normalize_l2_result(raw_result, {"initialCapital": 1_999})

  assert result["trades"][0]["fundingLimited"] is True
  assert result["trades"][0]["affordableQuantity"] == 100
  assert result["trades"][0]["availableCashBefore"] == 1_999


def test_l2_order_events_keep_signal_fields_for_unfilled_orders() -> None:
  engine = L2ExecutionEngine(BuyStrategy(limit_price=9.5), 100_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_chunk([_bar()], [_quote("2026-01-02 09:31:03")], [])
  engine.finish_day("20260102")

  events = engine.result()["orders"]
  accepted = next(item for item in events if item["type"] == "accepted")
  cancelled = next(item for item in events if item["type"] == "cancelled")

  assert accepted["direction"] == "BUY"
  assert accepted["quantity"] == 100
  assert accepted["reason"] == "测试订单"
  assert cancelled["remaining"] == 100


def test_l2_passive_limit_uses_trade_confirmation() -> None:
  engine = L2ExecutionEngine(BuyStrategy(limit_price=9.5), 100_000, 0, InstrumentRules(), passive_participation_rate=0.1)
  engine.begin_day("20260102")
  engine.process_chunk([_bar()], [_quote("2026-01-02 09:31:03")], [
    TradeEvent("688981.SH", "20260102", "2026-01-02 09:31:03", 1, "1", 9.5, 100, "S", "v1"),
  ])

  fills = engine.result()["fills"]

  assert len(fills) == 1
  assert fills[0]["quantity"] == 10
  assert fills[0]["metadata"]["price_source"] == "逐笔保守成交确认"


class CacheProbe:
  def __init__(self) -> None:
    self.calls: list[tuple[str, str]] = []

  def get(self, key: str):
    self.calls.append(("get", key))
    return None

  def set(self, key: str, value, ttl_seconds: int) -> None:
    self.calls.append(("set", key))


def test_unknown_cache_version_does_not_use_redis_cache() -> None:
  probe = CacheProbe()
  cache = VersionedBarCache(probe, 60)

  cache.set(None, "mapping-v1", "688981.SH", "20260102", "2m", [_bar()])
  result = cache.get(None, "mapping-v1", "688981.SH", "20260102", "2m")

  assert result is None
  assert probe.calls == []


def test_temporary_bar_marks_redis_unavailable_in_cache_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
  class FailingCache:
    def get(self, key: str) -> None:
      raise L2DataError("Redis 缓存不可用: Authentication required")

    def set(self, key: str, value: dict[str, object], ttl_seconds: int) -> None:
      raise L2DataError("Redis 缓存不可用: Authentication required")

  provider = TemporaryQuoteBarProvider(
    load_temporary_quote_profile(),
    VersionedBarCache(FailingCache(), 60),
    "tick_quote_session_v4",
    "tick_quote_depth_v3",
  )
  monkeypatch.setattr(provider, "_aggregate_day", lambda *args: [_bar()])

  provider._load_day("688981.SH", "2026-01-02", "1m")

  day = provider.cache_snapshot()["days"][0]
  assert day["cacheStatus"] == "miss_unavailable"


def test_temporary_quote_mapping_uses_actual_depth_volume_columns() -> None:
  profile = load_temporary_quote_profile()

  assert profile.table_name == "quant.tick_quote"
  assert profile.columns["bid_volume_1"] == "bid_vol_1"
  assert profile.columns["ask_volume_10"] == "ask_vol_10"


def test_quote_coverage_uses_actual_bar_dates(monkeypatch: pytest.MonkeyPatch) -> None:
  profile = load_temporary_quote_profile()
  adapter = ClickHouseL2Adapter(profile)
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, str]]:
    queries.append(query)
    if query.startswith("DESCRIBE TABLE"):
      return [{"name": column} for column in profile.columns.values()]
    return [{"trade_date": "2026-01-02"}]

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)

  missing_dates = adapter.missing_quote_dates("688981.SH", ["2026-01-02", "2026-01-03"])

  assert missing_dates == ["2026-01-03"]
  assert "GROUP BY trade_date" in queries[-1]
  assert "09:30:00" in queries[-1]
  assert "14:57:00" in queries[-1]


def test_l2_quote_coverage_fails_before_strategy_session() -> None:
  class CoverageProbe:
    def missing_quote_dates(self, symbol: str, dates: list[str]) -> list[str]:
      assert symbol == "688981.SH"
      assert dates == ["20260102"]
      return ["20260102"]

  with pytest.raises(L2DataCoverageError, match="20260102"):
    _validate_quote_coverage(CoverageProbe(), "688981.SH", [_bar()])  # type: ignore[arg-type]


def test_l2_quote_coverage_error_has_safe_data_message() -> None:
  error = L2DataCoverageError("688126.SH", ["2025-04-10"])
  detail = error_detail(error)
  classification = classify_system_error(error)

  assert detail.category == "data"
  assert detail.code == "l2.quote_coverage_missing"
  assert detail.retryable is False
  assert detail.message == "当前日期范围缺少 L2 十档快照。"
  assert classification.category == "data"
  assert classification.retryable is False


def test_temporary_quote_profile_scales_only_depth_prices() -> None:
  profile = load_temporary_quote_profile()
  adapter = ClickHouseL2Adapter(profile)
  row = {
    "symbol": "688126.SH", "trade_date": "2026-05-21", "time": "2026-05-21 10:22:34",
    "last_price": 30.9,
    **{f"bid_price_{index}": 308700 for index in range(1, 11)},
    **{f"bid_volume_{index}": 100 for index in range(1, 11)},
    **{f"ask_price_{index}": 309700 for index in range(1, 11)},
    **{f"ask_volume_{index}": 100 for index in range(1, 11)},
  }

  quote = adapter._quote_from_row(row, 1)

  assert quote.last_price == 30.9
  assert quote.bids[0].price == pytest.approx(30.87)
  assert quote.asks[0].price == pytest.approx(30.97)


def test_temporary_quote_profile_adapts_depth_prices_already_in_yuan() -> None:
  adapter = ClickHouseL2Adapter(load_temporary_quote_profile())
  row = {
    "symbol": "688981.SH", "trade_date": "2026-07-06", "time": "2026-07-06 10:15:03",
    "last_price": 139.8,
    **{f"bid_price_{index}": 139.8 for index in range(1, 11)},
    **{f"bid_volume_{index}": 100 for index in range(1, 11)},
    **{f"ask_price_{index}": 139.81 for index in range(1, 11)},
    **{f"ask_volume_{index}": 100 for index in range(1, 11)},
  }

  quote = adapter._quote_from_row(row, 1)

  assert quote.bids[0].price == pytest.approx(139.8)
  assert quote.asks[0].price == pytest.approx(139.81)


def test_quote_adapter_treats_zero_price_levels_as_empty_side() -> None:
  adapter = ClickHouseL2Adapter(load_temporary_quote_profile())
  row = {
    "symbol": "688126.SH", "trade_date": "2026-05-19", "time": "2026-05-19 14:56:57",
    "last_price": 29.52,
    **{f"bid_price_{index}": 295200 for index in range(1, 11)},
    **{f"bid_volume_{index}": 100 for index in range(1, 11)},
    **{f"ask_price_{index}": 0 for index in range(1, 11)},
    **{f"ask_volume_{index}": 0 for index in range(1, 11)},
  }

  quote = adapter._quote_from_row(row, 1)

  assert quote.bids[0].price == pytest.approx(29.52)
  assert quote.asks == ()


def test_scaled_depth_price_fills_requested_quantity_instead_of_one_share() -> None:
  engine = L2ExecutionEngine(BuyStrategy(quantity=1200), 1_000_000, 0, InstrumentRules())
  engine.begin_day("20260102")
  engine.process_chunk([_bar()], [_quote("2026-01-02 09:31:03", ask_volume=1200)], [])

  fills = engine.result()["fills"]

  assert len(fills) == 1
  assert fills[0]["quantity"] == 1200
  assert fills[0]["price"] == 10


def test_star_market_open_quantity_uses_minimum_200_and_one_share_step() -> None:
  rules = InstrumentRules(min_order_quantity=200, quantity_step=1)

  assert not rules.is_valid_open_quantity(199)
  assert rules.is_valid_open_quantity(200)
  assert rules.is_valid_open_quantity(201)


def test_l2_session_rules_follow_frontend_short_permission() -> None:
  session = L2Runner._session_payload({
    "symbol": "688126.SH",
    "scriptId": "script-1",
    "savedScriptSnapshot": {"id": "script-1", "code": "def on_bar(bar, state):\n  return []"},
    "strategyParams": {"l2InstrumentRules": {"allow_short": False}},
    "allowLong": True,
    "allowShort": True,
    "initialCapital": 1_000_000,
    "commissionRate": 0.0003,
  })

  assert session["instrumentRules"]["allow_short"] is True
  assert session["quoteMode"] == "order_driven"
  assert session["instrumentRules"]["min_order_quantity"] == 200
  assert session["instrumentRules"]["quantity_step"] == 1
  assert session["runtimeVersion"] == "legacy"


def test_l2_session_uses_continuous_mode_for_quote_script() -> None:
  payload = {
    "symbol": "600584.SH",
    "scriptId": "script-quote",
    "savedScriptSnapshot": {
      "id": "script-quote",
      "code": "def on_bar(bar, state):\n  return []\n\ndef on_quote(quote, state):\n  return []\n",
    },
    "strategyParams": {},
    "initialCapital": 1_000_000,
    "commissionRate": 0.0003,
  }

  assert _quote_mode(payload) == "continuous"
  assert L2Runner._session_payload(payload)["quoteMode"] == "continuous"


def test_l2_session_payload_keeps_directional_initial_position() -> None:
  session = L2Runner._session_payload({
    "symbol": "688981.SH",
    "scriptId": "script-1",
    "savedScriptSnapshot": {"id": "script-1", "code": "def on_bar(bar, state):\n  return []"},
    "strategyParams": {},
    "initialCapital": 1_000_000,
    "commissionRate": 0.0003,
    "strategyCategory": "directional_holding",
    "holdingPolicy": "overnight",
    "baseQuantity": 1_000,
    "baseCostPrice": 10,
    "initialPositionEnabled": True,
  })

  assert session["strategyCategory"] == "directional_holding"
  assert session["holdingPolicy"] == "overnight"
  assert session["initialPositionEnabled"] is True
  assert session["baseQuantity"] == 1_000


def test_l2_base_position_is_sellable_and_visible_to_script() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules(), holding_policy="overnight", base_quantity=1_000, base_cost_price=10)
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")

  snapshot = engine._snapshot()

  assert snapshot.positions["688981.SH"].quantity == 1_000
  assert snapshot.base_position == 1_000
  assert snapshot.sellable_quantity == 1_000



def test_l2_close_long_is_limited_to_actual_sellable_quantity() -> None:
  engine = L2ExecutionEngine(
    BuyStrategy(),
    100_000,
    0,
    InstrumentRules(),
    holding_policy="overnight",
    base_quantity=100,
    base_cost_price=10,
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")

  close = OrderIntent("688981.SH", OrderSide.SELL, 150, "平多", position_effect=PositionEffect.CLOSE_LONG)
  engine._submit(close, parse_timestamp("2026-01-02 09:31:00"))

  assert len(engine._orders) == 1
  assert engine._orders[0].intent.quantity == 100


def test_l2_overlay_sell_is_limited_by_position_and_t0_capacity() -> None:
  engine = L2ExecutionEngine(
    BuyStrategy(),
    100_000,
    0,
    InstrumentRules(),
    holding_policy="intraday",
    base_quantity=100,
    base_cost_price=10,
    t0_enabled=True,
    strategy_category="core_position_overlay",
    overlay_settlement_policy="cross_day_carry",
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")

  sell = OrderIntent(
    "688981.SH", OrderSide.SELL, 150, "负增强", position_effect=PositionEffect.OPEN_SHORT,
  )
  engine._submit(sell, parse_timestamp("2026-01-02 09:31:00"))

  assert len(engine._orders) == 1
  assert engine._orders[0].intent.quantity == 100
  assert engine._orders[0].intent.position_key == "base_position"



def test_l2_t0_capacity_reserves_both_open_directions(monkeypatch: pytest.MonkeyPatch) -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules(allow_short=True), holding_policy="intraday", base_quantity=5_000, base_cost_price=10, t0_enabled=True)
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  buy = OrderIntent("688981.SH", OrderSide.BUY, 2_000, "策略开多", position_effect=PositionEffect.OPEN_LONG, position_key="script:test")
  sell_short = OrderIntent("688981.SH", OrderSide.SELL, 2_000, "策略开空", position_effect=PositionEffect.OPEN_SHORT, position_key="script:test")

  engine._submit(buy, parse_timestamp("2026-01-02 09:31:00"))
  buy_order = engine._orders[-1]
  engine._apply_fill(buy_order, 2_000, 10, "2026-01-02 09:31:03", "test")
  engine._submit(sell_short, parse_timestamp("2026-01-02 09:32:00"))
  sell_order = engine._orders[-1]
  engine._apply_fill(sell_order, 2_000, 10, "2026-01-02 09:32:03", "test")

  snapshot = engine._snapshot()

  assert snapshot.positions["688981.SH"].quantity == 5_000
  assert snapshot.sellable_quantity == 5_000
  assert snapshot.t0_available_quantity == 1_000


def test_l2_t0_rejects_open_position_after_capacity_is_exhausted() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules(allow_short=True), holding_policy="intraday", base_quantity=5_000, base_cost_price=10, t0_enabled=True)
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  buy = OrderIntent("688981.SH", OrderSide.BUY, 3_000, "策略开多", position_effect=PositionEffect.OPEN_LONG, position_key="script:test")
  sell_short = OrderIntent("688981.SH", OrderSide.SELL, 2_000, "策略开空", position_effect=PositionEffect.OPEN_SHORT, position_key="script:test")
  extra_buy = OrderIntent("688981.SH", OrderSide.BUY, 200, "超额度开多", position_effect=PositionEffect.OPEN_LONG, position_key="script:test")

  engine._submit(buy, parse_timestamp("2026-01-02 09:31:00"))
  engine._apply_fill(engine._orders[-1], 3_000, 10, "2026-01-02 09:31:03", "test")
  engine._submit(sell_short, parse_timestamp("2026-01-02 09:32:00"))
  engine._apply_fill(engine._orders[-1], 2_000, 10, "2026-01-02 09:32:03", "test")

  order_count = len(engine._orders)
  engine._submit(extra_buy, parse_timestamp("2026-01-02 09:33:00"))

  assert len(engine._orders) == order_count
  assert engine.result()["orders"][-1]["rejectionReason"] == "底仓做 T 剩余额度不足"


def test_l2_overlay_intraday_settlement_keeps_only_base_position() -> None:
  engine = L2ExecutionEngine(
    BuyStrategy(), 100_000, 0, InstrumentRules(allow_short=True),
    base_quantity=5_000, base_cost_price=10, t0_enabled=True,
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  open_long = OrderIntent("688981.SH", OrderSide.BUY, 2_000, "策略开多", position_effect=PositionEffect.OPEN_LONG, position_key="script:test")
  open_short = OrderIntent("688981.SH", OrderSide.SELL, 1_000, "策略开空", position_effect=PositionEffect.OPEN_SHORT, position_key="script:test")
  engine._submit(open_long, parse_timestamp("2026-01-02 09:31:00"))
  engine._apply_fill(engine._orders[-1], 2_000, 10, "2026-01-02 09:31:03", "test")
  engine._submit(open_short, parse_timestamp("2026-01-02 09:32:00"))
  engine._apply_fill(engine._orders[-1], 1_000, 10, "2026-01-02 09:32:03", "test")
  engine._last_quote = QuoteSnapshot(
    "688981.SH", "20260102", "2026-01-02 14:56:57", 1,
    (QuoteLevel(10, 10_000),), (QuoteLevel(10, 10_000),), 10, "v1",
  )

  engine.finish_day("20260102")

  snapshot = engine._snapshot()
  assert snapshot.long_positions_by_key["base_position"].quantity == 5_000
  assert snapshot.long_positions_by_key.get("script:test:overlay_long") is None
  assert snapshot.short_positions_by_key.get("script:test:overlay_short") is None


def test_l2_overlay_day_end_uses_last_bar_close_after_quote_price_settlement() -> None:
  engine = L2ExecutionEngine(
    SellBaseOnceStrategy(), 100_000, 0, InstrumentRules(),
    base_quantity=100, base_cost_price=10, t0_enabled=True,
    strategy_category="core_position_overlay",
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(_quote("2026-01-02 09:31:03"))
  engine.process_quote(QuoteSnapshot(
    "688981.SH", "20260102", "2026-01-02 14:56:57", 1,
    (QuoteLevel(10.4, 1_000),), (QuoteLevel(10.5, 1_000),), 10.45, "v1",
  ))

  engine.finish_day("20260102")

  raw_result = engine.result()
  assert [(fill["side"], fill["price"]) for fill in raw_result["fills"]] == [
    ("SELL", 9.9),
    ("BUY", 10.5),
  ]
  assert [fill["fee"] for fill in raw_result["fills"]] == [0.5049, 0.0105]
  assert raw_result["equity"] == [{
    "time": "2026-01-02 09:31:00",
    "equity": 100_939.4846,
    "cash": 99_939.4846,
    "close": 10,
    "longQty": 100,
    "shortQty": 0,
    "baseQty": 100,
    "targetBaseQty": 100,
    "actualQty": 100,
    "overlayQty": 0,
    "sellableQty": 0,
  }]

  result = normalize_l2_result(raw_result, {
    "initialCapital": 100_000,
    "baseQuantity": 100,
    "baseCostPrice": 10,
    "t0Enabled": True,
    "strategyCategory": "core_position_overlay",
  })

  assert result["summary"]["accountPnl"] == -60.52
  assert result["summary"]["baseBenchmarkPnl"] == 0
  assert result["summary"]["overlayPnl"] == -60.52
  assert result["summary"]["realizedOverlayPnl"] == -60.52


def test_l2_day_end_rejects_quote_fallback_without_valid_bar() -> None:
  engine = L2ExecutionEngine(
    SellBaseOnceStrategy(), 100_000, 0, InstrumentRules(),
    base_quantity=100, base_cost_price=10, t0_enabled=True,
    strategy_category="core_position_overlay",
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  engine.process_quote(_quote("2026-01-02 14:56:57"))

  with pytest.raises(ValueError, match="20260102 无有效 Bar，无法进行日终结算"):
    engine.finish_day("20260102")


def test_l2_overlay_carry_uses_bar_close_for_unrealized_pnl() -> None:
  engine = L2ExecutionEngine(
    BuyStrategy(), 100_000, 0, InstrumentRules(),
    base_quantity=100, base_cost_price=10, t0_enabled=True,
    strategy_category="core_position_overlay",
    overlay_settlement_policy="cross_day_carry",
  )
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  engine.process_quote(QuoteSnapshot(
    "688981.SH", "20260102", "2026-01-02 09:31:03", 1,
    (QuoteLevel(10.4, 1_000),), (QuoteLevel(10.5, 1_000),), 10.45, "v1",
  ))

  engine.finish_day("20260102")

  raw_result = engine.result()
  assert [(fill["price"], fill["fee"]) for fill in raw_result["fills"]] == [(10.5, 0.0105)]
  assert raw_result["equity"][0]["cash"] == 98_949.9895
  assert raw_result["equity"][0]["equity"] == 100_949.9895
  assert raw_result["equity"][0]["close"] == 10

  result = normalize_l2_result(raw_result, {
    "initialCapital": 100_000,
    "baseQuantity": 100,
    "baseCostPrice": 10,
    "t0Enabled": True,
    "strategyCategory": "core_position_overlay",
    "overlaySettlementPolicy": "cross_day_carry",
  })

  assert result["summary"]["accountPnl"] == -50.01
  assert result["summary"]["baseBenchmarkPnl"] == 0
  assert result["summary"]["overlayPnl"] == -50.01
  assert result["summary"]["unrealizedOverlayPnl"] == -50.01


def test_l2_result_adapter_reports_base_holding_and_t0_excess() -> None:
  raw_result = {
    "fills": [],
    "equity": [{"time": "2026-01-02 14:57:00", "equity": 2_250, "cash": 1_050, "close": 12, "longQty": 100}],
    "orders": [],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {
    "initialCapital": 1_000,
    "baseQuantity": 100,
    "baseCostPrice": 10,
    "t0Enabled": True,
  })

  assert result["dailyHoldings"][0]["date"] == "2026-01-02"
  assert result["summary"]["baseHoldingPnl"] == 200
  assert result["summary"]["baseHoldingReturn"] == 0.1
  assert result["summary"]["t0ExcessPnl"] == 50
  assert result["summary"]["t0ExcessReturn"] == 0.05
  assert result["strategyCategory"] == "core_position_overlay"
  assert result["strategyMetrics"]["t0Metrics"]["t0ExcessPnl"] == 50


def test_l2_result_adapter_normalizes_pipe_enum_side_values() -> None:
  raw_result = {
    "fills": [{
      "time": "2026-01-02 09:31:03",
      "side": "OrderSide.BUY",
      "quantity": 100,
      "price": 10.0,
      "reason": "开多",
      "metadata": {"direction": "BUY_OPEN"},
    }],
    "equity": [],
    "orders": [],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {"initialCapital": 1_000})

  assert result["trades"][0]["side"] == "BUY"


def test_l2_result_adapter_normalizes_saved_l2_result_side_values() -> None:
  saved_result = {
    "summary": {"totalReturn": 0.01},
    "equity": [],
    "dailyHoldings": [],
    "trades": [{"side": "OrderSide.SELL", "action": "SELL_CLOSE"}],
    "skippedSignals": [{"side": "OrderSide.BUY", "action": "BUY_OPEN"}],
  }

  result = normalize_l2_result(saved_result, {"initialCapital": 1_000})

  assert result["trades"][0]["side"] == "SELL"
  assert result["skippedSignals"][0]["side"] == "BUY"


def test_l2_normalized_history_result_converts_compact_holding_dates() -> None:
  result = normalize_l2_result({
    "summary": {"totalReturn": 0.01},
    "equity": [{"date": "20260102", "pnl": 10, "value": 1_010}],
    "dailyHoldings": [{"date": "20260102", "close": 10, "longQty": 0, "shortQty": 0, "cash": 1_010, "totalEquity": 1_010, "dailyPnl": 10}],
  }, {"initialCapital": 1_000})

  assert result["equity"][0]["date"] == "2026-01-02"
  assert result["dailyHoldings"][0]["date"] == "2026-01-02"


def test_l2_directional_result_uses_complete_position_cycles() -> None:
  raw_result = {
    "fills": [{
      "time": "2026-01-02 09:31:03",
      "symbol": "688981.SH",
      "side": "BUY",
      "quantity": 100,
      "price": 10.0,
      "fee": 0.0,
      "realized_pnl": 0.0,
      "position_effect": "open_long",
      "position_key": "script:test",
    }, {
      "time": "2026-01-05 09:31:03",
      "symbol": "688981.SH",
      "side": "SELL",
      "quantity": 100,
      "price": 11.0,
      "fee": 0.0,
      "realized_pnl": 100.0,
      "position_effect": "close_long",
      "position_key": "script:test",
    }],
    "equity": [],
    "orders": [],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {
    "initialCapital": 10_000,
    "strategyCategory": "directional_holding",
  })

  assert result["resultSchemaVersion"] == 3
  assert result["summary"]["completedTradeCount"] == 1
  assert result["summary"]["openTradeCount"] == 0
  assert result["summary"]["winRate"] == 1.0
  assert result["cycles"] == []


def test_l2_directional_result_accepts_pipe_enum_and_serialized_effects() -> None:
  raw_result = {
    "fills": [{
      "time": "2026-01-02 09:31:03", "symbol": "688981.SH", "side": "BUY",
      "quantity": 100, "price": 10.0, "fee": 0.0, "realized_pnl": 0.0,
      "position_effect": PositionEffect.OPEN_LONG, "position_key": "script:long",
    }, {
      "time": "2026-01-02 10:31:03", "symbol": "688981.SH", "side": "SELL",
      "quantity": 100, "price": 11.0, "fee": 0.0, "realized_pnl": 100.0,
      "position_effect": "PositionEffect.CLOSE_LONG", "position_key": "script:long",
    }, {
      "time": "2026-01-02 11:31:03", "symbol": "688981.SH", "side": "SELL",
      "quantity": 100, "price": 11.0, "fee": 0.0, "realized_pnl": 0.0,
      "position_effect": "OPEN_SHORT", "position_key": "script:short",
    }, {
      "time": "2026-01-02 13:31:03", "symbol": "688981.SH", "side": "BUY",
      "quantity": 100, "price": 10.0, "fee": 0.0, "realized_pnl": 100.0,
      "position_effect": "close_short", "position_key": "script:short",
    }],
    "equity": [{
      "time": "2026-01-02 14:57:00", "equity": 10_200, "cash": 10_200,
      "close": 10.0,
    }],
    "orders": [],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {
    "initialCapital": 10_000,
    "strategyCategory": "directional_holding",
  })

  assert result["summary"]["completedTradeCount"] == 2
  assert result["summary"]["winningTrades"] == 2
  assert result["summary"]["openTradeCount"] == 0
  assert result["dailyHoldings"][0]["longQty"] == 0
  assert result["dailyHoldings"][0]["shortQty"] == 0


def test_l2_daily_holdings_accepts_pipe_position_effect_enum() -> None:
  result = normalize_l2_result({
    "fills": [{
      "time": "2026-01-02 09:31:03", "symbol": "688981.SH", "side": "SELL",
      "quantity": 100, "price": 10.0, "fee": 0.0, "realized_pnl": 0.0,
      "position_effect": PositionEffect.OPEN_SHORT, "position_key": "script:short",
    }],
    "equity": [{
      "time": "2026-01-02 14:57:00", "equity": 10_000, "cash": 11_000,
      "close": 10.0,
    }],
    "orders": [],
    "warnings": [],
  }, {
    "initialCapital": 10_000,
    "strategyCategory": "directional_holding",
  })

  assert result["dailyHoldings"][0]["longQty"] == 0
  assert result["dailyHoldings"][0]["shortQty"] == 100


def test_l2_overlay_result_keeps_cross_day_cycle_and_manifest_policy() -> None:
  raw_result = {
    "fills": [{
      "time": "2026-01-02 09:31:03",
      "symbol": "688981.SH",
      "side": "SELL",
      "quantity": 100,
      "price": 11.0,
      "fee": 0.0,
      "realized_pnl": 100.0,
      "position_effect": "close_long",
      "position_key": "base_position",
    }, {
      "time": "2026-01-05 09:31:03",
      "symbol": "688981.SH",
      "side": "BUY",
      "quantity": 100,
      "price": 10.0,
      "fee": 0.0,
      "realized_pnl": 0.0,
      "position_effect": "open_long",
      "position_key": "base_position",
    }],
    "equity": [{
      "time": "2026-01-02 14:57:00", "equity": 2_100, "cash": 2_100, "close": 11, "longQty": 0, "sellableQty": 0,
    }, {
      "time": "2026-01-05 14:57:00", "equity": 2_100, "cash": 1_100, "close": 10, "longQty": 100, "sellableQty": 100,
    }],
    "orders": [],
    "warnings": [],
  }

  result = normalize_l2_result(raw_result, {
    "initialCapital": 1_000,
    "baseQuantity": 100,
    "baseCostPrice": 10,
    "t0Enabled": True,
    "strategyCategory": "core_position_overlay",
    "overlaySettlementPolicy": "cross_day_carry",
  })

  assert result["summary"]["completedCycleCount"] == 1
  assert result["summary"]["realizedOverlayPnl"] == 100
  assert result["trades"][0]["accountRealizedPnl"] == 100
  assert result["trades"][0]["pnl"] == 0
  assert result["trades"][1]["accountRealizedPnl"] == 0
  assert result["trades"][1]["pnl"] == 100
  assert result["cycles"][0]["status"] == "completed"
  assert result["experimentManifest"]["overlaySettlementPolicy"] == "cross_day_carry"
  assert result["dailyHoldings"][1]["sellableQuantity"] == 100
  assert result["experimentManifest"]["resultSchemaVersion"] == 3


def test_l2_legacy_manifest_keeps_result_schema_version_two() -> None:
  result = normalize_l2_result({"fills": [], "equity": [], "orders": []}, {"initialCapital": 1_000})

  assert result["resultSchemaVersion"] == 2
  assert result["experimentManifest"]["resultSchemaVersion"] == 2


def test_l2_intraday_closes_overlay_long_at_one_sided_limit_quote() -> None:
  engine = L2ExecutionEngine(BuyStrategy(), 100_000, 0, InstrumentRules(), base_quantity=1_000, base_cost_price=10)
  engine.initialize_base_position("688981.SH")
  engine.begin_day("20260102")
  engine.process_bar(_bar())
  buy = OrderIntent("688981.SH", OrderSide.BUY, 1_000, "策略开多", position_effect=PositionEffect.OPEN_LONG, position_key="script:test")
  engine._submit(buy, parse_timestamp("2026-01-02 09:31:00"))
  engine._apply_fill(engine._orders[-1], 1_000, 10, "2026-01-02 09:31:03", "test")
  engine._last_quote = QuoteSnapshot("688981.SH", "20260102", "2026-01-02 14:56:57", 1, (QuoteLevel(9, 1_000),), (), 9, "v1")

  engine.finish_day("20260102")

  snapshot = engine._snapshot()
  assert snapshot.long_positions_by_key["base_position"].quantity == 1_000
  assert snapshot.long_positions_by_key.get("script:test:overlay_long") is None


def test_basic_l2_chunks_do_not_include_tick_data() -> None:
  class QuoteProvider:
    def count_rows(self, symbol: str, trade_date: str) -> int:
      return 2

    def iter_quotes(self, symbol: str, trade_date: str, page_size: int):
      yield [_quote("2026-01-02 09:31:03"), _quote("2026-01-02 09:31:06")]

  provider = QuoteProvider()
  chunks = list(_day_chunks([_bar()], provider, "688981.SH", "20260102", 2))

  assert _expected_chunk_count([_bar()], provider, "688981.SH", "20260102", 2) == 2
  assert sum(len(chunk["quotes"]) for chunk in chunks) == 2
  assert all(chunk["trades"] == [] for chunk in chunks)


def test_order_driven_runner_does_not_query_quotes_without_orders(monkeypatch: pytest.MonkeyPatch) -> None:
  class QuoteProvider:
    def quotes_after(self, *args, **kwargs):
      raise AssertionError("无订单时不应查询盘口")

    def last_quote_before(self, *args, **kwargs):
      raise AssertionError("无仓位时不应查询收盘盘口")

  class ReplaySession:
    def begin_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

    def process_bars(self, bars: list[NormalizedBar]) -> dict[str, Any]:
      return {"processedBars": len(bars), "quoteDemand": "idle", "dayEndQuoteRequired": False}

    def finish_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

  monkeypatch.setattr("backend.l2_runner.state_store.heartbeat_l2_backtest_job", lambda *args: True)

  performance = _new_performance()
  _replay_day(ReplaySession(), [_bar()], QuoteProvider(), "688981.SH", "20260102", {"runId": "run", "attemptToken": "token"}, performance)

  assert performance["quotesFetched"] == 0
  assert performance["quoteQueries"] == 0


def test_order_driven_runner_queries_quote_only_after_order(monkeypatch: pytest.MonkeyPatch) -> None:
  class QuoteProvider:
    def __init__(self) -> None:
      self.calls: list[tuple[str, str, int]] = []

    def quotes_after(self, symbol: str, trade_date: str, after_time: str, before_time: str | None, limit: int):
      self.calls.append((after_time, before_time, limit))
      return [_quote("2026-01-02 09:31:03")] if len(self.calls) == 1 else []

    def last_quote_before(self, *args, **kwargs):
      return None

  provider = QuoteProvider()
  bar_calls = 0

  class ReplaySession:
    def begin_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

    def process_bars(self, bars: list[NormalizedBar]) -> dict[str, Any]:
      nonlocal bar_calls
      bar_calls += 1
      if bar_calls == 1:
        return {"processedBars": 1, "quoteDemand": "waiting_quote", "nextQuoteAfter": "2026-01-02 09:31:00", "dayEndQuoteRequired": False}
      return {"processedBars": len(bars), "quoteDemand": "idle", "dayEndQuoteRequired": False}

    def process_quote(self, quote: QuoteSnapshot) -> dict[str, Any]:
      assert quote.time == "2026-01-02 09:31:03"
      return {"quoteDemand": "idle", "dayEndQuoteRequired": False}

    def finish_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

  monkeypatch.setattr("backend.l2_runner.state_store.heartbeat_l2_backtest_job", lambda *args: True)

  performance = _new_performance()
  _replay_day(ReplaySession(), [_bar()], provider, "688981.SH", "20260102", {"runId": "run", "attemptToken": "token"}, performance)

  assert provider.calls == [("2026-01-02 09:31:00", None, 128)]
  assert performance["quotesFetched"] == 1
  assert performance["quoteRowsPrefetched"] == 1


def test_order_driven_runner_uses_actual_last_quote_for_day_end(monkeypatch: pytest.MonkeyPatch) -> None:
  class QuoteProvider:
    def quotes_after(self, *args: Any, **kwargs: Any) -> list[QuoteSnapshot]:
      return [_quote("2026-01-02 14:51:03")]

    def last_quote_before(self, symbol: str, trade_date: str, before_time: str) -> QuoteSnapshot:
      assert before_time == "2026-01-02 14:57:00"
      return _quote("2026-01-02 14:56:57")

  processed_quotes: list[str] = []

  class ReplaySession:
    def begin_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

    def process_bars(self, bars: list[NormalizedBar]) -> dict[str, Any]:
      assert len(bars) == 1
      return {
        "processedBars": 1,
        "quoteDemand": "waiting_quote",
        "nextQuoteAfter": "2026-01-02 14:51:00",
        "dayEndQuoteRequired": False,
      }

    def process_quote(self, quote: QuoteSnapshot) -> dict[str, Any]:
      processed_quotes.append(quote.time)
      return {"quoteDemand": "idle", "dayEndQuoteRequired": True}

    def finish_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

  monkeypatch.setattr("backend.l2_runner.state_store.heartbeat_l2_backtest_job", lambda *args: True)

  performance = _new_performance()
  _replay_day(ReplaySession(), [_bar()], QuoteProvider(), "688981.SH", "20260102", {"runId": "run", "attemptToken": "token"}, performance)

  assert processed_quotes == ["2026-01-02 14:51:03", "2026-01-02 14:56:57"]
  assert performance["quoteQueries"] == 2


def test_order_driven_runner_reuses_future_quote_buffer_without_forward_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
  class QuoteProvider:
    def __init__(self) -> None:
      self.calls: list[tuple[str, str | None, int]] = []

    def quotes_after(self, symbol: str, trade_date: str, after_time: str, before_time: str | None, limit: int):
      self.calls.append((after_time, before_time, limit))
      return [_quote("2026-01-02 09:31:03"), _quote("2026-01-02 09:32:03")]

    def last_quote_before(self, *args: Any, **kwargs: Any):
      return None

  provider = QuoteProvider()
  second_bar = replace(_bar(), start_time="2026-01-02 09:31:00", end_time="2026-01-02 09:32:00")
  events: list[str] = []
  bar_calls = 0

  class ReplaySession:
    def begin_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

    def process_bars(self, bars: list[NormalizedBar]) -> dict[str, Any]:
      nonlocal bar_calls
      bar = bars[0]
      events.append(f"bar:{bar.end_time}")
      bar_calls += 1
      return {
        "processedBars": 1,
        "quoteDemand": "waiting_quote",
        "nextQuoteAfter": bar.end_time,
        "dayEndQuoteRequired": False,
      }

    def process_quote(self, quote: QuoteSnapshot) -> dict[str, Any]:
      events.append(f"quote:{quote.time}")
      return {"quoteDemand": "idle", "dayEndQuoteRequired": False}

    def finish_day(self, trade_date: str) -> None:
      assert trade_date == "2026-01-02"

  monkeypatch.setattr("backend.l2_runner.state_store.heartbeat_l2_backtest_job", lambda *args: True)

  performance = _new_performance()
  _replay_day(ReplaySession(), [_bar(), second_bar], provider, "688981.SH", "20260102", {"runId": "run", "attemptToken": "token"}, performance)

  assert bar_calls == 2
  assert events == [
    "bar:2026-01-02 09:31:00",
    "quote:2026-01-02 09:31:03",
    "bar:2026-01-02 09:32:00",
    "quote:2026-01-02 09:32:03",
  ]
  assert provider.calls == [("2026-01-02 09:31:00", None, 128)]
  assert performance["quoteQueries"] == 1
  assert performance["quoteBufferHits"] == 1
  assert performance["maxQuoteBufferRows"] == 2


@pytest.mark.parametrize(("value", "expected"), [("2026-01-02", "2026-01-02"), ("20260102", "2026-01-02")])
def test_temporary_quote_provider_normalizes_frontend_date(value: str, expected: str) -> None:
  assert TemporaryQuoteBarProvider._parse_date(value).isoformat() == expected


def test_temporary_quote_provider_excludes_zero_price_non_trading_days(monkeypatch: pytest.MonkeyPatch) -> None:
  provider = TemporaryQuoteBarProvider(
    load_temporary_quote_profile(), VersionedBarCache(None, 60), "tick_quote_session_v5", "tick_quote_depth_v3",
  )
  queries: list[str] = []
  monkeypatch.setattr(clickhouse_client, "query_rows", lambda query: queries.append(query) or [{"trade_date": "2025-03-10"}])

  assert provider._trading_days("688126.SH", "2025-03-07", "2025-03-10") == ["2025-03-10"]
  assert "last_price > 0" in queries[0]
  assert "toDayOfWeek(trade_date) BETWEEN 1 AND 5" in queries[0]
  assert "09:30:00" in queries[0]


def test_temporary_aggregation_does_not_require_fixed_snapshot_count() -> None:
  provider = TemporaryQuoteBarProvider(
    load_temporary_quote_profile(),
    VersionedBarCache(None, 60),
    "tick_quote_session_v4",
    "tick_quote_depth_v3",
  )

  query = provider._aggregation_query("688981.SH", "2026-01-02", "1m", 60)

  assert "snapshot_count >=" not in query
  assert "max_gap_ms <=" not in query
  assert "14:57:00" in query
  assert "15:00:00" not in query
  assert "cum_amount AS cum_amount" in query
  assert "4294967296.0 AS uint32_modulus" in query
  assert "if(cum_amount < 0, cum_amount + uint32_modulus, cum_amount) AS normalized_cum_amount" in query
  assert "normalized_cum_amount + uint32_modulus - previous_cum_amount" in query
  assert "countIf(volume < 0 OR amount_valid = 0)" in query


def test_temporary_quote_profile_maps_cumulative_amount_for_wrap_repair() -> None:
  profile = load_temporary_quote_profile()

  assert profile.columns["cum_amount"] == "cum_amount"


def test_continuous_quote_filter_excludes_closing_auction() -> None:
  adapter = ClickHouseL2Adapter(load_temporary_quote_profile())

  query = adapter._continuous_session_filter()

  assert "14:57:00" in query
  assert "15:00:00" not in query
  assert "concat(toString" in query


def test_clickhouse_query_reads_gzip_response(monkeypatch: pytest.MonkeyPatch) -> None:
  body = gzip.compress(json.dumps({"data": [{"value": 1}]}).encode("utf-8"))

  class Response:
    status = 200
    reason = "OK"
    headers: dict[str, str] = {}

    @staticmethod
    def read() -> bytes:
      return body

    @staticmethod
    def getheader(name: str, default: str) -> str:
      return "gzip" if name == "Content-Encoding" else default

  class Connection:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
      self.requests.append((method, path, headers))

    @staticmethod
    def getresponse() -> Response:
      return Response()

    @staticmethod
    def close() -> None:
      return None

  connection = Connection()

  monkeypatch.setattr(clickhouse_client, "_connection", lambda: ("http://clickhouse", {}))
  monkeypatch.setattr(clickhouse_client, "_new_connection", lambda *_: connection)
  monkeypatch.setattr(clickhouse_client._thread_connections, "connection", None, raising=False)

  assert clickhouse_client.query_rows("SELECT 1") == [{"value": 1}]
  assert clickhouse_client.query_rows("SELECT 2") == [{"value": 1}]
  assert len(connection.requests) == 2
  assert connection.requests[0][2]["Accept-Encoding"] == "gzip"


def test_clickhouse_query_reconnects_after_remote_close(monkeypatch: pytest.MonkeyPatch) -> None:
  calls = 0

  class Response:
    @staticmethod
    def read() -> bytes:
      return b'{"data":[{"value":1}]}'

    @staticmethod
    def getheader(name: str, default: str) -> str:
      return default

  def request(*_: Any) -> Response:
    nonlocal calls
    calls += 1
    if calls == 1:
      raise clickhouse_client.RemoteDisconnected("remote closed")
    return Response()

  monkeypatch.setattr(clickhouse_client, "_connection", lambda: ("http://clickhouse", {}))
  monkeypatch.setattr(clickhouse_client, "_request", request)

  assert clickhouse_client.query_rows("SELECT 1") == [{"value": 1}]
  assert calls == 2


def test_quote_adapter_derives_sequence_when_time_is_unique(monkeypatch: pytest.MonkeyPatch) -> None:
  profile = SourceMappingProfile(
    source_name="quote", table_name="quant.tick_quote",
    columns={
      "symbol": "symbol", "trade_date": "trade_date", "time": "datetime", "last_price": "last_price",
      **{f"bid_price_{index}": f"bid_price_{index}" for index in range(1, 11)},
      **{f"bid_volume_{index}": f"bid_vol_{index}" for index in range(1, 11)},
      **{f"ask_price_{index}": f"ask_price_{index}" for index in range(1, 11)},
      **{f"ask_volume_{index}": f"ask_vol_{index}" for index in range(1, 11)},
    },
  )

  def query_rows(query: str) -> list[dict[str, Any]]:
    if query.startswith("DESCRIBE"):
      return [{"name": item} for item in profile.columns.values()]
    if "uniqExact" in query:
      return [{"total": 2, "unique_total": 2}]
    return [
      {"symbol": "688981.SH", "trade_date": "2026-01-02", "time": "2026-01-02 09:30:02", "last_price": 10,
       **{f"bid_price_{index}": 9.9 for index in range(1, 11)}, **{f"bid_volume_{index}": 100 for index in range(1, 11)},
       **{f"ask_price_{index}": 10.1 for index in range(1, 11)}, **{f"ask_volume_{index}": 100 for index in range(1, 11)}},
      {"symbol": "688981.SH", "trade_date": "2026-01-02", "time": "2026-01-02 09:30:05", "last_price": 10,
       **{f"bid_price_{index}": 9.9 for index in range(1, 11)}, **{f"bid_volume_{index}": 100 for index in range(1, 11)},
       **{f"ask_price_{index}": 10.1 for index in range(1, 11)}, **{f"ask_volume_{index}": 100 for index in range(1, 11)}},
    ]

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)

  quotes = list(ClickHouseL2Adapter(profile).iter_quotes("688981.SH", "2026-01-02", 100))[0]

  assert [quote.sequence for quote in quotes] == [1, 2]


def test_quote_adapter_reads_next_snapshot_with_keyset_query(monkeypatch: pytest.MonkeyPatch) -> None:
  profile = load_temporary_quote_profile()
  queries: list[str] = []
  row = {
    "symbol": "688981.SH", "trade_date": "2026-01-02", "time": "2026-01-02 09:31:03", "last_price": 10,
    **{f"bid_price_{index}": 99000 for index in range(1, 11)},
    **{f"bid_volume_{index}": 100 for index in range(1, 11)},
    **{f"ask_price_{index}": 100000 for index in range(1, 11)},
    **{f"ask_volume_{index}": 100 for index in range(1, 11)},
  }

  def query_rows(query: str) -> list[dict[str, Any]]:
    queries.append(query)
    if query.startswith("DESCRIBE"):
      return [{"name": item} for item in profile.columns.values()]
    if "uniqExact" in query:
      return [{"total": 1, "unique_total": 1}]
    return [row]

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)

  quote = ClickHouseL2Adapter(profile).next_quote_after(
    "688981.SH", "2026-01-02", "2026-01-02 09:31:00", "2026-01-02 09:32:00",
  )

  assert quote is not None
  assert quote.time == "2026-01-02 09:31:03"
  assert "OFFSET" not in queries[-1]
  assert "LIMIT 1" in queries[-1]
  assert "datetime>'2026-01-02T09:31:00'" in queries[-1]


def test_clickhouse_query_retries_temporary_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
  calls = 0

  def query_rows(query: str) -> list[dict[str, Any]]:
    nonlocal calls
    calls += 1
    if calls == 1:
      error = clickhouse_client.ClickHouseError("ClickHouse 查询失败: timed out")
      error.__cause__ = TimeoutError("timed out")
      raise error
    return [{"value": 1}]

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(clickhouse_client.time, "sleep", lambda _: None)

  assert clickhouse_client.query_rows_with_retry("SELECT 1") == [{"value": 1}]
  assert calls == 2


def test_quote_page_retries_temporary_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
  adapter = ClickHouseL2Adapter(load_temporary_quote_profile())
  calls = 0

  def query_rows(query: str) -> list[dict[str, Any]]:
    nonlocal calls
    calls += 1
    if calls == 1:
      error = clickhouse_client.ClickHouseError("ClickHouse 查询失败: timed out")
      error.__cause__ = TimeoutError("timed out")
      raise error
    if calls == 2:
      return [{"symbol": "688981.SH"}]
    return []

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(clickhouse_client.time, "sleep", lambda _: None)

  pages = list(adapter._pages("symbol", "688981.SH", "2026-01-02", 100))

  assert pages == [[{"symbol": "688981.SH"}]]
  assert calls == 2


def test_worker_l2_session_keeps_script_state_across_chunks() -> None:
  session = L2WorkerSession(WorkerL2SessionCreateRequest(
    scriptId="test", code="""
def on_bar(bar, state):
  state['count'] = state.get('count', 0) + 1
  if state['count'] == 1:
    return [{'action': 'buy_open', 'quantity': 100, 'reason': '首次触发'}]
  return []
""", initialCapital=100_000, commissionRate=0, holdingPolicy="overnight",
  ))
  session.begin_day(WorkerL2DayRequest(tradeDate="20260102", expectedChunks=2))
  session.process_chunk(WorkerL2ChunkRequest(
    tradeDate="20260102", chunkNo=0, bars=[asdict(_bar())],
  ))
  session.process_chunk(WorkerL2ChunkRequest(
    tradeDate="20260102", chunkNo=1, quotes=[asdict(_quote("2026-01-02 09:31:03"))],
  ))
  session.finish_day("20260102")

  result = session.result()

  assert len(result["fills"]) == 1
  assert result["fills"][0]["quantity"] == 100


def test_l2_stream_decoder_preserves_chunk_boundaries() -> None:
  decoder = L2StreamDecoder()
  first = json.dumps({"tradeDate": "20260102", "chunkNo": 0, "bars": [asdict(_bar())]}).encode("utf-8")
  second = json.dumps({"tradeDate": "20260102", "chunkNo": 1, "quotes": [asdict(_quote("2026-01-02 09:31:03"))]}).encode("utf-8")

  assert decoder.feed(first[:20]) == []
  records = decoder.feed(first[20:] + b"\n" + second + b"\n")
  decoder.finish()

  assert [item.chunkNo for item in records] == [0, 1]
  assert len(records[0].bars) == 1
  assert len(records[1].quotes) == 1


def test_continuous_stream_chunks_maintain_chunk_order(monkeypatch: pytest.MonkeyPatch) -> None:
  class Provider:
    def iter_quotes(self, *_: Any):
      yield [_quote("2026-01-02 09:31:03"), _quote("2026-01-02 09:31:06")]

  monkeypatch.setattr("backend.l2_runner._heartbeat", lambda _: None)
  performance = _new_performance("continuous")
  chunks = list(_continuous_stream_chunks([_bar()], Provider(), "688981.SH", "2026-01-02", {}, performance))

  assert [chunk["chunkNo"] for chunk in chunks] == [0]
  assert chunks[0]["bars"][0]["end_time"] == "2026-01-02 09:31:00"
  assert [item["time"] for item in chunks[0]["quotes"]] == ["2026-01-02 09:31:03", "2026-01-02 09:31:06"]


def test_worker_l2_session_stops_bar_batch_when_order_needs_quote() -> None:
  session = L2WorkerSession(WorkerL2SessionCreateRequest(
    scriptId="test",
    code="def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100, 'reason': '测试'}]",
    initialCapital=100_000,
    commissionRate=0,
    holdingPolicy="overnight",
  ))
  session.begin_day(WorkerL2DayRequest(tradeDate="20260102"))

  response = session.process_bars(WorkerL2BarsRequest(tradeDate="20260102", bars=[asdict(_bar())]))

  assert response["processedBars"] == 1
  assert response["quoteDemand"] == "waiting_quote"
  quote_response = session.process_quote(WorkerL2QuoteRequest(
    tradeDate="20260102", quote=asdict(_quote("2026-01-02 09:31:03")),
  ))
  session.finish_day("20260102")

  assert quote_response["quoteDemand"] == "idle"
  assert len(session.result()["fills"]) == 1


def test_quote_callback_orders_fill_on_next_snapshot_and_receives_fill_event() -> None:
  session = L2WorkerSession(WorkerL2SessionCreateRequest(
    scriptId="quote-test", code="""
def on_bar(bar, state):
  return []

def on_quote(quote, state):
  if not state.get('submitted'):
    state['submitted'] = True
    return [{'action': 'buy_open', 'quantity': 100, 'clientOrderKey': 'quote-entry'}]
  return []

def on_order_event(event, state):
  if event['type'] == 'filled':
    state['fillSeen'] = event['clientOrderKey']
""", initialCapital=100_000, commissionRate=0, holdingPolicy="overnight", quoteMode="continuous",
  ))
  session.begin_day(WorkerL2DayRequest(tradeDate="20260102"))
  first = _quote("2026-01-02 09:30:03")
  second = _quote("2026-01-02 09:30:06")

  session.process_quote(WorkerL2QuoteRequest(tradeDate="20260102", quote=asdict(first)))
  assert session.result()["fills"] == []
  session.process_quote(WorkerL2QuoteRequest(tradeDate="20260102", quote=asdict(second)))

  assert len(session.result()["fills"]) == 1
  assert session.strategy._state["fillSeen"] == "quote-entry"


def test_local_strategy_session_keeps_l2_engine_in_runner_process() -> None:
  session = L2StrategySession({
    "scriptId": "local-session",
    "code": "def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100, 'reason': '测试'}]",
    "params": {},
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "latencyMs": 0,
    "passiveParticipationRate": 0.1,
    "holdingPolicy": "overnight",
    "symbol": "688981.SH",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "quoteMode": "order_driven",
    "instrumentRules": {},
  })
  try:
    session.begin_day("2026-01-02")
    demand = session.process_bars([_bar()])
    assert demand["quoteDemand"] == "waiting_quote"

    session.process_quote(_quote("2026-01-02 09:31:03"))
    session.finish_day("2026-01-02")
    result = session.result()

    assert len(result["fills"]) == 1
    assert result["fills"][0]["time"] == "2026-01-02 09:31:03"
    assert session.callback_calls >= 2
  finally:
    session.close()


def test_local_strategy_session_returns_strategy_error() -> None:
  session = L2StrategySession({
    "scriptId": "local-error",
    "code": "def on_bar(bar, state):\n  raise ValueError('策略错误')",
    "params": {},
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "latencyMs": 0,
    "passiveParticipationRate": 0.1,
    "holdingPolicy": "overnight",
    "symbol": "688981.SH",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "quoteMode": "order_driven",
    "instrumentRules": {},
  })
  try:
    session.begin_day("2026-01-02")
    with pytest.raises(Exception) as error:
      session.process_bars([_bar()])
    assert error_detail(error.value).category == "strategy_error"
    assert isinstance(error.value, RemoteBacktestError)
    assert error.value.diagnostic["exceptionType"] == "ValueError"
    assert error.value.diagnostic["trace"]
    assert error.value.diagnostic["__internal_raw_message"]
    assert error.value.diagnostic["__internal_raw_traceback"]
  finally:
    session.close()


def test_local_l2_strategy_session_supports_tuple_and_runtime_version() -> None:
  session = L2StrategySession({
    "scriptId": "local-tuple",
    "code": "def on_bar(bar, state):\n  state['items'] = tuple(enumerate(range(2)))\n  return []",
    "params": {},
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "latencyMs": 0,
    "passiveParticipationRate": 0.1,
    "holdingPolicy": "overnight",
    "symbol": "688981.SH",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "quoteMode": "order_driven",
    "runtimeVersion": "python3.12-runtime-v2",
    "instrumentRules": {},
  })
  try:
    session.begin_day("2026-01-02")
    assert session.process_bars([_bar()])["quoteDemand"] == "idle"
    session.finish_day("2026-01-02")
    assert session.result()["fills"] == []
  finally:
    session.close()


def test_strategy_session_diagnostic_redacts_credentials() -> None:
  value = _redact_diagnostic_text("http://user:password@host/path token=secret-value")

  assert "password@" not in value
  assert "secret-value" not in value


def test_local_strategy_session_preserves_quote_and_order_event_callbacks() -> None:
  session = L2StrategySession({
    "scriptId": "local-quote",
    "code": """
def on_bar(bar, state):
  return []

def on_quote(quote, state):
  if not state.get('submitted'):
    state['submitted'] = True
    return [{'action': 'buy_open', 'quantity': 100, 'clientOrderKey': 'entry'}]
  return []

def on_order_event(event, state):
  if event['type'] == 'filled':
    state['filled'] = event['clientOrderKey']
""",
    "params": {},
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "latencyMs": 0,
    "passiveParticipationRate": 0.1,
    "holdingPolicy": "overnight",
    "symbol": "688981.SH",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "quoteMode": "continuous",
    "instrumentRules": {},
  })
  try:
    session.begin_day("2026-01-02")
    session.process_bars([_bar()])
    first = session.process_quote(_quote("2026-01-02 09:31:03"))
    second = session.process_quote(_quote("2026-01-02 09:31:06"))
    session.finish_day("2026-01-02")
    result = session.result()

    assert first["quoteDemand"] == "waiting_quote"
    assert second["quoteDemand"] == "idle"
    assert len(result["fills"]) == 1
    assert session.callback_calls >= 4
  finally:
    session.close()


def test_local_strategy_session_accepts_dataclass_quote_chunk() -> None:
  session = L2StrategySession({
    "scriptId": "local-chunk",
    "code": "def on_bar(bar, state):\n  return []\n\ndef on_quote(quote, state):\n  return []",
    "params": {},
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "latencyMs": 0,
    "passiveParticipationRate": 0.1,
    "holdingPolicy": "overnight",
    "symbol": "688981.SH",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "quoteMode": "continuous",
    "instrumentRules": {},
  })
  try:
    session.begin_day("2026-01-02")
    session.process_chunk_payload({
      "bars": [asdict(_bar())],
      "quotes": [asdict(_quote("2026-01-02 09:31:03"))],
      "trades": [],
    })
    session.finish_day("2026-01-02")
    session.result()

    assert session.callback_calls >= 1
  finally:
    session.close()


def test_job_lease_keeper_detects_lost_lease(monkeypatch: pytest.MonkeyPatch) -> None:
  observed = threading.Event()

  def lose_lease(_: dict[str, Any]) -> None:
    observed.set()
    raise L2DataError("L2 任务租约丢失或已取消")

  monkeypatch.setattr(settings, "L2_JOB_LEASE_SECONDS", 3)
  monkeypatch.setattr("backend.l2_runner._heartbeat", lose_lease)
  keeper = _JobLeaseKeeper({"runId": "run", "attemptToken": "token"})
  keeper.start()
  try:
    assert observed.wait(timeout=2)
    with pytest.raises(L2DataError, match="租约丢失"):
      keeper.ensure_active()
  finally:
    keeper.stop()


def test_event_adapter_only_queries_trade_rows(monkeypatch: pytest.MonkeyPatch) -> None:
  profile = SourceMappingProfile(
    source_name="event", table_name="quant.tick_data", trade_value="trade",
    columns={
      "symbol": "symbol", "trade_date": "trade_date", "time": "datetime", "sequence": "trade_id",
      "trade_id": "trade_id", "price": "price", "volume": "volume", "side": "side", "event_type": "type",
    },
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, Any]]:
    queries.append(query)
    if query.startswith("DESCRIBE"):
      return [{"name": item} for item in profile.columns.values()]
    return []

  monkeypatch.setattr(clickhouse_client, "query_rows", query_rows)

  assert list(ClickHouseL2Adapter(profile).iter_trades("688981.SH", "20260102", 100)) == []
  assert "AND type='trade'" in queries[-1]
