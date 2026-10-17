from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from time import perf_counter
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
  id="20000000-0000-4000-8000-000000000120",
  username="search-owner",
  display_name="Search Owner",
  role="user",
)
OTHER = CurrentUser(
  id="20000000-0000-4000-8000-000000000121",
  username="search-other",
  display_name="Search Other",
  role="user",
)
OWNER_EXACT_RUN_ID = "21000000-0000-4000-8000-000000000120"
OTHER_EXACT_RUN_ID = "21000000-0000-4000-8000-000000000121"
MIGRATIONS = (
  "058_search_projection_foundation.sql",
  "059_search_projection_backfill.sql",
)


def _admin_connection(*, autocommit: bool = False) -> psycopg.Connection[Any]:
  return psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"],
    row_factory=dict_row,
    autocommit=autocommit,
  )


def _as_user(user: CurrentUser, action: Callable[[], Any]) -> Any:
  token = set_current_user(user)
  try:
    return action()
  finally:
    reset_current_user(token)


def _migration_source(name: str) -> str:
  path = Path(__file__).resolve().parents[2] / "db" / "migrations" / name
  return path.read_text(encoding="utf-8").replace(
    ':"app_db_user"', '"quant_backtest_test_app"',
  )


def _rerun_search_migrations() -> None:
  with _admin_connection(autocommit=True) as conn:
    with conn.cursor() as cur:
      cur.execute("SET ROLE quant_backtest_owner")
      for name in MIGRATIONS:
        cur.execute(_migration_source(name))


def _insert_users(cur: Any) -> None:
  for user in (OWNER, OTHER):
    cur.execute("""
      INSERT INTO app_user (
        id, username, display_name, password_hash, role, status
      ) VALUES (%s, %s, %s, 'test-only', 'user', 'active')
      ON CONFLICT (id) DO UPDATE SET status='active'
    """, (user.id, user.username, user.display_name))


def _delete_search_rows(cur: Any) -> None:
  cur.execute(
    "DELETE FROM backtest_run WHERE owner_user_id=ANY(%s)",
    ([OWNER.id, OTHER.id],),
  )
  cur.execute(
    "DELETE FROM user_backtest_counter WHERE owner_user_id=ANY(%s)",
    ([OWNER.id, OTHER.id],),
  )
  cur.execute(
    "DELETE FROM app_user WHERE id=ANY(%s)",
    ([OWNER.id, OTHER.id],),
  )


def _insert_search_rows(cur: Any) -> None:
  cur.execute("""
    INSERT INTO backtest_run (
      id, owner_user_id, display_no, status, requested_params,
      params_snapshot, tags, favorited_at, created_at
    )
    SELECT gen_random_uuid(), %s, value, CASE
        WHEN value %% 3=0 THEN 'queued'
        WHEN value %% 3=1 THEN 'succeeded'
        ELSE 'failed'
      END,
      jsonb_build_object(
        'symbol', CASE WHEN value %% 10=0 THEN '688981.SH' ELSE '000001.SZ' END,
        'strategy', CASE WHEN value %% 20=0 THEN '候选词策略' ELSE '普通策略' END
      ),
      jsonb_build_object(
        'name', CASE WHEN value %% 20=0 THEN '保存时方案' ELSE '普通方案' END
      ),
      CASE WHEN value %% 20=0 THEN '["回归标签"]'::jsonb ELSE '[]'::jsonb END,
      CASE WHEN value %% 5=0 THEN timestamptz '2026-10-17 08:00:00+08' END,
      timestamptz '2026-10-17 08:00:00+08'
        - make_interval(secs => (value / 20)::integer)
    FROM generate_series(1, 1200) AS series(value)
  """, (OWNER.id,))
  cur.execute("""
    INSERT INTO backtest_run (
      id, owner_user_id, display_no, status, requested_params,
      params_snapshot, tags, created_at
    ) VALUES
      (%s, %s, 688981, 'succeeded',
        '{"symbol":"600519.SH","strategy":"精确策略"}'::jsonb,
        '{"name":"精确方案"}'::jsonb, '[]'::jsonb,
        timestamptz '2026-10-18 08:00:00+08'),
      (%s, %s, 688981, 'succeeded',
        '{"symbol":"688981.SH","strategy":"其他用户策略"}'::jsonb,
        '{"name":"其他用户方案"}'::jsonb, '["回归标签"]'::jsonb,
        timestamptz '2026-10-18 08:00:00+08')
  """, (OWNER_EXACT_RUN_ID, OWNER.id, OTHER_EXACT_RUN_ID, OTHER.id))
  cur.execute("ANALYZE backtest_run")


