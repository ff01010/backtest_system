from typing import Any

from backend.services import state_store


class FakeCursor:
  def __init__(self) -> None:
    self.query = ""
    self.params: tuple[Any, ...] = ()

  def execute(self, query: str, params: tuple[Any, ...]) -> None:
    self.query = query
    self.params = params

  def fetchone(self) -> dict[str, int]:
    return {"id": 42}


def test_instrument_id_preserves_existing_name_when_name_is_missing() -> None:
  cursor = FakeCursor()

  instrument_id = state_store._instrument_id(cursor, "688981.SH")

  assert instrument_id == 42
  assert "WHEN %s <> '' THEN EXCLUDED.display_name" in cursor.query
  assert cursor.params == ("688981", "SH", "688981.SH", "1.688981", "")


def test_instrument_id_updates_existing_name_when_name_is_provided() -> None:
  cursor = FakeCursor()

  state_store._instrument_id(cursor, "688981.SH", "中芯国际")

  assert cursor.params == ("688981", "SH", "中芯国际", "1.688981", "中芯国际")


def test_instrument_search_matches_exact_symbol_with_exchange() -> None:
  cursor = FakeCursor()

  state_store._execute_instrument_search(cursor, "688981.SH", 20)

  assert "symbol=%s AND exchange=%s" in cursor.query
  assert cursor.params == ("688981", "SH", 20)


def test_empty_instrument_search_uses_a_bounded_default_query() -> None:
  cursor = FakeCursor()

  state_store._execute_instrument_search(cursor, "", 20)

  assert "OR" not in cursor.query
  assert "LIMIT %s" in cursor.query
  assert cursor.params == (20,)


def test_instrument_name_search_escapes_like_wildcards() -> None:
  cursor = FakeCursor()

  state_store._execute_instrument_search(cursor, r"A%_\B", 20)

  assert "lower(display_name) LIKE %s ESCAPE '\\'" in cursor.query
  assert cursor.params[1] == r"%a\%\_\\b%"
