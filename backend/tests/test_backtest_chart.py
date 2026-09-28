import pytest
import pandas as pd
from fastapi import HTTPException

from backend import app as api_app
from backend.app import _backtest_chart_trades
from backend.services import data_repository, market_data
from backend.services.backtest_faults import BacktestFault


def test_backtest_chart_trades_only_returns_requested_date() -> None:
  run = {
    "id": "run-1",
    "resultJson": {
      "trades": [
        {
          "time": "202607300935",
          "signalTime": "202607300935",
          "executionTime": "202607300935",
          "action": "buy_open",
          "side": "BUY",
          "price": 12.34,
          "quantity": 100,
          "note": "entry",
        },
        {
          "time": "202607311000",
          "executionTime": "202607311000",
          "action": "sell_close",
          "side": "SELL",
          "price": 12.56,
          "quantity": 100,
          "note": "exit",
        },
      ],
    },
  }

  trades, unsupported_count = _backtest_chart_trades(run, "20260730")

  assert unsupported_count == 0
  assert trades == [{
    "id": "run-1-0",
    "executionTime": "202607300935",
    "signalTime": "202607300935",
    "action": "buy_open",
    "side": "BUY",
    "price": 12.34,
    "quantity": 100,
    "note": "entry",
  }]


def test_backtest_chart_trades_accepts_iso_date_for_l2_execution_time() -> None:
  run = {
    "id": "run-l2",
    "resultJson": {
      "trades": [{
        "signalTime": "2026-05-06 09:30:00.000",
        "executionTime": "2026-05-06 09:30:03.000",
        "action": "BUY_OPEN",
        "side": "BUY",
        "price": 21.44,
        "quantity": 200,
        "note": "L2 entry",
      }],
    },
  }

  trades, unsupported_count = _backtest_chart_trades(run, "2026-05-06")

  assert unsupported_count == 0
  assert trades == [{
    "id": "run-l2-0",
    "executionTime": "202605060930",
    "signalTime": "202605060930",
    "action": "buy_open",
    "side": "BUY",
    "price": 21.44,
    "quantity": 200,
    "note": "L2 entry",
  }]


def test_backtest_chart_trades_supports_legacy_buy_sell_actions() -> None:
  run = {
    "id": "run-2",
    "resultJson": {
      "trades": [{
        "time": "202607300935",
        "action": "BUY",
        "side": "BUY",
        "price": 12.34,
        "quantity": 100,
      }],
    },
  }

  trades, unsupported_count = _backtest_chart_trades(run, "20260730")

  assert unsupported_count == 0
  assert trades[0]["action"] == "buy"


def test_backtest_chart_trades_does_not_guess_missing_price() -> None:
  run = {
    "id": "run-3",
    "resultJson": {
      "trades": [{
        "time": "202607300935",
        "side": "BUY",
        "quantity": 100,
      }],
    },
  }

  trades, unsupported_count = _backtest_chart_trades(run, "20260730")

  assert trades == []
  assert unsupported_count == 1


def test_backtest_chart_trades_reports_enum_side_value_as_unsupported() -> None:
  run = {
    "id": "run-enum-side",
    "resultJson": {
      "trades": [{
        "time": "2026-07-30 09:35:00",
        "action": "BUY_OPEN",
        "side": "OrderSide.BUY",
        "price": 12.34,
        "quantity": 100,
      }],
    },
  }

  trades, unsupported_count = _backtest_chart_trades(run, "2026-07-30")

  assert trades == []
  assert unsupported_count == 1


def test_chart_bar_load_wraps_internal_market_error(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    market_data,
    "load_bars",
    lambda *_args: (_ for _ in ()).throw(
      market_data.MarketDataError("qmt_kline_5m_raw query timed out"),
    ),
  )

  with pytest.raises(BacktestFault) as captured:
    data_repository.load_chart_bars("688981.SH", "2026-07-30", "5m")

  assert captured.value.code == "data.bar_load_failed"
  assert isinstance(captured.value.__cause__, market_data.MarketDataError)


def test_chart_bar_conversion_wraps_invalid_market_row(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    market_data,
    "load_bars",
    lambda *_args: pd.DataFrame([{
      "time": "202607300930", "open": "invalid", "high": 10.2,
      "low": 9.9, "close": 10.1, "volume": 100,
    }]),
  )

  with pytest.raises(BacktestFault) as captured:
    data_repository.load_chart_bars("688981.SH", "2026-07-30", "5m")

  assert captured.value.code == "data.bar_load_failed"
  assert isinstance(captured.value.__cause__, ValueError)


def test_chart_api_returns_uniform_market_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    data_repository,
    "load_chart_data",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      BacktestFault(
        "data.bar_load_failed", "internal table detail",
        {"symbol": "688981.SH", "timeframe": "5m"},
      ),
    ),
  )

  with pytest.raises(HTTPException) as captured:
    api_app.chart("688981.SH", "2026-07-30", "5m", "user_script", "")

  assert captured.value.status_code == 503
  assert captured.value.detail["errorDetail"]["message"] == "行情数据异常，请联系管理员"
