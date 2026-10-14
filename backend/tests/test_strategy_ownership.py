from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from backend.services import state_store


class FakeCursor:
  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self.rows = rows
    self.statements: list[str] = []

  def execute(self, statement: str, _: Any = None) -> None:
    self.statements.append(" ".join(statement.split()))

  def fetchone(self) -> dict[str, Any]:
    return self.rows.pop(0)

  def __enter__(self) -> FakeCursor:
    return self

  def __exit__(self, *_: Any) -> None:
    return None


class FakeConnection:
  def __init__(self, cursor: FakeCursor) -> None:
    self.fake_cursor = cursor

  def cursor(self) -> FakeCursor:
    return self.fake_cursor


def test_script_fingerprint_covers_runtime_semantics_but_not_status() -> None:
  source = "def on_bar(bar, state):\n  return []"
  base = {"entryType": "on_bar", "signalRequired": False, "status": "active"}

  assert state_store._script_content_fingerprint(source, base) == (
    state_store._script_content_fingerprint(source, {**base, "status": "draft"})
  )
  assert state_store._script_content_fingerprint(source, base) != (
    state_store._script_content_fingerprint(source, {**base, "signalRequired": True})
  )
  assert state_store._script_content_fingerprint(source, base) != (
    state_store._script_content_fingerprint(source, {**base, "entryType": "on_quote"})
  )


def test_unchanged_script_reuses_immutable_version(monkeypatch: Any) -> None:
  payload = {
    "name": "策略 A",
    "description": "说明",
    "code": "def on_bar(bar, state):\n  return []",
    "entryType": "on_bar",
    "signalRequired": False,
  }
  fingerprint = state_store._script_content_fingerprint(
    payload["code"], state_store._script_runtime_config(payload),
  )
  cursor = FakeCursor([{
    "current_version_no": 3,
    "version_id": "92eed776-36a7-40cb-b5ce-a7830099a710",
    "content_fingerprint": fingerprint,
  }])
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  saved = state_store._write_strategy_script(cursor, "script-1", payload)

  assert saved == ("script-1", "92eed776-36a7-40cb-b5ce-a7830099a710", 3)
  assert not any("UPDATE user_strategy_version" in value for value in cursor.statements)
  assert not any("INSERT INTO user_strategy_version" in value for value in cursor.statements)


def test_runtime_change_appends_new_script_version(monkeypatch: Any) -> None:
  payload = {
    "name": "策略 A",
    "description": "说明",
    "code": "def on_bar(bar, state):\n  return []",
    "entryType": "on_bar",
    "signalRequired": True,
  }
  cursor = FakeCursor([
    {
      "current_version_no": 3,
      "version_id": "92eed776-36a7-40cb-b5ce-a7830099a710",
      "content_fingerprint": "0" * 64,
    },
    {"id": "ab68187d-825e-418b-8f0a-8f9403317d6a"},
  ])
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  saved = state_store._write_strategy_script(cursor, "script-1", payload)

  assert saved == ("script-1", "ab68187d-825e-418b-8f0a-8f9403317d6a", 4)
  assert sum("INSERT INTO user_strategy_version" in value for value in cursor.statements) == 1
  assert not any("UPDATE user_strategy_version" in value for value in cursor.statements)


def test_historical_script_content_reuses_immutable_version(monkeypatch: Any) -> None:
  payload = {
    "name": "策略 A",
    "code": "def on_bar(bar, state):\n  return []",
  }
  cursor = FakeCursor([{
    "current_version_no": 5,
    "version_id": "55555555-5555-4555-8555-555555555555",
    "content_fingerprint": "b" * 64,
    "matching_version_id": "22222222-2222-4222-8222-222222222222",
    "matching_version_no": 2,
    "max_version_no": 5,
  }])
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  saved = state_store._write_strategy_script(cursor, "script-1", payload)

  assert saved == ("script-1", "22222222-2222-4222-8222-222222222222", 2)
  assert not any("INSERT INTO user_strategy_version" in value for value in cursor.statements)


def test_new_content_uses_max_version_after_historical_restore(monkeypatch: Any) -> None:
  payload = {
    "name": "策略 A",
    "code": "def on_bar(bar, state):\n  return ['new']",
  }
  cursor = FakeCursor([
    {
      "current_version_no": 2,
      "version_id": "22222222-2222-4222-8222-222222222222",
      "content_fingerprint": "a" * 64,
      "matching_version_id": None,
      "matching_version_no": None,
      "max_version_no": 5,
    },
    {"id": "66666666-6666-4666-8666-666666666666"},
  ])
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  saved = state_store._write_strategy_script(cursor, "script-1", payload)

  assert saved == ("script-1", "66666666-6666-4666-8666-666666666666", 6)


@pytest.mark.parametrize(
  ("existing_names", "requested_name", "expected"),
  [
    ([], "策略 A", "策略 A"),
    (["策略 A"], "策略 A", "策略 A-1"),
    (["策略 A", "策略 A-1"], "策略 A", "策略 A-2"),
    (["A" * 64], "A" * 64, f"{'A' * 62}-1"),
  ],
)
def test_strategy_name_uses_first_available_numeric_suffix(
  monkeypatch: Any,
  existing_names: list[str],
  requested_name: str,
  expected: str,
) -> None:
  cursor = FakeCursor([{"names": existing_names}])
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  assert state_store._resolve_unique_strategy_name(cursor, requested_name, None) == expected
  assert any("pg_advisory_xact_lock" in value for value in cursor.statements)


