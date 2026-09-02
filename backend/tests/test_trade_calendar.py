from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from backend.services import trade_calendar


def test_parse_eastmoney_open_dates_reads_dates_from_index_klines() -> None:
  payload = {
    'data': {
      'klines': [
        '2026-08-26,3881.74,3912.52',
        '2026-08-27,3911.89,3916.01',
        'invalid',
      ],
    },
  }

  assert trade_calendar._parse_eastmoney_open_dates(payload) == ['2026-08-26', '2026-08-27']


def test_decode_sina_trading_dates_reads_compact_calendar_payload() -> None:
  payload = _sina_calendar_payload([139, 63, 1, 2], [12, 6, 18, 18], [False, False])

  assert trade_calendar._decode_sina_trading_dates(payload) == [
    date(1990, 12, 20), date(1990, 12, 21),
  ]


def test_parse_sina_open_dates_filters_requested_range(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    trade_calendar,
    '_decode_sina_trading_dates',
    lambda _: [date(2026, 8, 26), date(2026, 8, 27), date(2026, 9, 1)],
  )

  result = trade_calendar._parse_sina_open_dates(
    'var datelist="encoded";var KLC_TD_SH=datelist;', date(2026, 8, 27), date(2026, 8, 31),
  )

  assert result == ['2026-08-27']


def test_fetch_open_dates_uses_eastmoney_after_sina_failure(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    trade_calendar,
    '_fetch_sina_open_dates',
    lambda *_: (_ for _ in ()).throw(trade_calendar.TradeCalendarSyncError('unavailable')),
  )
  monkeypatch.setattr(trade_calendar, '_fetch_eastmoney_open_dates', lambda *_: ['2026-08-27'])

  dates, source = trade_calendar._fetch_open_dates(date(2026, 8, 27), date(2026, 8, 27))

  assert dates == ['2026-08-27']
  assert source == 'eastmoney_sse_index'


def test_with_retries_waits_before_the_successful_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
  attempts: list[int] = []
  waits: list[int] = []

  def load() -> list[str]:
    attempts.append(1)
    if len(attempts) < 3:
      raise trade_calendar.TradeCalendarSyncError('temporary failure')
    return ['2026-08-27']

  monkeypatch.setattr(trade_calendar.time, 'sleep', waits.append)

  assert trade_calendar._with_retries('新浪交易日历', load) == ['2026-08-27']
  assert len(attempts) == 3
  assert waits == [1, 2]


def test_sync_trade_calendar_writes_all_calendar_days(monkeypatch: pytest.MonkeyPatch) -> None:
  writes: list[dict[str, Any]] = []

  def fetch_open_dates(start: date, end: date) -> tuple[list[str], str]:
    return [start.isoformat(), end.isoformat()], 'sina_sse_calendar'

  def replace_calendar(
    exchange: str, start: str, end: str, open_dates: list[str], source: str,
  ) -> None:
    writes.append({'exchange': exchange, 'start': start, 'end': end, 'openDates': open_dates, 'source': source})

  monkeypatch.setattr(trade_calendar, '_fetch_open_dates', fetch_open_dates)
  monkeypatch.setattr(trade_calendar.state_store, 'replace_trade_calendar', replace_calendar)
  monkeypatch.setattr(trade_calendar.state_store, 'latest_trade_calendar_date', lambda *_: None)

  result = trade_calendar.sync_trade_calendar('2025-12-31', '2026-01-02')

  assert result['chunks'] == 2
  assert result['mode'] == 'full'
  assert result['syncedStartDate'] == '2025-12-31'
  assert result['openDateCount'] == 2
  assert [(item['start'], item['end']) for item in writes] == [
    ('2025-12-31', '2025-12-31'), ('2026-01-01', '2026-01-02'),
  ]


def test_sync_trade_calendar_only_backfills_recent_dates_after_initialization(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  writes: list[tuple[str, str]] = []

  monkeypatch.setattr(trade_calendar.state_store, 'latest_trade_calendar_date', lambda *_: '2026-08-20')
  monkeypatch.setattr(
    trade_calendar,
    '_fetch_open_dates',
    lambda start, end: ([start.isoformat()], 'sina_sse_calendar'),
  )
  monkeypatch.setattr(
    trade_calendar.state_store,
    'replace_trade_calendar',
    lambda _exchange, start, end, _open_dates, _source: writes.append((start, end)),
  )

  result = trade_calendar.sync_trade_calendar('2010-01-01', '2026-08-27')

  assert result['mode'] == 'incremental'
  assert result['syncedStartDate'] == '2026-08-14'
  assert writes == [('2026-08-14', '2026-08-27')]


def _sina_calendar_payload(values: list[int], widths: list[int], trailing_bits: list[bool]) -> str:
  bits: list[bool] = []
  for value, width in zip(values, widths, strict=True):
    bits.extend(bool(value & (1 << index)) for index in range(width))
  bits.extend(trailing_bits)
  while len(bits) % 6:
    bits.append(False)
  characters = []
  for offset in range(0, len(bits), 6):
    value = sum(1 << index for index, bit in enumerate(bits[offset:offset + 6]) if bit)
    characters.append(trade_calendar._SINA_ALPHABET[value])
  return ''.join(characters)
