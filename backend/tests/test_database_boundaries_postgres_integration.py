from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from backend.services import shared_backtest_copy, state_store
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

WRITER = CurrentUser(
  id="20000000-0000-4000-8000-000000000001",
  username="boundary-writer",
  display_name="Boundary Writer",
  role="user",
)
SOURCE = CurrentUser(
  id="20000000-0000-4000-8000-000000000002",
  username="boundary-source",
  display_name="Boundary Source",
  role="user",
)
RECIPIENT = CurrentUser(
  id="20000000-0000-4000-8000-000000000003",
  username="boundary-recipient",
  display_name="Boundary Recipient",
  role="user",
)
QUEUE_A = CurrentUser(
  id="20000000-0000-4000-8000-000000000004",
  username="boundary-queue-a",
  display_name="Boundary Queue A",
  role="user",
)
QUEUE_B = CurrentUser(
  id="20000000-0000-4000-8000-000000000005",
  username="boundary-queue-b",
  display_name="Boundary Queue B",
  role="user",
)
BASELINE = CurrentUser(
  id="20000000-0000-4000-8000-000000000006",
  username="boundary-baseline",
  display_name="Boundary Baseline",
  role="user",
)
PUBLIC_API_SUBMITTER = CurrentUser(
  id="20000000-0000-4000-8000-000000000007",
  username="boundary-public-api",
  display_name="Boundary Public API",
  role="user",
)
ADMIN = CurrentUser(
  id="00000000-0000-0000-0000-000000000001",
  username="local-system",
  display_name="本机工作台",
  role="admin",
)
API_KEY_ID = "20000000-0000-4000-8000-000000000010"
PUBLICATION_ID = "20000000-0000-4000-8000-000000000011"
INVALID_PUBLICATION_ID = "20000000-0000-4000-8000-000000000012"
PUBLIC_API_KEY_ID = "20000000-0000-4000-8000-000000000013"


def _as_user(user: CurrentUser, action: Callable[[], Any]) -> Any:
  token = set_current_user(user)
  try:
    return action()
  finally:
    reset_current_user(token)


def _admin_connection() -> psycopg.Connection[Any]:
  return psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"],
    row_factory=dict_row,
  )


@pytest.fixture(scope="module", autouse=True)
def database_facts() -> Iterator[None]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      for user in (
        WRITER, SOURCE, RECIPIENT, QUEUE_A, QUEUE_B, BASELINE,
        PUBLIC_API_SUBMITTER,
      ):
        cur.execute("""
          INSERT INTO app_user (
            id, username, display_name, password_hash, role, status
          ) VALUES (%s, %s, %s, 'test-only', 'user', 'active')
        """, (user.id, user.username, user.display_name))
      cur.execute("""
        INSERT INTO api_access_key (
          id, owner_user_id, name, key_prefix, secret_hash, scopes
        ) VALUES (%s, %s, 'integration-key', 'bt_test_boundary', %s, %s)
      """, (API_KEY_ID, WRITER.id, "1" * 64, Jsonb(["strategies:write"])))
      cur.execute("""
        INSERT INTO api_access_key (
          id, owner_user_id, name, key_prefix, secret_hash, scopes
        ) VALUES (%s, %s, 'public-api-submit', 'bt_test_submit', %s, %s)
      """, (
        PUBLIC_API_KEY_ID,
        PUBLIC_API_SUBMITTER.id,
        "2" * 64,
        Jsonb(["backtest:submit"]),
      ))
  yield