def test_config_snapshot_binds_exact_script_version() -> None:
  config = {
    "id": "config-1",
    "name": "策略 A",
    "scriptId": "script-1",
    "strategyVersionId": "version-4",
    "description": "说明",
    "code": "def on_bar(bar, state):\n  return []",
    "entryType": "on_bar",
    "signalRequired": True,
    "strategy": "user_script",
    "mode": "backtest",
    "symbol": "688981.SH",
    "timeframe": "5m",
    "benchmarkSymbol": "688981.SH",
    "accountId": None,
    "paramsJson": {"params": {}},
  }

  snapshot = state_store._config_snapshot(config)

  assert snapshot["strategyVersionId"] == "version-4"
  assert snapshot["paramsJson"]["scriptSnapshot"]["versionId"] == "version-4"
  assert snapshot["paramsJson"]["scriptSnapshot"]["signalRequired"] is True


def test_complete_strategy_rejects_stale_revision(monkeypatch: Any) -> None:
  cursor = FakeCursor([{"strategy_id": "script-1", "revision": 4}])

  @contextmanager
  def database() -> Any:
    yield FakeConnection(cursor)

  monkeypatch.setattr(state_store, "_db", database)
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(state_store.ConflictError, match="其他会话"):
    state_store.save_complete_strategy(
      {"name": "策略 A", "code": "return []"},
      {"name": "策略 A"},
      "script-1",
      "config-1",
      expected_revision=3,
    )


def test_strategy_config_read_does_not_reveal_other_owner(monkeypatch: Any) -> None:
  cursor = FakeCursor([None])  # type: ignore[list-item]
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(KeyError, match="config-2"):
    state_store._get_strategy_config_with_cursor(
      cursor, "config-2", include_archived=True,
    )

  assert "c.owner_user_id=%s" in cursor.statements[0]


def test_archive_rejects_stale_revision_before_updates(monkeypatch: Any) -> None:
  cursor = FakeCursor([])
  monkeypatch.setattr(
    state_store,
    "_get_strategy_config_with_cursor",
    lambda *_args, **_kwargs: {"id": "config-1", "revision": 4},
  )

  with pytest.raises(state_store.RevisionConflictError):
    state_store.archive_strategy_config_in_transaction(cursor, "config-1", 3)

  assert cursor.statements == []


def test_archive_updates_config_and_owned_script(monkeypatch: Any) -> None:
  cursor = FakeCursor([{"strategy_id": "script-1"}])
  calls = 0

  def get_config(*_args: Any, **kwargs: Any) -> dict[str, Any]:
    nonlocal calls
    calls += 1
    return {
      "id": "config-1",
      "revision": 1 if calls == 1 else 2,
      "archivedAt": "2026-09-28T10:00:00+08:00" if kwargs.get("include_archived") else None,
    }

  monkeypatch.setattr(state_store, "_get_strategy_config_with_cursor", get_config)
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  result = state_store.archive_strategy_config_in_transaction(cursor, "config-1", 1)

  assert result["revision"] == 2
  sql = "\n".join(cursor.statements)
  assert "archived_at=now(), published=false, revision=revision + 1" in sql
  assert "UPDATE user_strategy SET status='archived'" in sql


def test_restore_rejects_active_name_conflict_without_renaming(monkeypatch: Any) -> None:
  cursor = FakeCursor([{"exists": 1}])
  monkeypatch.setattr(
    state_store,
    "_get_strategy_config_with_cursor",
    lambda *_args, **_kwargs: {
      "id": "config-1",
      "name": "策略 A",
      "revision": 2,
      "archivedAt": "2026-09-28T10:00:00+08:00",
    },
  )
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(state_store.NameConflictError, match="同名"):
    state_store.restore_strategy_config_in_transaction(cursor, "config-1", 2)

  sql = "\n".join(cursor.statements)
  assert "SET archived_at=NULL" not in sql


def test_restore_reactivates_config_and_owned_script(monkeypatch: Any) -> None:
  cursor = FakeCursor([None, {"strategy_id": "script-1"}])  # type: ignore[list-item]
  calls = 0

  def get_config(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    nonlocal calls
    calls += 1
    return {
      "id": "config-1",
      "name": "策略 A",
      "revision": calls + 1,
      "archivedAt": "2026-09-28T10:00:00+08:00" if calls == 1 else None,
    }

  monkeypatch.setattr(state_store, "_get_strategy_config_with_cursor", get_config)
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  result = state_store.restore_strategy_config_in_transaction(cursor, "config-1", 2)

  assert result["revision"] == 3
  sql = "\n".join(cursor.statements)
  assert "SET archived_at=NULL, published=true, revision=revision + 1" in sql
  assert "UPDATE user_strategy SET status='active'" in sql
