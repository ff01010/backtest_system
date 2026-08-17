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
