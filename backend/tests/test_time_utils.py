from datetime import datetime, timezone

from backend.time_utils import format_epoch_as_shanghai, format_shanghai_datetime, format_shanghai_iso_datetime


def test_database_timestamp_is_displayed_as_shanghai_time() -> None:
  value = datetime(2026, 8, 14, 6, 49, 15, tzinfo=timezone.utc)

  assert format_shanghai_datetime(value) == "2026-08-14 14:49:15"


def test_legacy_naive_timestamp_is_treated_as_utc() -> None:
  value = datetime(2026, 8, 14, 6, 49, 15)

  assert format_shanghai_datetime(value) == "2026-08-14 14:49:15"


def test_file_timestamp_and_metadata_include_shanghai_timezone() -> None:
  timestamp = datetime(2026, 8, 14, 6, 49, 15, tzinfo=timezone.utc).timestamp()

  assert format_epoch_as_shanghai(timestamp) == "2026-08-14 14:49:15"
  assert format_shanghai_iso_datetime(datetime(2026, 8, 14, 6, 49, 15, tzinfo=timezone.utc)) == "2026-08-14T14:49:15+08:00"
