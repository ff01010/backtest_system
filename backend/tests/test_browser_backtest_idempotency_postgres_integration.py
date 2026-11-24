from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
import pytest

from backend.services import state_store
from backend.services.user_context import (
  CurrentUser,
  reset_current_user,
  set_current_user,
)


pytestmark = [
  pytest.mark.integration,
  pytest.mark.skipif(
    os.getenv("RUN_EXPERIMENT_DB_INTEGRATION") != "1",
    reason="需要已迁移的隔离 PostgreSQL",
  ),
]

OWNER = CurrentUser(
  id="20000000-0000-4000-8000-000000000131",
  username="browser-idempotency-owner",
  display_name="Browser Idempotency Owner",
  role="user",
)


def _admin_connection() -> psycopg.Connection[Any]:
  return psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"],
    row_factory=dict_row,
  )


def _as_user(user: CurrentUser, action: Callable[[], Any]) -> Any:
  token = set_current_user(user)
  try:
    return action()
  finally:
    reset_current_user(token)


def _payload() -> dict[str, Any]:
  return {
    "symbol": "688981.SH",
    "timeframe": "5m",
    "fillModel": "bar_next_open",
  }


@pytest.fixture
def browser_owner() -> Iterator[CurrentUser]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_run WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("DELETE FROM user_backtest_counter WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("""
        INSERT INTO app_user (
          id, username, display_name, password_hash, role, status
        ) VALUES (%s, %s, %s, 'test-only', 'user', 'active')
        ON CONFLICT (id) DO UPDATE SET status='active'
      """, (OWNER.id, OWNER.username, OWNER.display_name))
  yield OWNER
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_run WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("DELETE FROM user_backtest_counter WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("DELETE FROM app_user WHERE id=%s", (OWNER.id,))


def test_browser_backtest_uses_unique_request_key(
  browser_owner: CurrentUser,
) -> None:
  idempotency_key = "browser-postgres-unique"

  run_id, _display_no, replayed, status, _fill_model = _as_user(
    browser_owner,
    lambda: state_store.create_browser_backtest_job(
      _payload(), idempotency_key, "a" * 64,
    ),
  )

  assert replayed is False
  assert status == "queued"
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT request_key FROM backtest_run WHERE id=%s",
        (run_id,),
      )
      row = cur.fetchone()
  assert row is not None
  assert row["request_key"] == state_store._browser_backtest_request_key(
    idempotency_key,
  )


def test_concurrent_browser_retries_create_one_backtest(
  browser_owner: CurrentUser,
) -> None:
  idempotency_key = "browser-postgres-concurrent"

  def create() -> tuple[str, int, bool, str, str]:
    return _as_user(
      browser_owner,
      lambda: state_store.create_browser_backtest_job(
        _payload(), idempotency_key, "b" * 64,
      ),
    )

  with ThreadPoolExecutor(max_workers=2) as executor:
    results = list(executor.map(lambda _: create(), range(2)))

  assert results[0][0] == results[1][0]
  assert sorted(result[2] for result in results) == [False, True]
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT count(*) AS value FROM backtest_run WHERE owner_user_id=%s",
        (browser_owner.id,),
      )
      row = cur.fetchone()
  assert row is not None
  assert int(row["value"]) == 1


def test_legacy_browser_idempotency_key_still_replays(
  browser_owner: CurrentUser,
) -> None:
  idempotency_key = "browser-postgres-legacy"
  legacy_payload = {
    **_payload(),
    "_browserIdempotency": {
      "key": idempotency_key,
      "requestHash": "c" * 64,
    },
  }
  legacy_run_id, _display_no = _as_user(
    browser_owner,
    lambda: state_store.create_bar_backtest_job(legacy_payload),
  )

  result = _as_user(
    browser_owner,
    lambda: state_store.create_browser_backtest_job(
      _payload(), idempotency_key, "c" * 64,
    ),
  )

  assert result[0] == legacy_run_id
  assert result[2] is True


def test_request_key_lookup_uses_unique_index(
  browser_owner: CurrentUser,
) -> None:
  request_key = state_store._browser_backtest_request_key("browser-plan-check")
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO backtest_run (
          id, owner_user_id, display_no, status, requested_params, created_at
        )
        SELECT gen_random_uuid(), %s, value, 'succeeded', '{}'::jsonb,
          now() - make_interval(secs => value)
        FROM generate_series(1, 5000) AS value
      """, (browser_owner.id,))
      cur.execute("ANALYZE backtest_run")
      cur.execute("SET LOCAL enable_seqscan=off")
      cur.execute("""
        EXPLAIN (FORMAT JSON)
        SELECT id, display_no, status, fill_model
        FROM backtest_run
        WHERE owner_user_id=%s AND request_key=%s
        LIMIT 1
      """, (browser_owner.id, request_key))
      row = cur.fetchone()

  assert row is not None
  plan = json.dumps(row["QUERY PLAN"])
  assert "backtest_run_owner_user_id_request_key_key" in plan
  assert "ix_backtest_run_owner_created" not in plan
