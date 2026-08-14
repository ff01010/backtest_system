import pytest

from backend.strategies.script_time_context import ScriptTimeContextError, normalize_script_bar_time


@pytest.mark.parametrize(
  ("raw_time", "expected_time", "expected_datetime", "expected_date", "expected_hhmm"),
  [
    ("202607270930", "20260727093000", "2026-07-27 09:30:00", "2026-07-27", 930),
    ("20260727093000", "20260727093000", "2026-07-27 09:30:00", "2026-07-27", 930),
    ("2026-07-27 09:30", "20260727093000", "2026-07-27 09:30:00", "2026-07-27", 930),
    ("2026-07-27 09:30:00", "20260727093000", "2026-07-27 09:30:00", "2026-07-27", 930),
    ("2026-05-06 09:30:00.000", "20260506093000", "2026-05-06 09:30:00", "2026-05-06", 930),
    ("2026-07-27 09:30:00.123456", "20260727093000", "2026-07-27 09:30:00", "2026-07-27", 930),
  ],
)
def test_normalize_script_bar_time_uses_one_intraday_contract(
  raw_time: str,
  expected_time: str,
  expected_datetime: str,
  expected_date: str,
  expected_hhmm: int,
) -> None:
  context = normalize_script_bar_time(raw_time)

  assert context.time == expected_time
  assert context.datetime == expected_datetime
  assert context.trade_date == expected_date
  assert context.hhmm == expected_hhmm


def test_normalize_script_bar_time_keeps_daily_contract_explicit() -> None:
  context = normalize_script_bar_time("2026-07-27")

  assert context.time == "20260727"
  assert context.datetime == "2026-07-27"
  assert context.trade_date == "2026-07-27"
  assert context.hhmm is None


@pytest.mark.parametrize("raw_time", ["202607279930", "2026-07-27 09:61:00", "2026-07-27T09:30:00", ""])
def test_normalize_script_bar_time_rejects_unknown_format(raw_time: str) -> None:
  with pytest.raises(ScriptTimeContextError, match="支持格式"):
    normalize_script_bar_time(raw_time)