@pytest.fixture
def search_rows() -> Iterator[None]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      _delete_search_rows(cur)
      _insert_users(cur)
      _insert_search_rows(cur)
  yield
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      _delete_search_rows(cur)


def _plan_index_names(plan: dict[str, Any]) -> set[str]:
  names = {str(plan["Index Name"])} if plan.get("Index Name") else set()
  for child in plan.get("Plans", []):
    names.update(_plan_index_names(child))
  return names


def _explain_index_names(query: str, params: tuple[Any, ...]) -> set[str]:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("SET LOCAL enable_seqscan=off")
      cur.execute(f"EXPLAIN (FORMAT JSON) {query}", params)
      result = cur.fetchone()["QUERY PLAN"]
  return _plan_index_names(result[0]["Plan"])


def _insert_benchmark_strategies(cur: Any, count: int) -> None:
  cur.execute("""
    INSERT INTO market_instrument (
      symbol, exchange, display_name, instrument_type, board, eastmoney_secid
    ) VALUES ('688981', 'SH', '性能基准标的', 'stock', 'star', '1.688981')
    ON CONFLICT (exchange, symbol) DO NOTHING
  """)
  cur.execute("""
    SELECT id FROM market_instrument WHERE symbol='688981' AND exchange='SH'
  """)
  instrument_id = int(cur.fetchone()["id"])
  cur.execute("""
    INSERT INTO user_strategy (id, owner_user_id, name)
    SELECT md5('search-strategy-' || value)::uuid, %s,
      '基准脚本-' || value
    FROM generate_series(1, %s) AS series(value)
  """, (OWNER.id, count))
  cur.execute("""
    INSERT INTO user_strategy_version (
      id, strategy_id, version_no, source_code, source_checksum,
      content_fingerprint, created_by_user_id
    )
    SELECT md5('search-version-' || value)::uuid,
      md5('search-strategy-' || value)::uuid, 1,
      'def on_bar(bar, state):' || chr(10) || '  return []',
      repeat('1', 64), repeat('0', 64), %s
    FROM generate_series(1, %s) AS series(value)
  """, (OWNER.id, count))
  cur.execute("""
    INSERT INTO strategy_config (
      id, owner_user_id, name, strategy_id, strategy_version_id,
      strategy_key, instrument_id
    )
    SELECT md5('search-config-' || value)::uuid, %s,
      CASE WHEN value %% 1000=0
        THEN '性能候选方案-' || value ELSE '普通基准方案-' || value END,
      md5('search-strategy-' || value)::uuid,
      md5('search-version-' || value)::uuid,
      'user_script', %s
    FROM generate_series(1, %s) AS series(value)
  """, (OWNER.id, instrument_id, count))
  cur.execute("""
    INSERT INTO strategy_config_version (
      id, config_id, version_no, strategy_version_id, snapshot
    )
    SELECT md5('search-complete-version-' || value)::uuid,
      md5('search-config-' || value)::uuid, 1,
      md5('search-version-' || value)::uuid,
      jsonb_build_object('name', '基准方案-' || value)
    FROM generate_series(1, %s) AS series(value)
  """, (count,))