def _strategy_payloads(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
  return (
    {
      "name": name,
      "description": "数据库边界集成验证",
      "code": "def on_bar(bar, state):\n  return []\n",
      "entryType": "on_bar",
      "signalRequired": False,
    },
    {
      "name": name,
      "strategy": "user_script",
      "mode": "backtest",
      "symbol": "688981.SH",
      "timeframe": "5m",
      "benchmarkSymbol": "000300.SH",
      "accountId": None,
      "paramsJson": {"params": {"window": 20}},
      "enabled": True,
    },
  )


def _public_write(
  idempotency_key: str,
  request_hash: str,
  request_id: str,
  operation: Callable[[Any], tuple[str | None, int, dict[str, Any]]],
) -> tuple[dict[str, Any], bool, int]:
  return _as_user(WRITER, lambda: state_store.execute_public_write(
    API_KEY_ID,
    idempotency_key,
    request_hash,
    "strategy.create",
    "strategy",
    request_id,
    operation,
  ))


def _save_strategy(
  cur: Any,
  name: str,
  script_id: str | None = None,
) -> tuple[str, int, dict[str, Any]]:
  script_payload, config_payload = _strategy_payloads(name)
  result = state_store.save_complete_strategy_in_transaction(
    cur, script_payload, config_payload, script_id, None,
  )
  config = result["config"]
  return str(config["id"]), 201, {
    "configId": config["id"],
    "scriptId": config["scriptId"],
    "publishedVersionId": config["publishedVersionId"],
  }


def _saved_strategy_facts(config_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT
          (SELECT count(*) FROM strategy_config WHERE owner_user_id=%s) AS configs,
          (SELECT count(*) FROM user_strategy WHERE owner_user_id=%s) AS scripts,
          (SELECT count(*) FROM strategy_config_version version
            JOIN strategy_config config ON config.id=version.config_id
            WHERE config.owner_user_id=%s) AS config_versions,
          (SELECT count(*) FROM api_idempotency_record
            WHERE api_key_id=%s AND idempotency_key='strategy-save-12345678') AS records,
          (SELECT count(*) FROM api_access_audit
            WHERE api_key_id=%s AND request_id='request-success') AS audits
      """, (WRITER.id, WRITER.id, WRITER.id, API_KEY_ID, API_KEY_ID))
      counts = cur.fetchone()
      cur.execute("""
        SELECT published, strategy_id, strategy_version_id
        FROM strategy_config WHERE id=%s
      """, (config_id,))
      config = cur.fetchone()
  assert counts is not None
  assert config is not None
  return counts, config


def test_public_write_persists_and_replays_a_published_strategy_once() -> None:
  supplied_script_id = "20000000-0000-4000-8000-000000000099"
  saved_responses: list[tuple[str, int, dict[str, Any]]] = []

  def save(cur: Any) -> tuple[str, int, dict[str, Any]]:
    response = _save_strategy(cur, "幂等策略", supplied_script_id)
    saved_responses.append(response)
    return response

  first = _public_write(
    "strategy-save-12345678",
    "a" * 64,
    "request-success",
    save,
  )
  replay = _public_write(
    "strategy-save-12345678",
    "a" * 64,
    "request-replay",
    lambda _cur: pytest.fail("幂等重放不应再次执行策略保存"),
  )

  assert first == (replay[0], False, 201)
  assert replay == (first[0], True, 201)
  assert len(saved_responses) == 1
  assert first[0]["scriptId"] != supplied_script_id
  assert first[0]["publishedVersionId"]
  with pytest.raises(state_store.IdempotencyConflictError):
    _public_write(
      "strategy-save-12345678",
      "different-request-hash",
      "request-conflict",
      lambda _cur: pytest.fail("冲突请求不应进入资源写入"),
    )

  counts, config = _saved_strategy_facts(first[0]["configId"])
  assert counts == {
    "configs": 1,
    "scripts": 1,
    "config_versions": 1,
    "records": 1,
    "audits": 1,
  }
  assert config["published"] is True
  assert str(config["strategy_id"]) == first[0]["scriptId"]
  assert config["strategy_version_id"] is not None


def test_public_write_rolls_back_resource_and_idempotency_facts() -> None:
  def fail_after_save(cur: Any) -> tuple[str, int, dict[str, Any]]:
    _save_strategy(cur, "回滚策略")
    raise RuntimeError("write failed")

  with pytest.raises(RuntimeError, match="write failed"):
    _public_write(
      "strategy-rollback-12345678",
      "b" * 64,
      "request-rollback",
      fail_after_save,
    )

  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT
          (SELECT count(*) FROM user_strategy
            WHERE owner_user_id=%s AND name='回滚策略') AS rolled_back_scripts,
          (SELECT count(*) FROM api_idempotency_record
            WHERE api_key_id=%s AND idempotency_key='strategy-rollback-12345678')
            AS rolled_back_records
      """, (WRITER.id, API_KEY_ID))
      counts = cur.fetchone()

  assert counts == {
    "rolled_back_scripts": 0,
    "rolled_back_records": 0,
  }


def _submit_public_api_backtest(sequence: int) -> tuple[str, int, bool, str, str]:
  return _as_user(PUBLIC_API_SUBMITTER, lambda: state_store.create_api_backtest_job(
    {
      "symbol": "688981.SH",
      "timeframe": "5m",
      "fillModel": "bar_next_open",
      "strategy": "user_script",
    },
    PUBLIC_API_KEY_ID,
    f"public-backtest-{sequence:03d}",
    f"{sequence:064x}",
    f"request-{sequence:03d}",
  ))


def _finish_public_api_backtests(run_ids: list[str]) -> None:
  if not run_ids:
    return
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job SET status='succeeded', finished_at=now()
        WHERE run_id=ANY(%s::uuid[])
      """, (run_ids,))
      cur.execute("""
        UPDATE backtest_run SET status='succeeded', finished_at=now()
        WHERE id=ANY(%s::uuid[])
      """, (run_ids,))


def test_public_api_limits_inflight_without_daily_submission_quota(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(state_store.settings, "BACKTEST_RUNNER_AVAILABILITY_REQUIRED", False)
  run_ids: list[str] = []
  resumed_run_id: str | None = None
  try:
    for sequence in range(100):
      run_ids.append(_submit_public_api_backtest(sequence)[0])
    with pytest.raises(ValueError, match="排队、运行或取消中的回测已达上限"):
      _submit_public_api_backtest(100)

    _finish_public_api_backtests(run_ids)
    resumed = _submit_public_api_backtest(100)
    resumed_run_id = resumed[0]
    assert resumed[2:] == (False, "queued", "bar_next_open")
  finally:
    _finish_public_api_backtests(
      [*run_ids, *([resumed_run_id] if resumed_run_id else [])],
    )


def _queue_slots_and_finish_jobs() -> list[dict[str, Any]]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT owner_user_id, count(*) AS slots
        FROM backtest_execution_slot
        WHERE owner_user_id=ANY(%s::uuid[])
        GROUP BY owner_user_id ORDER BY owner_user_id
      """, ([QUEUE_A.id, QUEUE_B.id],))
      slots = cur.fetchall()
      for table in ("l2_backtest_job", "backtest_run"):
        cur.execute(f"""
          UPDATE {table} SET status='succeeded', finished_at=now()
          WHERE owner_user_id=ANY(%s::uuid[])
        """, ([QUEUE_A.id, QUEUE_B.id],))
      cur.execute("""
        DELETE FROM backtest_execution_slot
        WHERE owner_user_id=ANY(%s::uuid[])
      """, ([QUEUE_A.id, QUEUE_B.id],))
  return slots


