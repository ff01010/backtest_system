import json
from pathlib import Path

import pytest

from backend.services.backtest_runner import _load_frozen_research_signals


def test_loads_one_frozen_signal_per_effective_trade_date(tmp_path: Path) -> None:
  artifact = tmp_path / "signals.ndjson"
  artifact.write_text(json.dumps({"availableAt": "2026-01-02T15:00:00+08:00", "effectiveAt": "2026-01-05T09:30:00+08:00", "action": "long"}) + "\n", encoding="utf-8")

  assert _load_frozen_research_signals(str(artifact)) == {"2026-01-05": "long"}


def test_rejects_multiple_signals_for_one_daily_consumption_time(tmp_path: Path) -> None:
  artifact = tmp_path / "signals.ndjson"
  artifact.write_text("\n".join([json.dumps({"effectiveAt": "2026-01-02T09:30:00+08:00", "action": "long"}), json.dumps({"effectiveAt": "2026-01-02T09:31:00+08:00", "action": "flat"})]), encoding="utf-8")

  with pytest.raises(ValueError, match="同日"):
    _load_frozen_research_signals(str(artifact))
