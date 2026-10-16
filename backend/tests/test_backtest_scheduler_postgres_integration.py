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


@pytest.mark.xfail(
  strict=True,
  reason="锁定的队首任务当前会持有全局调度锁并阻塞其他任务领取",
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

  def claim(worker_id: str) -> dict[str, Any] | None:
    return _as_user(
      ADMIN,
      lambda: state_store.claim_bar_backtest_job(worker_id, 30),
    )

  with ThreadPoolExecutor(max_workers=2) as executor:
    futures = {
      executor.submit(claim, "scheduler-lock-a:1"),
      executor.submit(claim, "scheduler-lock-b:2"),
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
