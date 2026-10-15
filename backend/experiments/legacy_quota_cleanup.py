"""rolling_window_v1 激活后的旧计划额度幂等清理作业。"""

from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend import settings
from backend.services import state_store


def cleanup_legacy_plan_reservations(
  *,
  apply: bool,
  build_id: str,
  actor: str,
) -> dict[str, Any]:
  with _session() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("experiment-legacy-quota-cleanup-v1",),
      )
      cur.execute("""
        SELECT contract_epoch FROM backtest_experiment_rollout_state
        WHERE singleton FOR SHARE
      """)
      epoch = cur.fetchone()
      if epoch is None or epoch["contract_epoch"] != "rolling_window_v1":
        raise RuntimeError("只有 rolling_window_v1 激活后才能清理旧计划额度")
      cur.execute("""
        SELECT count(*) AS user_count,
          COALESCE(sum(reserved_planned_trial_count),0) AS reservation_count
        FROM backtest_experiment_user_quota
        WHERE reserved_planned_trial_count<>0
      """)
      before = cur.fetchone()
      user_count = int(before["user_count"])
      reservation_count = int(before["reservation_count"])
      if apply:
        cur.execute("""
          UPDATE backtest_experiment_user_quota
          SET reserved_planned_trial_count=0,
            row_version=row_version+1, updated_at=now()
          WHERE reserved_planned_trial_count<>0
        """)
      cur.execute("""
        INSERT INTO backtest_experiment_legacy_cleanup_audit (
          applied, build_id, actor, cleared_user_count,
          cleared_reservation_count
        ) VALUES (%s,%s,%s,%s,%s)
      """, (apply, build_id, actor, user_count, reservation_count))
  return {
    "cleanupVersion": "experiment_legacy_quota_cleanup_v1",
    "applied": apply,
    "clearedUserCount": user_count if apply else 0,
    "clearedReservationCount": reservation_count if apply else 0,
    "pendingUserCount": 0 if apply else user_count,
    "pendingReservationCount": 0 if apply else reservation_count,
  }


def _session() -> psycopg.Connection[Any]:
  if not settings.DATABASE_URL:
    raise state_store.DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    conn = psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)
    with conn.cursor() as cur:
      cur.execute(
        "SELECT set_config('app.current_user_id', %s, true)",
        ("00000000-0000-0000-0000-000000000000",),
      )
      cur.execute("SELECT set_config('app.current_is_admin', 'true', true)")
    return conn
  except psycopg.Error as error:
    raise state_store.DatabaseError(str(error)) from error


def main() -> None:
  parser = argparse.ArgumentParser(description="清理旧实验计划额度预留")
  parser.add_argument("--apply", action="store_true")
  parser.add_argument("--build-id", required=True)
  parser.add_argument("--actor", required=True)
  args = parser.parse_args()
  print(json.dumps(cleanup_legacy_plan_reservations(
    apply=args.apply,
    build_id=args.build_id,
    actor=args.actor,
  ), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
  main()
