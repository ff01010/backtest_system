from pathlib import Path


MIGRATION = (
  Path(__file__).resolve().parents[2]
  / "db"
  / "migrations"
  / "042_public_strategy_lifecycle.sql"
)


def test_public_strategy_migration_is_idempotent_and_backfills_backtests() -> None:
  sql = MIGRATION.read_text(encoding="utf-8")
  compact_sql = " ".join(sql.split())

  for column in (
    "operation_type",
    "resource_type",
    "resource_id",
    "response_status",
    "response_body",
  ):
    assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
  assert "operation_type, 'backtest.submit'" in compact_sql
  assert "resource_type, 'backtest'" in compact_sql
  assert "resource_id, run_id" in compact_sql
  assert "ALTER COLUMN operation_type DROP NOT NULL" in compact_sql
  assert "ALTER COLUMN resource_type DROP NOT NULL" in compact_sql
  assert "WHERE conname='api_idempotency_operation_type_check'" in sql
  assert "CREATE INDEX IF NOT EXISTS ix_api_idempotency_resource" in sql
  assert "CREATE INDEX IF NOT EXISTS ix_backtest_run_browser_idempotency" in sql
