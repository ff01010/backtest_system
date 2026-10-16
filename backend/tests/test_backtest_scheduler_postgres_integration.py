from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
  id="20000000-0000-4000-8000-000000000092",
  username="scheduler-lock-owner",
  display_name="Scheduler Lock Owner",
  role="user",
)
ADMIN = CurrentUser(
  id="00000000-0000-0000-0000-000000000001",
  username="local-system",
  display_name="本机工作台",
  role="admin",
)


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


@pytest.fixture
def scheduler_owner() -> Iterator[CurrentUser]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_run WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("""
        INSERT INTO app_user (
          id, username, display_name, password_hash, role, status
        ) VALUES (%s, %s, %s, 'test-only', 'user', 'active')
        ON CONFLICT (id) DO UPDATE SET
          username=EXCLUDED.username,
          display_name=EXCLUDED.display_name,
          status='active'
      """, (OWNER.id, OWNER.username, OWNER.display_name))
  yield OWNER
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_run WHERE owner_user_id=%s", (OWNER.id,))
      cur.execute("DELETE FROM app_user WHERE id=%s", (OWNER.id,))


def _create_bar_job(owner: CurrentUser) -> tuple[str, int]:
  return _as_user(owner, lambda: state_store.create_bar_backtest_job({
    "symbol": "688981.SH",
    "timeframe": "5m",
    "fillModel": "bar_next_open",
  }))


def _create_l2_job(owner: CurrentUser) -> tuple[str, int]:
  return _as_user(owner, lambda: state_store.create_l2_backtest_job({
    "symbol": "688981.SH",
    "timeframe": "5m",
    "fillModel": "l2_quote",
  }))


def _claim_bar_job(worker_id: str) -> dict[str, Any] | None:
  return _as_user(
    ADMIN,
    lambda: state_store.claim_bar_backtest_job(worker_id, 30),
  )


def _claim_l2_job(worker_id: str) -> dict[str, Any] | None:
  return _as_user(
    ADMIN,
    lambda: state_store.claim_l2_backtest_job(worker_id, 30),
  )