def test_l2_scheduler_favors_the_user_with_fewer_active_slots() -> None:
  first_a = _as_user(QUEUE_A, lambda: state_store.create_l2_backtest_job({
    "symbol": "688981.SH", "timeframe": "5m", "fillModel": "l2_quote",
  }))
  _as_user(QUEUE_A, lambda: state_store.create_l2_backtest_job({
    "symbol": "688981.SH", "timeframe": "5m", "fillModel": "l2_quote",
  }))
  first_b = _as_user(QUEUE_B, lambda: state_store.create_l2_backtest_job({
    "symbol": "688981.SH", "timeframe": "5m", "fillModel": "l2_quote",
  }))

  claimed_a = _as_user(
    ADMIN,
    lambda: state_store.claim_l2_backtest_job("integration-l2-a:1", 30),
  )
  claimed_b = _as_user(
    ADMIN,
    lambda: state_store.claim_l2_backtest_job("integration-l2-b:2", 30),
  )

  assert claimed_a is not None
  assert claimed_b is not None
  assert claimed_a["runId"] == first_a[0]
  assert claimed_a["ownerUserId"] == QUEUE_A.id
  assert claimed_b["runId"] == first_b[0]
  assert claimed_b["ownerUserId"] == QUEUE_B.id

  slots = _queue_slots_and_finish_jobs()
  assert [(str(row["owner_user_id"]), row["slots"]) for row in slots] == [
    (QUEUE_A.id, 1),
    (QUEUE_B.id, 1),
  ]


