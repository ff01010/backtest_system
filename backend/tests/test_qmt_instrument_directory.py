from pathlib import Path

import pytest

from backend.services import qmt_instrument_directory


ROOT = Path(__file__).resolve().parents[2]


def test_sync_market_instruments_reads_unified_directory(monkeypatch: pytest.MonkeyPatch) -> None:
  rows = [{
    "symbol": "920001.BJ", "exchange": "BJSE", "name": "北交所测试股票",
    "type": "CS", "status": "Active", "board_type": "BJS",
    "listed_date": "2021-11-15", "de_listed_date": "1970-01-01",
    "source": "rqdatac",
  }]
  queries: list[str] = []
  received: list[dict[str, str | None]] = []
  monkeypatch.setattr(
    qmt_instrument_directory.clickhouse_client,
    "query_rows",
    lambda query: queries.append(query) or rows,
  )
  monkeypatch.setattr(
    qmt_instrument_directory.state_store,
    "replace_market_instruments_from_directory",
    lambda items: received.extend(items) or len(items),
  )

  assert qmt_instrument_directory.sync_market_instruments() == 1
  assert "FROM xl_derived.stock_directory FINAL" in queries[0]
  assert "WHERE type = 'CS' AND status = 'Active'" in queries[0]
  assert received == [{
    "symbol": "920001", "exchange": "BJ", "display_name": "北交所测试股票",
    "instrument_type": "stock", "board": "beijing", "list_date": "2021-11-15",
    "delist_date": None, "source": "rqdatac",
  }]


def test_sync_market_instruments_rejects_empty_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(qmt_instrument_directory.clickhouse_client, "query_rows", lambda _query: [])

  try:
    qmt_instrument_directory.sync_market_instruments()
  except RuntimeError as error:
    assert "没有活跃 A 股" in str(error)
  else:
    raise AssertionError("expected empty directory to fail")


def test_legacy_secid_migration_preserves_exchange_symbol_identity() -> None:
  migration = (
    ROOT / "db" / "migrations" / "038_market_instrument_legacy_secid.sql"
  ).read_text(encoding="utf-8")

  assert "DROP CONSTRAINT IF EXISTS market_instrument_eastmoney_secid_key" in migration
  assert "market_instrument_exchange_symbol_key" not in migration


@pytest.mark.parametrize(("exchange", "board_type", "expected_exchange", "expected_board"), [
  ("XSHG", "MainBoard", "SH", "main"),
  ("XSHE", "GEM", "SZ", "chinext"),
  ("XSHG", "KSH", "SH", "star"),
  ("BJSE", "BJS", "BJ", "beijing"),
])
def test_normalize_row_maps_directory_values(
  exchange: str,
  board_type: str,
  expected_exchange: str,
  expected_board: str,
) -> None:
  row = qmt_instrument_directory.normalize_row({
    "symbol": f"600001.{expected_exchange}", "exchange": exchange, "name": "测试股票",
    "board_type": board_type, "listed_date": None, "de_listed_date": None,
    "source": "rqdatac",
  })

  assert row["symbol"] == "600001"
  assert row["exchange"] == expected_exchange
  assert row["board"] == expected_board


def test_normalize_row_rejects_unknown_exchange() -> None:
  with pytest.raises(RuntimeError, match="未支持的交易所"):
    qmt_instrument_directory.normalize_row({
      "symbol": "600001.OTHER", "exchange": "OTHER", "name": "测试股票",
      "board_type": "MainBoard", "listed_date": None, "de_listed_date": None,
    })
