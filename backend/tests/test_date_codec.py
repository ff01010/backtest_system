from __future__ import annotations

import pytest

from backend.date_codec import TradingDateError, format_api_date, format_legacy_date, normalize_date_range


@pytest.mark.parametrize(("value", "expected"), [("20260102", "2026-01-02"), ("2026-01-02", "2026-01-02")])
def test_format_api_date_normalizes_supported_inputs(value: str, expected: str) -> None:
  assert format_api_date(value) == expected


def test_format_legacy_date_only_converts_at_legacy_boundary() -> None:
  assert format_legacy_date("2026-01-02") == "20260102"


def test_normalize_date_range_rejects_reverse_order() -> None:
  with pytest.raises(TradingDateError, match="开始日期"):
    normalize_date_range("2026-01-03", "20260102")