def test_deployment_barrier_blocks_bar_and_l2_claims_but_not_queued_jobs() -> None:
  bar_run_id = _as_user(QUEUE_A, lambda: state_store.create_bar_backtest_job({
    "symbol": "688981.SH", "timeframe": "5m", "fillModel": "bar_next_open",
    "strategy": "user_script",
  }))[0]
  l2_run_id = _as_user(QUEUE_B, lambda: state_store.create_l2_backtest_job({
    "symbol": "688981.SH", "timeframe": "5m", "fillModel": "l2_quote",
  }))[0]

  with _admin_connection() as barrier_connection:
    barrier_connection.execute(
      "SELECT pg_advisory_lock(%s)",
      (state_store._BACKTEST_DEPLOYMENT_BARRIER_LOCK_KEY,),
    )
    assert _as_user(
      ADMIN, lambda: state_store.claim_bar_backtest_job("barrier-bar:1", 30),
    ) is None
    assert _as_user(
      ADMIN, lambda: state_store.claim_l2_backtest_job("barrier-l2:2", 30),
    ) is None
    with _admin_connection() as observer:
      statuses = observer.execute(
        "SELECT id, status FROM backtest_run WHERE id=ANY(%s::uuid[]) ORDER BY id",
        ([bar_run_id, l2_run_id],),
      ).fetchall()
    assert {str(row["id"]): row["status"] for row in statuses} == {
      bar_run_id: "queued",
      l2_run_id: "queued",
    }
    # 不主动 unlock，验证发布辅助连接异常断开时 PostgreSQL 会自动释放会话锁。

  assert _as_user(
    ADMIN, lambda: state_store.claim_bar_backtest_job("barrier-bar:1", 30),
  )["runId"] == bar_run_id
  assert _as_user(
    ADMIN, lambda: state_store.claim_l2_backtest_job("barrier-l2:2", 30),
  )["runId"] == l2_run_id
  _queue_slots_and_finish_jobs()


def _publication_snapshot(
  title: str,
  benchmark_symbol: str = "000300.SH",
) -> dict[str, Any]:
  return {
    "strategy": {
      "description": "共享策略说明",
      "code": "def on_bar(bar, state):\n  return []\n",
      "entryType": "on_bar",
      "signalRequired": False,
    },
    "configuration": {
      "strategy": "user_script",
      "symbol": "688981.SH",
      "timeframe": "5m",
      "benchmarkSymbol": benchmark_symbol,
      "startDate": "2026-01-01",
      "endDate": "2026-01-31",
      "initialCapital": 1_000_000,
      "commissionRate": 0.0003,
      "slippage": 0.0001,
      "strategyCategory": "directional_holding",
      "params": {"window": 20},
      "title": title,
    },
  }


def _insert_publication(
  publication_id: str,
  title: str,
  snapshot: dict[str, Any],
  digest: str,
) -> None:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT set_config('app.current_user_id', %s, true)",
        (SOURCE.id,),
      )
      cur.execute(
        "SELECT set_config('app.current_is_admin', 'true', true)",
      )
      cur.execute("""
        INSERT INTO backtest_publication (
          id, owner_user_id, author_name_snapshot, title, description, tags,
          status, snapshot_schema_version, snapshot_json, snapshot_digest,
          symbol, timeframe, strategy_category, start_date, end_date
        ) VALUES (
          %s, %s, %s, %s, '集成测试共享策略', '[]'::jsonb,
          'active', 1, %s, %s, '688981.SH', '5m',
          'directional_holding', '2026-01-01', '2026-01-31'
        )
      """, (
        publication_id, SOURCE.id, SOURCE.display_name, title,
        Jsonb(snapshot), digest,
      ))


