# -*- coding: utf-8 -*-
"""分钟缓存覆盖状态测试。"""

from datetime import datetime, timedelta
from typing import Any

from backend.services import eastmoney_client, market_data


class _Cursor:
  def __init__(self) -> None:
    self.rows: list[tuple[Any, ...]] = []

  def executemany(self, _query: str, rows: list[tuple[Any, ...]]) -> None:
    self.rows = rows


def _bars(count: int) -> list[eastmoney_client.MinuteBar]:
  start = datetime(2026, 7, 27, 9, 30)
  return [
    eastmoney_client.MinuteBar(
      time=start + timedelta(minutes=index), open=10, high=10, low=10, close=10,
      volume=100, amount=1000,
    )
    for index in range(count)
  ]


def test_coverage_marks_complete_and_incomplete_days() -> None:
  cursor = _Cursor()
  bars = _bars(240) + [
    eastmoney_client.MinuteBar(
      time=datetime(2026, 7, 28, 9, 30), open=10, high=10, low=10, close=10,
      volume=100, amount=1000,
    ),
  ]

  market_data._update_coverage(
    cursor, 1, "1m", market_data._date_range("20260727", "20260728"), bars,
  )

  assert cursor.rows[0][5:7] == ("complete", 240)
  assert cursor.rows[1][5:7] == ("incomplete", 1)
