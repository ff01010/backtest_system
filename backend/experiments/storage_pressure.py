"""聚合数据库逻辑占用与卷剩余空间的存储压力门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Protocol
from urllib import request as urlrequest

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.canonicalization import digest, with_version
from backend.services import state_store


@dataclass(frozen=True)
class StorageObservation:
  name: str
  value: int
  total: int | None
  observed_at: datetime


@dataclass(frozen=True)
class StorageDecision:
  status: str
  reasons: tuple[str, ...]
  missing_signals: tuple[str, ...]
  observed_at: datetime | None
  signals: dict[str, dict[str, Any]]

  @property
  def allows_new_work(self) -> bool:
    return self.status == "open"

  def as_dict(self) -> dict[str, Any]:
    return {
      "status": self.status,
      "reasons": list(self.reasons),
      "missingSignals": list(self.missing_signals),
      "observedAt": self.observed_at.isoformat() if self.observed_at else None,
      "signals": self.signals,
    }


class CapacityProbe(Protocol):
  def observe(self, now: datetime) -> tuple[StorageObservation, ...]:
    """读取卷容量信号。"""


class StoragePressureRepository(Protocol):
  def postgres_logical_size(self, now: datetime) -> StorageObservation:
    """读取当前数据库逻辑大小。"""

  def load(self) -> dict[str, Any]:
    """读取上一次持久化决定。"""

  def save(
    self,
    decision: StorageDecision,
    threshold_version: str,
  ) -> None:
    """持久化多个协调器共享的门禁结论。"""


class HttpCapacityProbe:
  def __init__(self, base_url: str | None = None) -> None:
    self._base_url = (base_url or settings.EXPERIMENT_STORAGE_PROBE_URL).rstrip("/")

  def observe(self, _now: datetime) -> tuple[StorageObservation, ...]:
    if not self._base_url:
      return ()
    try:
      with urlrequest.urlopen(f"{self._base_url}/v1/capacity", timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
      return ()
    observed_at = _parse_time(payload.get("observedAt"))
    volumes = payload.get("volumes")
    if observed_at is None or not isinstance(volumes, dict):
      return ()
    observations: list[StorageObservation] = []
    for name in ("postgres_volume_free", "artifact_volume_free"):
      item = volumes.get(name)
      if not isinstance(item, dict):
        continue
      available = item.get("availableBytes")
      total = item.get("totalBytes")
      if not _is_nonnegative_int(available) or not _is_positive_int(total):
        continue
      observations.append(StorageObservation(name, available, total, observed_at))
    return tuple(observations)


class PostgresStoragePressureRepository:
  def postgres_logical_size(self, now: datetime) -> StorageObservation:
    with self._session() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database()) AS value")
        value = int(cur.fetchone()["value"])
    return StorageObservation("postgres_logical", value, None, now)

  def load(self) -> dict[str, Any]:
    with self._session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT decision, signals, reasons, observed_at,
            threshold_version, decision_changed_at
          FROM backtest_experiment_storage_pressure
          WHERE singleton
        """)
        row = cur.fetchone()
    return dict(row) if row else {}

  def save(
    self,
    decision: StorageDecision,
    threshold_version: str,
  ) -> None:
    with self._session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_storage_pressure
          SET decision=%s, signals=%s, reasons=%s, threshold_version=%s,
            observed_at=%s,
            decision_changed_at=CASE
              WHEN decision<>%s THEN now() ELSE decision_changed_at END,
            updated_at=now()
          WHERE singleton
        """, (
          decision.status, Jsonb(decision.signals),
          Jsonb([*decision.reasons, *decision.missing_signals]),
          threshold_version, decision.observed_at, decision.status,
        ))

  @staticmethod
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


class StoragePressureGate:
  def __init__(
    self,
    *,
    repository: StoragePressureRepository | None = None,
    probe: CapacityProbe | None = None,
  ) -> None:
    self._repository = repository or PostgresStoragePressureRepository()
    self._probe = probe or HttpCapacityProbe()

  def evaluate(self, now: datetime | None = None) -> StorageDecision:
    observed_now = now or datetime.now(timezone.utc)
    config, missing = _configuration()
    previous = self._repository.load()
    observations: dict[str, StorageObservation] = {}
    if not missing:
      try:
        logical = self._repository.postgres_logical_size(observed_now)
        observations[logical.name] = logical
      except state_store.DatabaseError:
        missing.append("postgres_logical")
      for item in self._probe.observe(observed_now):
        observations[item.name] = item
    expected = (
      "postgres_logical", "postgres_volume_free", "artifact_volume_free",
    )
    previous_signals = dict(previous.get("signals") or {})
    signals: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    max_age = config.get("maxAgeSeconds")
    for name in expected:
      observation = observations.get(name)
      if observation is None:
        if name not in missing:
          missing.append(name)
        continue
      age = max(0.0, (observed_now - observation.observed_at).total_seconds())
      if max_age is None or age > max_age:
        missing.append(f"{name}:stale")
        continue
      previous_state = str(previous_signals.get(name, {}).get("state") or "open")
      state, thresholds = _signal_state(name, observation.value, previous_state, config)
      if state == "pressured":
        reasons.append(name)
      signals[name] = {
        "value": observation.value,
        "total": observation.total,
        "observedAt": observation.observed_at.isoformat(),
        "ageSeconds": age,
        "state": state,
        **thresholds,
      }
    status = "pressured" if reasons else "unknown" if missing else "open"
    observed_at = min(
      (item.observed_at for item in observations.values()), default=None,
    )
    decision = StorageDecision(
      status=status,
      reasons=tuple(sorted(set(reasons))),
      missing_signals=tuple(sorted(set(missing))),
      observed_at=observed_at,
      signals=signals,
    )
    self._repository.save(decision, digest(with_version(config)))
    return decision


def health_snapshot(now: datetime | None = None) -> dict[str, Any]:
  """返回最近一次共享决定；健康接口不主动访问宿主卷。"""
  observed_now = now or datetime.now(timezone.utc)
  config, configuration_errors = _configuration()
  try:
    stored = PostgresStoragePressureRepository().load()
  except state_store.DatabaseError as error:
    return {
      "status": "unknown",
      "allowsNewWork": False,
      "configurationErrors": configuration_errors,
      "reasons": ["storage_pressure_state_unavailable"],
      "signals": {},
      "error": str(error),
    }
  signals = dict(stored.get("signals") or {})
  stale: list[str] = []
  max_age = config.get("maxAgeSeconds")
  for name, signal in signals.items():
    observed_at = _parse_time(dict(signal).get("observedAt"))
    if observed_at is None or max_age is None:
      stale.append(str(name))
      continue
    if (observed_now - observed_at).total_seconds() > max_age:
      stale.append(str(name))
  status = str(stored.get("decision") or "unknown")
  if configuration_errors or stale:
    status = "unknown"
  return {
    "status": status,
    "allowsNewWork": status == "open",
    "configurationErrors": configuration_errors,
    "staleSignals": sorted(stale),
    "reasons": list(stored.get("reasons") or []),
    "observedAt": (
      stored["observed_at"].isoformat() if stored.get("observed_at") else None
    ),
    "decisionChangedAt": (
      stored["decision_changed_at"].isoformat()
      if stored.get("decision_changed_at") else None
    ),
    "thresholdVersion": stored.get("threshold_version"),
    "signals": signals,
  }


def _configuration() -> tuple[dict[str, int], list[str]]:
  values = {
    "logicalHigh": settings.EXPERIMENT_POSTGRES_LOGICAL_HIGH_WATER_BYTES,
    "logicalLow": settings.EXPERIMENT_POSTGRES_LOGICAL_LOW_WATER_BYTES,
    "postgresMinFree": settings.EXPERIMENT_POSTGRES_VOLUME_MIN_FREE_BYTES,
    "postgresResumeFree": settings.EXPERIMENT_POSTGRES_VOLUME_RESUME_FREE_BYTES,
    "artifactMinFree": settings.EXPERIMENT_ARTIFACT_VOLUME_MIN_FREE_BYTES,
    "artifactResumeFree": settings.EXPERIMENT_ARTIFACT_VOLUME_RESUME_FREE_BYTES,
    "maxAgeSeconds": settings.EXPERIMENT_STORAGE_SIGNAL_MAX_AGE_SECONDS,
  }
  missing = [name for name, value in values.items() if value is None]
  config = {name: int(value) for name, value in values.items() if value is not None}
  pairs = (
    ("logicalLow", "logicalHigh"),
    ("postgresMinFree", "postgresResumeFree"),
    ("artifactMinFree", "artifactResumeFree"),
  )
  for lower, upper in pairs:
    if lower in config and upper in config and config[lower] >= config[upper]:
      missing.append(f"{lower}:{upper}:invalid")
  return config, missing


def _signal_state(
  name: str,
  value: int,
  previous_state: str,
  config: dict[str, int],
) -> tuple[str, dict[str, int]]:
  if name == "postgres_logical":
    high, low = config["logicalHigh"], config["logicalLow"]
    pressured = value >= high or (previous_state == "pressured" and value > low)
    return ("pressured" if pressured else "open", {"high": high, "low": low})
  prefix = "postgres" if name == "postgres_volume_free" else "artifact"
  minimum = config[f"{prefix}MinFree"]
  resume = config[f"{prefix}ResumeFree"]
  pressured = value <= minimum or (previous_state == "pressured" and value < resume)
  return (
    "pressured" if pressured else "open",
    {"minimumFree": minimum, "resumeFree": resume},
  )


def _parse_time(value: object) -> datetime | None:
  if not isinstance(value, str):
    return None
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None
  return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_nonnegative_int(value: object) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value > 0
