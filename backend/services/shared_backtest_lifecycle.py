# -*- coding: utf-8 -*-
"""共享回测作者生命周期服务。

写操作统一通过受限数据库函数执行，避免赋予应用角色通用 UPDATE 权限。
"""

from typing import Any

from psycopg.types.json import Jsonb

from backend.services import state_store


_VALID_STATUSES = {"active", "withdrawn", "removed"}


def _management_item(row: dict[str, Any]) -> dict[str, Any]:
  """返回作者管理投影，不暴露原任务或所有者内部标识。"""
  return {
    **state_store._publication_list_item(row),
    "description": row["description"],
    "status": row["status"],
    "withdrawnAt": _optional_time(row.get("withdrawn_at")),
    "removedAt": _optional_time(row.get("removed_at")),
    "removalReason": row.get("removal_reason"),
    "updatedAt": state_store._format_time(row["updated_at"]),
    "sourceRunAvailable": row.get("source_run_id") is not None,
  }


def _optional_time(value: Any) -> str | None:
  return state_store._format_time(value) if value is not None else None


def list_my_shared_backtests(
  limit: int = 20,
  offset: int = 0,
  status: str | None = None,
) -> dict[str, Any]:
  """分页返回当前作者的全部状态共享条目。"""
  if limit < 1 or limit > 100 or offset < 0:
    raise ValueError("分页参数无效")
  if status is not None and status not in _VALID_STATUSES:
    raise ValueError("共享状态无效")
  user_id = state_store.current_user_id()
  where = "owner_user_id=%s"
  params: list[Any] = [user_id]
  if status is not None:
    where += " AND status=%s"
    params.append(status)
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"SELECT count(*) AS value FROM backtest_publication WHERE {where}", params)
      total = int(cur.fetchone()["value"])
      cur.execute(f"""
        SELECT * FROM backtest_publication WHERE {where}
        ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s
      """, (*params, limit, offset))
      return {
        "items": [_management_item(row) for row in cur.fetchall()],
        "total": total,
        "limit": limit,
        "offset": offset,
      }


def update_publication_metadata(
  publication_id: str,
  title: str,
  description: str,
  tags: list[str],
) -> dict[str, Any]:
  """只更新当前作者共享条目的展示元数据。"""
  return _execute_author_mutation(
    publication_id,
    "SELECT app.update_backtest_publication_metadata(%s, %s, %s, %s, %s) AS publication",
    (title, description, Jsonb(tags)),
    expected_status=None,
  )


def withdraw_publication(publication_id: str) -> dict[str, Any]:
  """将作者本人已上架条目撤回。"""
  return _execute_author_mutation(
    publication_id,
    "SELECT app.withdraw_backtest_publication(%s, %s) AS publication",
    (),
    expected_status="active",
  )


def relist_publication(publication_id: str) -> dict[str, Any]:
  """重新上架作者本人已撤回的原条目。"""
  return _execute_author_mutation(
    publication_id,
    "SELECT app.relist_backtest_publication(%s, %s) AS publication",
    (),
    expected_status="withdrawn",
  )


def _execute_author_mutation(
  publication_id: str,
  query: str,
  values: tuple[Any, ...],
  expected_status: str | None,
) -> dict[str, Any]:
  user_id = state_store.current_user_id()
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(query, (publication_id, user_id, *values))
      result = cur.fetchone()["publication"]
      if result is not None:
        return _management_item(result)
      cur.execute(
        "SELECT status FROM backtest_publication WHERE id=%s AND owner_user_id=%s",
        (publication_id, user_id),
      )
      row = cur.fetchone()
      if row is None:
        raise KeyError(publication_id)
      if expected_status is not None:
        raise ValueError(_transition_error(expected_status, row["status"]))
      raise state_store.DatabaseError("共享展示信息更新失败")


def _transition_error(expected_status: str, actual_status: str) -> str:
  if actual_status == "removed":
    return "已下架的共享回测不能由作者更改状态"
  if expected_status == "active":
    return "只能撤回已上架的共享回测"
  return "只能重新上架本人已撤回的共享回测"
