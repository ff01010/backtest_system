# -*- coding: utf-8 -*-
"""策略广场发现与个人收藏查询。"""

from typing import Any, Literal

from backend.services import state_store
from backend.services.search_query import escape_like_literal


SharedBacktestSort = Literal["latest", "copies"]


def _search_clause(query: str, params: list[Any]) -> str:
  normalized = query.strip()
  if not normalized:
    return ""
  params.append(f"%{escape_like_literal(normalized.lower())}%")
  return "AND p.search_text_v1 ILIKE %s ESCAPE '\\'"


def _filter_clause(
  query: str,
  strategy_category: str | None,
  timeframe: str | None,
  favorites_only: bool,
) -> tuple[str, list[Any]]:
  params: list[Any] = []
  clauses = ["p.status='active'", _search_clause(query, params)]
  if strategy_category:
    clauses.append("AND p.strategy_category=%s")
    params.append(strategy_category)
  if timeframe:
    clauses.append("AND p.timeframe=%s")
    params.append(timeframe)
  if favorites_only:
    clauses.append("AND favorite.user_id IS NOT NULL")
  return "\n".join(clauses), params


def _order_clause(sort: SharedBacktestSort) -> str:
  if sort == "copies":
    return "p.copy_count DESC, p.first_published_at DESC, p.id DESC"
  return "p.first_published_at DESC, p.id DESC"


def _list_item(row: dict[str, Any]) -> dict[str, Any]:
  item = state_store._publication_list_item(row)
  item["isFavorited"] = bool(row.get("is_favorited"))
  item["hasCopied"] = bool(row.get("has_copied"))
  return item


def list_shared_backtests(
  *,
  limit: int = 20,
  offset: int = 0,
  query: str = "",
  strategy_category: str | None = None,
  timeframe: str | None = None,
  sort: SharedBacktestSort = "latest",
  favorites_only: bool = False,
) -> dict[str, Any]:
  """按稳定顺序返回当前用户可见的共享回测轻量投影。"""
  if limit < 1 or limit > 100 or offset < 0 or len(query) > 100:
    raise ValueError("查询或分页参数无效")
  viewer_id = state_store.current_user_id()
  where_sql, filter_params = _filter_clause(
    query, strategy_category, timeframe, favorites_only,
  )
  joins = """
    FROM backtest_publication p
    LEFT JOIN backtest_publication_favorite favorite
      ON favorite.publication_id=p.id AND favorite.user_id=%s
    LEFT JOIN backtest_publication_copy copied
      ON copied.publication_id=p.id AND copied.recipient_user_id=%s
  """
  base_params = [viewer_id, viewer_id, *filter_params]
  with state_store._db() as conn:
    with conn.cursor() as cur:
      state_store._set_search_statement_timeout(cur)
      cur.execute(f"SELECT count(*) AS value {joins} WHERE {where_sql}", tuple(base_params))
      total = int(cur.fetchone()["value"])
      cur.execute(f"""
        SELECT p.*, favorite.user_id IS NOT NULL AS is_favorited,
          copied.recipient_user_id IS NOT NULL AS has_copied
        {joins} WHERE {where_sql}
        ORDER BY {_order_clause(sort)} LIMIT %s OFFSET %s
      """, (*base_params, limit, offset))
      return {
        "items": [_list_item(row) for row in cur.fetchall()],
        "total": total,
        "limit": limit,
        "offset": offset,
      }


def favorite_shared_backtest(publication_id: str) -> bool:
  """幂等收藏仍处于上架状态的共享回测。"""
  user_id = state_store.current_user_id()
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT id FROM backtest_publication WHERE id=%s AND status='active'",
        (publication_id,),
      )
      if cur.fetchone() is None:
        raise KeyError(publication_id)
      cur.execute("""
        INSERT INTO backtest_publication_favorite (publication_id, user_id)
        VALUES (%s, %s) ON CONFLICT (publication_id, user_id) DO NOTHING
      """, (publication_id, user_id))
      return cur.rowcount == 1


def unfavorite_shared_backtest(publication_id: str) -> bool:
  """幂等取消当前用户的收藏，不依赖共享回测当前状态。"""
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        DELETE FROM backtest_publication_favorite
        WHERE publication_id=%s AND user_id=%s
      """, (publication_id, state_store.current_user_id()))
      return cur.rowcount == 1