def _insert_benchmark_runs(cur: Any, count: int) -> None:
  cur.execute("""
    INSERT INTO backtest_run (
      id, owner_user_id, display_no, status, requested_params,
      params_snapshot, tags, created_at
    )
    SELECT gen_random_uuid(), %s, value, 'succeeded',
      jsonb_build_object(
        'symbol', CASE WHEN value %% 500=0 THEN '688981.SH' ELSE '000001.SZ' END,
        'strategy', CASE WHEN value %% 1000=0 THEN '性能候选策略' ELSE '普通策略' END
      ),
      jsonb_build_object(
        'name', CASE WHEN value %% 1000=0 THEN '性能候选方案' ELSE '普通方案' END
      ),
      CASE WHEN value %% 1000=0 THEN '["性能标签"]'::jsonb ELSE '[]'::jsonb END,
      timestamptz '2026-10-17 08:00:00+08' - make_interval(secs => value)
    FROM generate_series(1, %s) AS series(value)
  """, (OWNER.id, count))
  cur.execute("ANALYZE backtest_run")
  cur.execute("ANALYZE strategy_config")


def _query_execution_ms(query: str, params: tuple[Any, ...]) -> float:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}", params)
      result = cur.fetchone()["QUERY PLAN"]
  return float(result[0]["Execution Time"])


def _duration_ms(action: Callable[[], Any]) -> float:
  started = perf_counter()
  action()
  return (perf_counter() - started) * 1000


def _p95(values: list[float]) -> float:
  return sorted(values)[max(0, int(len(values) * 0.95) - 1)]


def _benchmark_page(query: str) -> dict[str, Any]:
  return _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=20,
    offset=0,
    query=query,
    include_filter_options=False,
  ))


def _delete_benchmark_rows() -> None:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        TRUNCATE TABLE backtest_run, strategy_config_version,
          strategy_config, user_strategy_version, user_strategy CASCADE
      """)
      cur.execute(
        "DELETE FROM user_backtest_counter WHERE owner_user_id=ANY(%s)",
        ([OWNER.id, OTHER.id],),
      )
      cur.execute(
        "DELETE FROM app_user WHERE id=ANY(%s)",
        ([OWNER.id, OTHER.id],),
      )


def _assert_dedicated_benchmark_database() -> None:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT count(*) AS count FROM app_user
        WHERE id <> '00000000-0000-0000-0000-000000000001'::uuid
      """)
      if int(cur.fetchone()["count"]) > 0:
        pytest.fail("大数据基准必须在不包含其他测试用户的隔离数据库单独运行")


def test_search_migrations_are_repeatable_and_preserve_business_fields() -> None:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      _delete_search_rows(cur)
      _insert_users(cur)
      cur.execute("""
        INSERT INTO backtest_run (
          id, owner_user_id, display_no, status, requested_params,
          params_snapshot, tags, created_at
        ) VALUES (
          %s, %s, 900001, 'succeeded',
          '{"symbol":"688981.SH","strategy":"迁移校验"}'::jsonb,
          '{"name":"保存快照"}'::jsonb, '["原始标签"]'::jsonb,
          timestamptz '2026-10-17 08:00:00+08'
        )
      """, (OWNER_EXACT_RUN_ID, OWNER.id))
      cur.execute("""
        SELECT status, requested_params, params_snapshot, tags, created_at
        FROM backtest_run WHERE id=%s
      """, (OWNER_EXACT_RUN_ID,))
      before = cur.fetchone()

  _rerun_search_migrations()
  _rerun_search_migrations()

  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT status, requested_params, params_snapshot, tags, created_at,
          search_text_v1
        FROM backtest_run WHERE id=%s
      """, (OWNER_EXACT_RUN_ID,))
      after = cur.fetchone()
      cur.execute("""
        SELECT count(*) AS count
        FROM pg_indexes
        WHERE indexname=ANY(%s)
      """, ([
        "ix_backtest_run_search_text_v1_trgm",
        "ix_strategy_config_name_trgm",
        "ix_market_instrument_name_trgm",
        "ix_market_instrument_symbol_active",
        "ix_backtest_publication_search_text_v1_trgm",
        "ix_backtest_experiment_name_trgm",
        "ix_backtest_experiment_revision_strategy_name_trgm",
        "ix_backtest_experiment_revision_symbol_trgm",
      ],))
      index_count = int(cur.fetchone()["count"])
      _delete_search_rows(cur)

  assert {key: after[key] for key in before} == dict(before)
  assert "保存快照" in after["search_text_v1"]
  assert index_count == 8


def test_projection_trigger_tracks_tag_changes(search_rows: None) -> None:
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_run SET tags='["新增标签"]'::jsonb
        WHERE owner_user_id=%s AND display_no=1
        RETURNING search_text_v1
      """, (OWNER.id,))
      assert "新增标签" in cur.fetchone()["search_text_v1"]
      cur.execute("""
        UPDATE backtest_run SET tags='[]'::jsonb
        WHERE owner_user_id=%s AND display_no=1
        RETURNING search_text_v1
      """, (OWNER.id,))
      assert "新增标签" not in cur.fetchone()["search_text_v1"]


