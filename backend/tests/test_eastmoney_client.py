# -*- coding: utf-8 -*-
"""东财分钟数据适配器测试。"""

from backend.services import eastmoney_client


def test_parse_rows_normalizes_and_deduplicates() -> None:
  rows = [
    "2026-07-27 09:31,10,10.2,10.3,9.9,100,1000,0",
    "2026-07-27 09:30,9.8,10,10.1,9.7,90,900,0",
    "2026-07-27 09:31,10,10.2,10.3,9.9,100,1000,0",
    "2026-07-28 09:30,10,10.1,10.2,9.9,120,1200,0",
  ]

  bars = eastmoney_client._parse_rows(rows, "20260727", "20260727")

  assert [item.time.strftime("%H%M") for item in bars] == ["0930", "0931"]
  assert bars[1].amount == 1000


def test_parse_rows_rejects_invalid_ohlc() -> None:
  rows = ["2026-07-27 09:30,10,10.2,10.1,9.9,100,1000,0"]

  try:
    eastmoney_client._parse_rows(rows, "20260727", "20260727")
  except eastmoney_client.EastmoneyError as error:
    assert "OHLC" in str(error)
  else:
    raise AssertionError("应拒绝非法 OHLC")


def test_parse_rows_rejects_invalid_numeric_value() -> None:
  rows = ["2026-07-27 09:30,10,not-a-number,10.2,9.9,100,1000,0"]

  try:
    eastmoney_client._parse_rows(rows, "20260727", "20260727")
  except eastmoney_client.EastmoneyError as error:
    assert "数值非法" in str(error)
  else:
    raise AssertionError("应拒绝非法数值")


def test_parse_market_instrument_keeps_valid_shenzhen_and_shanghai_stocks() -> None:
  shanghai = eastmoney_client._parse_market_instrument({"f12": "600519", "f13": 1, "f14": "贵州茅台"})
  shenzhen = eastmoney_client._parse_market_instrument({"f12": "300750", "f13": 0, "f14": "宁德时代"})
  beijing = eastmoney_client._parse_market_instrument({"f12": "430047", "f13": 0, "f14": "诺思兰德"})

  assert shanghai is not None
  assert shanghai.exchange == "SH"
  assert shanghai.eastmoney_secid == "1.600519"
  assert shenzhen is not None
  assert shenzhen.exchange == "SZ"
  assert shenzhen.board == "chinext"
  assert beijing is not None
  assert beijing.exchange == "BJ"


def test_parse_market_instrument_skips_incomplete_rows() -> None:
  assert eastmoney_client._parse_market_instrument({"f12": "600519", "f13": 1, "f14": ""}) is None
