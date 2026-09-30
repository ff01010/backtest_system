"""独立 Evaluation Worker 进程入口。"""

from __future__ import annotations

import argparse
import logging

import psycopg

from backend import settings
from backend.experiments.evaluation_worker import EvaluationWorker


def healthcheck() -> None:
  if not settings.DATABASE_URL:
    raise RuntimeError("未设置 TREND_GRID_DATABASE_URL")
  with psycopg.connect(settings.DATABASE_URL) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT 1 FROM backtest_experiment_evaluation_run LIMIT 1")


def main() -> None:
  parser = argparse.ArgumentParser(description="实验评价 Worker")
  parser.add_argument("--healthcheck", action="store_true")
  args = parser.parse_args()
  if args.healthcheck:
    healthcheck()
    return
  logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
  )
  settings.validate_experiment_configuration()
  EvaluationWorker().run_forever()


if __name__ == "__main__":
  main()