def test_search_is_owner_scoped_and_uses_snapshot_fields(search_rows: None) -> None:
  exact = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=10, offset=0, query="#688981", include_filter_options=False,
  ))
  hidden = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=10, offset=0, query=OTHER_EXACT_RUN_ID, include_filter_options=False,
  ))
  text = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=10, offset=0, query="保存时方案", include_filter_options=False,
  ))
  symbol = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=100, offset=0, query="688981", include_filter_options=False,
  ))

  assert exact["total"] == 1
  assert exact["runs"][0]["id"] == OWNER_EXACT_RUN_ID
  assert hidden["total"] == 0
  assert text["total"] == 60
  assert symbol["total"] == 121


def test_totals_cursor_and_filter_selection_share_one_candidate_set(
  search_rows: None,
) -> None:
  first = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=20,
    offset=0,
    query="候选词",
    symbol="688981.SH",
    favorites_only=True,
    include_filter_options=False,
  ))
  second = _as_user(OWNER, lambda: state_store.list_backtest_run_page(
    limit=20,
    offset=0,
    query="候选词",
    symbol="688981.SH",
    favorites_only=True,
    cursor=first["nextCursor"],
    include_totals=False,
    include_filter_options=False,
  ))
  selection = {
    "mode": "filter",
    "query": "候选词",
    "symbol": "688981.SH",
    "configId": None,
    "favoritesOnly": True,
    "excludedRunIds": [],
  }
  summary = _as_user(
    OWNER, lambda: state_store.summarize_backtest_run_selection(selection),
  )

  assert first["total"] == 60
  assert first["selectableTotal"] == 40
  assert second["total"] is None
  assert second["selectableTotal"] is None
  assert set(run["id"] for run in first["runs"]).isdisjoint(
    run["id"] for run in second["runs"]
  )
  assert summary["selectedRunCount"] == first["selectableTotal"]


def test_exact_and_text_queries_use_expected_indexes(search_rows: None) -> None:
  exact_indexes = _explain_index_names(
    "SELECT id FROM backtest_run WHERE owner_user_id=%s AND display_no=%s",
    (OWNER.id, 688981),
  )
  text_indexes = _explain_index_names(
    "SELECT id FROM backtest_run WHERE search_text_v1 ILIKE %s ESCAPE '\\'",
    ("%候选词%",),
  )

  assert "ux_backtest_run_owner_display_no" in exact_indexes
  assert "ix_backtest_run_search_text_v1_trgm" in text_indexes


def test_database_statement_timeout_is_not_reported_as_empty_results(
  search_rows: None,
) -> None:
  def time_out() -> None:
    with state_store._db() as conn:
      with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='1ms'")
        cur.execute("SELECT pg_sleep(0.05)")

  with pytest.raises(state_store.SearchTimeoutError, match="搜索耗时过长"):
    _as_user(OWNER, time_out)


