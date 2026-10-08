"""独立 Evaluation Export Worker 进程入口。"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import psycopg

from backend import settings
from backend.experiments.export_worker import EvaluationExportWorker


def healthcheck() -> None:
  if not settings.DATABASE_URL:
    raise RuntimeError("未设置 TREND_GRID_DATABASE_URL")
  with psycopg.connect(settings.DATABASE_URL) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT 1 FROM backtest_experiment_evaluation_export LIMIT 1")
  root = Path(settings.OUTPUT_ROOT).resolve() / "evaluation-exports"
  root.mkdir(parents=True, exist_ok=True)
  probe = root / f".health-{os.getpid()}"
  probe.touch(exist_ok=False)
  probe.unlink()


def main() -> None:
  parser = argparse.ArgumentParser(description="实验评价导出 Worker")
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
  EvaluationExportWorker().run_forever()


if __name__ == "__main__":
  main()
