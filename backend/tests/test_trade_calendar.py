from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from backend.services import trade_calendar


def test_parse_open_dates_reads_dates_from_index_klines() -> None:
  payload = {
    'data': {
      'klines': [
        '2026-08-26,3881.74,3912.52',
        '2026-08-27,3911.89,3916.01',
        'invalid',
      ],
    },
  }

  assert trade_calendar._parse_open_dates(payload) == ['2026-08-26', '2026-08-27']


def test_sync_trade_calendar_writes_all_calendar_days(monkeypatch: pytest.MonkeyPatch) -> None:
  writes: list[dict[str, Any]] = []

  def fetch_open_dates(start: date, end: date) -> list[str]:
    return [start.isoformat(), end.isoformat()]

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
  assert result['openDateCount'] == 4
  assert [(item['start'], item['end']) for item in writes] == [
    ('2025-12-31', '2025-12-31'), ('2026-01-01', '2026-01-02'),
  ]


def test_sync_trade_calendar_only_backfills_recent_dates_after_initialization(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  writes: list[tuple[str, str]] = []

  monkeypatch.setattr(trade_calendar.state_store, 'latest_trade_calendar_date', lambda *_: '2026-08-20')
  monkeypatch.setattr(trade_calendar, '_fetch_open_dates', lambda start, end: [start.isoformat()])
  monkeypatch.setattr(
    trade_calendar.state_store,
    'replace_trade_calendar',
    lambda _exchange, start, end, _open_dates, _source: writes.append((start, end)),
  )

  result = trade_calendar.sync_trade_calendar('2010-01-01', '2026-08-27')

  assert result['mode'] == 'incremental'
  assert result['syncedStartDate'] == '2026-08-14'
  assert writes == [('2026-08-14', '2026-08-27')]
