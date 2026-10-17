import pytest

from backend.services.search_query import (
  SearchCursorError,
  SearchQueryError,
  backtest_search_bindings,
  decode_backtest_cursor,
  encode_backtest_cursor,
  parse_backtest_run_search,
)


def test_display_number_prefix_uses_exact_mode() -> None:
  spec = parse_backtest_run_search("#100405")

  assert spec.mode == "display_no"
  assert spec.display_no == 100405
  assert spec.text_pattern is None


def test_zero_padded_display_number_is_normalized() -> None:
  spec = parse_backtest_run_search("#01")

  assert spec.mode == "display_no"
  assert spec.normalized == "1"
  assert spec.display_no == 1


@pytest.mark.parametrize(
  "run_id",
  [
    "550E8400-E29B-41D4-A716-446655440000",
    "0193B4DC-8A5B-7CC3-923F-2957B0A88410",
  ],
)
def test_complete_uuid_uses_primary_key_mode(run_id: str) -> None:
  spec = parse_backtest_run_search(run_id)

  assert spec.mode == "uuid"
  assert spec.run_id == run_id.lower()


@pytest.mark.parametrize(
  ("query", "symbols"),
  [
    ("600519", ("600519.SH", "600519.SZ", "600519.BJ")),
    ("600519.sh", ("600519.SH",)),
  ],
)
def test_six_digit_input_keeps_display_number_and_symbol_candidates(
  query: str,
  symbols: tuple[str, ...],
) -> None:
  spec = parse_backtest_run_search(query)

  assert spec.mode == "symbol"
  assert spec.display_no == 600519
  assert spec.symbols == symbols
  assert spec.text_pattern is None


def test_other_digits_use_display_number_mode() -> None:
  spec = parse_backtest_run_search("12345")

  assert spec.mode == "display_no"
  assert spec.display_no == 12345


def test_text_search_escapes_like_wildcards_as_literals() -> None:
  spec = parse_backtest_run_search(r" A%_\B ")

  assert spec.mode == "text"
  assert spec.normalized == r"a%_\b"
  assert spec.text_pattern == r"%a\%\_\\b%"


def test_single_text_character_is_rejected() -> None:
  with pytest.raises(SearchQueryError, match="至少输入两个字符"):
    parse_backtest_run_search("候")


def test_cursor_is_bound_to_search_and_filters() -> None:
  first = parse_backtest_run_search("候选策略")
  bindings = backtest_search_bindings(
    first,
    symbol="688981.SH",
    config_id=None,
    favorites_only=False,
  )
  cursor = encode_backtest_cursor(
    "2026-10-17T10:00:00+08:00",
    "550e8400-e29b-41d4-a716-446655440000",
    bindings,
    "s" * 32,
  )

  assert decode_backtest_cursor(cursor, bindings, "s" * 32) == (
    "2026-10-17T10:00:00+08:00",
    "550e8400-e29b-41d4-a716-446655440000",
  )
  changed = backtest_search_bindings(
    parse_backtest_run_search("另一个条件"),
    symbol="688981.SH",
    config_id=None,
    favorites_only=False,
  )
  with pytest.raises(SearchCursorError, match="筛选条件不一致"):
    decode_backtest_cursor(cursor, changed, "s" * 32)