def test_large_search_benchmark_requires_explicit_opt_in() -> None:
  """25 万规模与 10 并发基准仅在隔离性能环境显式启用。"""
  if os.getenv("RUN_SEARCH_PERFORMANCE_BENCHMARK") != "1":
    pytest.skip("设置 RUN_SEARCH_PERFORMANCE_BENCHMARK=1 后运行合成大数据基准")
  count = 250_000
  _assert_dedicated_benchmark_database()
  with _admin_connection() as conn:
    with conn.cursor() as cur:
      _delete_search_rows(cur)
      _insert_users(cur)
      _insert_benchmark_strategies(cur, count)
      _insert_benchmark_runs(cur, count)
      cur.execute(
        "SELECT id FROM backtest_run WHERE owner_user_id=%s AND display_no=%s",
        (OWNER.id, count),
      )
      benchmark_run_id = str(cur.fetchone()["id"])

  try:
    exact_db_ms = _query_execution_ms(
      "SELECT id FROM backtest_run WHERE owner_user_id=%s AND display_no=%s",
      (OWNER.id, count),
    )
    uuid_db_ms = _query_execution_ms(
      "SELECT id FROM backtest_run WHERE owner_user_id=%s AND id=%s",
      (OWNER.id, benchmark_run_id),
    )
    symbol_db_ms = _query_execution_ms(
      "SELECT id FROM backtest_run WHERE owner_user_id=%s "
      "AND upper(COALESCE(requested_params->>'symbol', ''))=%s",
      (OWNER.id, "688981.SH"),
    )
    text_db_ms = _query_execution_ms(
      "SELECT id FROM backtest_run WHERE search_text_v1 ILIKE %s ESCAPE '\\'",
      ("%性能候选%",),
    )
    strategy_db_ms = _query_execution_ms(
      "SELECT id FROM strategy_config WHERE lower(name) LIKE %s ESCAPE '\\'",
      ("%性能候选%",),
    )
    cold_text_api_ms = _duration_ms(lambda: _benchmark_page("性能候选"))
    exact_api = [_duration_ms(lambda: _benchmark_page(f"#{count}")) for _ in range(20)]
    uuid_api = [_duration_ms(lambda: _benchmark_page(benchmark_run_id)) for _ in range(20)]
    symbol_api = [_duration_ms(lambda: _benchmark_page("688981")) for _ in range(20)]
    text_api = [_duration_ms(lambda: _benchmark_page("性能候选")) for _ in range(20)]
    strategy_api = [_duration_ms(lambda: _as_user(
      OWNER,
      lambda: state_store.list_strategy_config_summaries(
        20, None, "性能候选", True,
      ),
    )) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=10) as executor:
      concurrent = list(executor.map(
        lambda _index: _duration_ms(lambda: _benchmark_page("性能候选")),
        range(10),
      ))
    metrics = {
      "rowsPerResource": count,
      "exactDbMs": round(exact_db_ms, 3),
      "uuidDbMs": round(uuid_db_ms, 3),
      "symbolDbMs": round(symbol_db_ms, 3),
      "textDbMs": round(text_db_ms, 3),
      "strategyDbMs": round(strategy_db_ms, 3),
      "coldTextApiMs": round(cold_text_api_ms, 3),
      "exactApiP95Ms": round(_p95(exact_api), 3),
      "uuidApiP95Ms": round(_p95(uuid_api), 3),
      "symbolApiP95Ms": round(_p95(symbol_api), 3),
      "textApiP95Ms": round(_p95(text_api), 3),
      "strategyApiP95Ms": round(_p95(strategy_api), 3),
      "concurrentMaxMs": round(max(concurrent), 3),
    }
    print(f"search_benchmark={json.dumps(metrics, ensure_ascii=False)}", flush=True)

    assert exact_db_ms < 50
    assert uuid_db_ms < 50
    assert symbol_db_ms < 100
    assert text_db_ms < 300
    assert strategy_db_ms < 300
    assert cold_text_api_ms < 2_000
    assert _p95(exact_api) < 300
    assert _p95(uuid_api) < 300
    assert _p95(symbol_api) < 500
    assert _p95(text_api) < 1_000
    assert _p95(strategy_api) < 1_000
    assert max(concurrent) < 5_000
  finally:
    _delete_benchmark_rows()
