from backend.app import _backtest_chart_trades


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
