# -*- coding: utf-8 -*-
"""东财分钟数据适配器测试。"""

from http.client import RemoteDisconnected

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


def test_fetch_a_share_instruments_reads_all_pages(monkeypatch) -> None:
  pages = {
    1: {"data": {"total": 3, "diff": [
      {"f12": "600519", "f13": 1, "f14": "贵州茅台"},
      {"f12": "300750", "f13": 0, "f14": "宁德时代"},
    ]}},
    2: {"data": {"total": 3, "diff": [
      {"f12": "430047", "f13": 0, "f14": "诺思兰德"},
    ]}},
  }

  monkeypatch.setattr(eastmoney_client, "_fetch_market_directory_page", lambda page, timeout: pages[page])

  items = eastmoney_client.fetch_a_share_instruments()

  assert [item.exchange for item in items] == ["SZ", "BJ", "SH"]


def test_market_directory_request_retries_after_connection_drop(monkeypatch) -> None:
  attempts = 0

  class Response:
    def __enter__(self) -> "Response":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    def read(self) -> bytes:
      return b'{"data": {"total": 0, "diff": []}}'

  def fake_urlopen(*_args: object, **_kwargs: object) -> Response:
    nonlocal attempts
    attempts += 1
    if attempts < 3:
      raise RemoteDisconnected("temporary disconnect")
    return Response()

  monkeypatch.setattr(eastmoney_client, "urlopen", fake_urlopen)
  monkeypatch.setattr(eastmoney_client.time, "sleep", lambda _seconds: None)

  payload = eastmoney_client._fetch_json("https://example.test", 1)

  assert payload["data"]["total"] == 0
  assert attempts == 3