def _shared_copy_facts() -> dict[str, Any]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT publication.copy_count,
          count(copy.id) AS copy_rows,
          count(config.id) AS config_rows,
          min(config.owner_user_id::text) AS config_owner,
          min(config.source_publication_id::text) AS source_publication
        FROM backtest_publication publication
        LEFT JOIN backtest_publication_copy copy
          ON copy.publication_id=publication.id
        LEFT JOIN strategy_config config
          ON config.id=copy.strategy_config_id
        WHERE publication.id=%s
        GROUP BY publication.copy_count
      """, (PUBLICATION_ID,))
      row = cur.fetchone()
  assert row is not None
  return row


def test_shared_copy_is_independent_and_idempotent() -> None:
  _insert_publication(
    PUBLICATION_ID,
    "均线策略",
    _publication_snapshot("均线策略"),
    "c" * 64,
  )
  first = _as_user(
    RECIPIENT,
    lambda: shared_backtest_copy.copy_shared_backtest(PUBLICATION_ID),
  )
  replay = _as_user(
    RECIPIENT,
    lambda: shared_backtest_copy.copy_shared_backtest(PUBLICATION_ID),
  )

  assert first["created"] is True
  assert replay == {**first, "created": False}
  assert first["accountCleared"] is True
  assert first["sharedSource"] == {
    "publicationId": PUBLICATION_ID,
    "authorName": SOURCE.display_name,
    "title": "均线策略",
  }

  assert _shared_copy_facts() == {
    "copy_count": 1,
    "copy_rows": 1,
    "config_rows": 1,
    "config_owner": RECIPIENT.id,
    "source_publication": PUBLICATION_ID,
  }


def test_shared_copy_failure_rolls_back_strategy_and_relation() -> None:
  _insert_publication(
    INVALID_PUBLICATION_ID,
    "坏快照",
    _publication_snapshot("坏快照", benchmark_symbol="invalid"),
    "d" * 64,
  )
  with pytest.raises(ValueError):
    _as_user(
      RECIPIENT,
      lambda: shared_backtest_copy.copy_shared_backtest(INVALID_PUBLICATION_ID),
    )

  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT publication.copy_count,
          (SELECT count(*) FROM backtest_publication_copy copy
            WHERE copy.publication_id=publication.id) AS copy_rows,
          (SELECT count(*) FROM user_strategy strategy
            WHERE strategy.owner_user_id=%s AND strategy.name='坏快照-副本')
            AS rolled_back_scripts
        FROM backtest_publication publication
        WHERE publication.id=%s
      """, (RECIPIENT.id, INVALID_PUBLICATION_ID))
      rolled_back = cur.fetchone()

  assert rolled_back == {
    "copy_count": 0,
    "copy_rows": 0,
    "rolled_back_scripts": 0,
  }


def test_complete_version_migration_and_update_preserve_real_baselines() -> None:
  script_payload, config_payload = _strategy_payloads("完整版本基线")
  saved = _as_user(
    BASELINE,
    lambda: state_store.save_complete_strategy(
      script_payload, config_payload, None, None,
    ),
  )
  original_config = saved["config"]
  config_id = original_config["id"]
  script_id = original_config["scriptId"]
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM strategy_config_version WHERE config_id=%s", (config_id,))

  migration = Path(
    "db/migrations/057_strategy_complete_version_baseline.sql",
  ).read_text(encoding="utf-8")
  for _ in range(2):
    with psycopg.connect(
      os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], autocommit=True,
    ) as conn:
      conn.execute("SET ROLE quant_backtest_owner")
      conn.execute(
        "SELECT set_config('app.current_user_id', "
        "'00000000-0000-0000-0000-000000000001', false)",
      )
      conn.execute(
        "SELECT set_config('app.current_is_admin', 'true', false)",
      )
      conn.execute(migration)

  migrated_config = _as_user(
    BASELINE, lambda: state_store.get_strategy_config(config_id),
  )
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT version_no, strategy_version_id, snapshot
        FROM strategy_config_version WHERE config_id=%s
      """, (config_id,))
      rows = cur.fetchall()
  assert len(rows) == 1
  assert rows[0]["version_no"] == 1
  assert str(rows[0]["strategy_version_id"]) == migrated_config["strategyVersionId"]
  assert rows[0]["snapshot"] == state_store._config_snapshot(migrated_config)

  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM strategy_config_version WHERE config_id=%s", (config_id,))
  changed_script = {
    **script_payload,
    "code": "def on_bar(bar, state):\n  return [{'action': 'buy', 'quantity': 100}]\n",
  }
  updated = _as_user(
    BASELINE,
    lambda: state_store.save_complete_strategy(
      changed_script,
      config_payload,
      script_id,
      config_id,
      expected_revision=migrated_config["revision"],
    ),
  )

  assert updated["config"]["id"] == config_id
  assert updated["config"]["scriptId"] == script_id
  assert updated["config"]["versionNo"] == 2
  versions = _as_user(
    BASELINE, lambda: state_store.list_strategy_config_versions(config_id),
  )
  assert [item["versionNo"] for item in versions] == [2, 1]
  assert versions[1]["paramsJson"]["scriptSnapshot"]["code"] == script_payload["code"]
  assert versions[0]["paramsJson"]["scriptSnapshot"]["code"] == changed_script["code"]