def test_locked_bar_candidate_does_not_block_other_ready_jobs(
  scheduler_owner: CurrentUser,
) -> None:
  first_run_id, _ = _create_bar_job(scheduler_owner)
  second_run_id, _ = _create_bar_job(scheduler_owner)
  third_run_id, _ = _create_bar_job(scheduler_owner)
  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute(
    "SELECT run_id FROM bar_backtest_job WHERE run_id=%s FOR UPDATE",
    (first_run_id,),
  )
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=2) as executor:
    futures = {
      executor.submit(_claim_bar_job, "scheduler-lock-a:1"),
      executor.submit(_claim_bar_job, "scheduler-lock-b:2"),
    }
    try:
      completed, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "未锁定的排队任务应在空闲槽位中继续被领取"
      claimed = next(iter(completed)).result()
      assert claimed is not None
      assert claimed["runId"] in {second_run_id, third_run_id}
    finally:
      lock_conn.rollback()
      for future in futures:
        future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_locked_parent_run_does_not_block_other_ready_job(
  scheduler_owner: CurrentUser,
) -> None:
  locked_run_id, _ = _create_bar_job(scheduler_owner)
  ready_run_id, _ = _create_bar_job(scheduler_owner)
  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute(
    "SELECT id FROM backtest_run WHERE id=%s FOR UPDATE",
    (locked_run_id,),
  )
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_claim_bar_job, "scheduler-parent-lock:3")
    try:
      completed, _ = wait({future}, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "被锁定的运行记录不应阻塞其他任务领取"
      claimed = future.result()
      assert claimed is not None
      assert claimed["runId"] == ready_run_id
    finally:
      lock_conn.rollback()
      future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_locked_l2_candidate_does_not_block_other_ready_l2_job(
  scheduler_owner: CurrentUser,
) -> None:
  locked_run_id, _ = _create_l2_job(scheduler_owner)
  ready_run_id, _ = _create_l2_job(scheduler_owner)
  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute(
    "SELECT run_id FROM l2_backtest_job WHERE run_id=%s FOR UPDATE",
    (locked_run_id,),
  )
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_claim_l2_job, "scheduler-l2-lock:4")
    try:
      completed, _ = wait({future}, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "被锁定的 L2 队首不应阻塞其他 L2 任务领取"
      claimed = future.result()
      assert claimed is not None
      assert claimed["runId"] == ready_run_id
    finally:
      lock_conn.rollback()
      future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_locked_expired_cancellation_does_not_block_ready_job(
  scheduler_owner: CurrentUser,
) -> None:
  cancelling_run_id, _ = _create_bar_job(scheduler_owner)
  ready_run_id, _ = _create_bar_job(scheduler_owner)
  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute("""
    UPDATE bar_backtest_job
    SET status='cancelling', lease_expires_at=now() - interval '1 second'
    WHERE run_id=%s
  """, (cancelling_run_id,))
  lock_cur.execute(
    "UPDATE backtest_run SET status='cancelling' WHERE id=%s",
    (cancelling_run_id,),
  )
  lock_conn.commit()
  lock_cur.execute(
    "SELECT run_id FROM bar_backtest_job WHERE run_id=%s FOR UPDATE",
    (cancelling_run_id,),
  )
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_claim_bar_job, "scheduler-cleanup-lock:3")
    try:
      completed, _ = wait({future}, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "被锁定的过期取消任务不应阻塞其他任务领取"
      claimed = future.result()
      assert claimed is not None
      assert claimed["runId"] == ready_run_id
    finally:
      lock_conn.rollback()
      future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_locked_expired_slot_does_not_block_ready_job(
  scheduler_owner: CurrentUser,
) -> None:
  expired_run_id, _ = _create_bar_job(scheduler_owner)
  ready_run_id, _ = _create_bar_job(scheduler_owner)
  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute(
    "UPDATE bar_backtest_job SET status='cancelled' WHERE run_id=%s",
    (expired_run_id,),
  )
  lock_cur.execute(
    "UPDATE backtest_run SET status='cancelled' WHERE id=%s",
    (expired_run_id,),
  )
  lock_cur.execute("""
    INSERT INTO backtest_execution_slot (
      run_id, owner_user_id, job_kind, attempt_token, worker_id,
      is_burst, lease_expires_at
    ) VALUES (%s, %s, 'bar', %s, 'expired-slot:4', false,
      now() - interval '1 second')
  """, (
    expired_run_id,
    scheduler_owner.id,
    "40000000-0000-4000-8000-000000000004",
  ))
  lock_conn.commit()
  lock_cur.execute(
    "SELECT run_id FROM backtest_execution_slot WHERE run_id=%s FOR UPDATE",
    (expired_run_id,),
  )
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_claim_bar_job, "scheduler-slot-lock:4")
    try:
      completed, _ = wait({future}, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "被锁定的过期槽位不应阻塞其他任务领取"
      claimed = future.result()
      assert claimed is not None
      assert claimed["runId"] == ready_run_id
    finally:
      lock_conn.rollback()
      future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_claim_releases_scheduler_when_attempt_row_is_locked(
  scheduler_owner: CurrentUser,
) -> None:
  run_id, _ = _create_bar_job(scheduler_owner)
  claimed = _claim_bar_job("scheduler-expired-attempt:5")
  assert claimed is not None
  attempt_token = claimed["attemptToken"]

  lock_conn = _admin_connection()
  lock_cur = lock_conn.cursor()
  lock_cur.execute(
    "UPDATE bar_backtest_job SET lease_expires_at=now() - interval '1 second' WHERE run_id=%s",
    (run_id,),
  )
  lock_cur.execute(
    "UPDATE backtest_execution_slot SET lease_expires_at=now() + interval '30 seconds' WHERE run_id=%s",
    (run_id,),
  )
  lock_conn.commit()
  lock_cur.execute("""
    SELECT run_id FROM backtest_run_attempt
    WHERE run_id=%s AND attempt_token=%s
    FOR UPDATE
  """, (run_id, attempt_token))
  assert lock_cur.fetchone() is not None

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_claim_bar_job, "scheduler-timeout:6")
    try:
      completed, _ = wait({future}, timeout=1, return_when=FIRST_COMPLETED)
      assert completed, "内部行锁等待必须在一秒内释放全局调度锁"
      assert future.result() is None
    finally:
      lock_conn.rollback()
      future.result(timeout=5)
      lock_cur.close()
      lock_conn.close()


def test_concurrent_claims_do_not_exceed_global_capacity(
  scheduler_owner: CurrentUser,
) -> None:
  capacity = state_store.settings.BACKTEST_BASE_CONCURRENCY
  for _ in range(capacity + 4):
    _create_bar_job(scheduler_owner)

  worker_ids = [f"scheduler-capacity:{index}" for index in range(capacity * 2)]
  with ThreadPoolExecutor(max_workers=capacity * 2) as executor:
    results = list(executor.map(_claim_bar_job, worker_ids))

  claimed = [result for result in results if result is not None]
  assert len(claimed) == capacity
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT count(*) AS slot_count
        FROM backtest_execution_slot
        WHERE owner_user_id=%s AND lease_expires_at >= now()
      """, (scheduler_owner.id,))
      assert int(cur.fetchone()["slot_count"]) == capacity
