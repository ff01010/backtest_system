from backend.services import qmt_instrument_directory


def test_sync_market_instruments_reads_qmt_directory(monkeypatch):
  rows = [{
    "symbol": "600519", "exchange": "SH", "display_name": "贵州茅台",
    "instrument_type": "stock", "board": "main", "list_date": "2001-08-27",
    "delist_date": "1970-01-01",
  }]
  received = []
  monkeypatch.setattr(qmt_instrument_directory.clickhouse_client, "query_rows", lambda _query: rows)
  monkeypatch.setattr(
    qmt_instrument_directory.state_store,
    "replace_market_instruments_from_qmt",
    lambda items: received.extend(items) or len(items),
  )

  assert qmt_instrument_directory.sync_market_instruments() == 1
  assert received == [{
    "symbol": "600519", "exchange": "SH", "display_name": "贵州茅台",
    "instrument_type": "stock", "board": "main", "list_date": "2001-08-27",
    "delist_date": None,
  }]


def test_sync_market_instruments_rejects_empty_snapshot(monkeypatch):
  monkeypatch.setattr(qmt_instrument_directory.clickhouse_client, "query_rows", lambda _query: [])

  try:
    qmt_instrument_directory.sync_market_instruments()
  except RuntimeError as error:
    assert "目录为空" in str(error)
  else:
    raise AssertionError("expected empty directory to fail")
