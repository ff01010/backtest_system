"""独立 Experiment 协调器进程入口。"""

from __future__ import annotations

import logging

from backend import settings
from backend.experiments.coordinator import ExperimentCoordinator


def main() -> None:
  logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
  )
  settings.validate_experiment_configuration()
  ExperimentCoordinator().run_forever()


if __name__ == "__main__":
  main()
