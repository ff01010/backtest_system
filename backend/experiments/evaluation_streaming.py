"""评价 v2 的分批计算、数据库聚合和全局稳定排序。"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from typing import Any, Callable, Iterator, Sequence

from psycopg.types.json import Jsonb

from backend.experiments.canonicalization import digest, with_version
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation import (
  CANDIDATE_RATE_DISTRIBUTION_NORMALIZER_VERSION,
  EvaluationRow,
  GroupRow,
  MetricValue,
  _adapt_row,
  _apply_formulas,
  _catalog_by_id,
  _compile_formulas,
  _formula_definitions,
  _json_value,
  _parameter_curve,
  _parameter_heatmap,
  _passes_filters,
  _cost_stress,
  _stable_key,
  _time_stability,
  _uses_group_output,
  validate_evaluation_spec,
)
from backend.experiments.evaluation_dataset import (
  OUTPUT_SCHEMA_VERSION,
  EvaluationDataset,
  _worker_session,
)
from backend.experiments.evaluation_statistics import STATISTICS_VERSION


class StreamingEvaluationExecutor:
  """将评价内存占用限制在输入批次或单条分组结果以内。"""

  def __init__(self, dataset: EvaluationDataset) -> None:
    self._dataset = dataset

  def evaluate(
    self,
    run: dict[str, Any],
    source_adapter: Callable[[dict[str, Any]], dict[str, Any]],
    heartbeat: Callable[[], str],
  ) -> dict[str, Any]:
    evaluation_spec = dict(run["evaluation_spec"])
    catalogs = dict(run["catalog_snapshot"])
    validate_evaluation_spec(evaluation_spec, catalogs)
    definitions = _catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId")
    row_formulas = _formula_definitions(evaluation_spec, "row_formula_v1")
    numeric_names = {
      metric_id for metric_id, definition in definitions.items()
      if definition["dataType"] in {"decimal", "integer"}
    }
    compiled = _compile_formulas(row_formulas, numeric_names)
    row_definitions = {**definitions, **row_formulas}
    comparison_domains = {
      item["metricId"]: set() for item in evaluation_spec["filters"]
    }
    run_id = str(run["id"])
    owner_id = str(run["owner_user_id"])
    self._clear(run_id)
    source_count = 0
    for batch in self._dataset.iter_input_batches(run_id):
      self._check_heartbeat(heartbeat())
      persisted: list[dict[str, Any]] = []
      for raw in batch:
        source_count += 1
        row = _adapt_row(source_adapter(raw), definitions)
        row = _apply_formulas(row, row_formulas, compiled)
        self._track_domains(row, comparison_domains)
        if _passes_filters(row, evaluation_spec["filters"], row_definitions):
          persisted.append(self._serialize_row(raw, row, evaluation_spec))
      self._dataset.write_result_batch(run_id, owner_id, persisted)
    self._check_heartbeat(heartbeat())
    self._assert_domains(comparison_domains)
    if _uses_group_output(evaluation_spec):
      output_count = self._build_groups(
        run_id, owner_id, evaluation_spec, row_definitions,
      )
      grouped = True
    else:
      self._assert_persisted_domains(
        run_id, "backtest_experiment_evaluation_result_row",
        [item["metricId"] for item in evaluation_spec["sort"]],
      )
      self._rank_rows(run_id, evaluation_spec["sort"], row_definitions)
      output_count = self._count_rows(
        "backtest_experiment_evaluation_result_row", run_id,
      )
      grouped = False
    visualizations = self._build_visualizations(
      run_id, evaluation_spec, catalogs, row_definitions, heartbeat,
    )
    return self._summary(
      run, evaluation_spec, source_count, output_count, grouped,
      visualizations,
    )

  @staticmethod
  def _check_heartbeat(status: str) -> None:
    if status == "cancel_requested":
      raise ExperimentError(
        "experiment.evaluation_cancel_requested",
        "评价运行已请求取消",
        409,
      )
    if status != "running":
      raise ExperimentError(
        "experiment.evaluation_lease_lost",
        "评价运行租约已失效",
        409,
      )

  @staticmethod
  def _clear(run_id: str) -> None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "DELETE FROM backtest_experiment_evaluation_group_result "
          "WHERE evaluation_run_id=%s",
          (run_id,),
        )
        cur.execute(
          "DELETE FROM backtest_experiment_evaluation_result_row "
          "WHERE evaluation_run_id=%s",
          (run_id,),
        )

  @staticmethod
  def _track_domains(row: Any, domains: dict[str, set[str]]) -> None:
    for metric_id, values in domains.items():
      metric = row.metrics[metric_id]
      if metric.value is not None and metric.comparability_key:
        values.add(metric.comparability_key)
        if len(values) > 1:
          raise _not_comparable(metric_id, len(values))

  @staticmethod
  def _assert_domains(domains: dict[str, set[str]]) -> None:
    for metric_id, values in domains.items():
      if len(values) > 1:
        raise _not_comparable(metric_id, len(values))

  @staticmethod
  def _serialize_row(
    raw: dict[str, Any],
    row: Any,
    evaluation_spec: dict[str, Any],
  ) -> dict[str, Any]:
    metric_values = {
      metric_id: _json_value(metric.value)
      for metric_id, metric in row.metrics.items()
    }
    metric_domains = {
      metric_id: metric.comparability_key
      for metric_id, metric in row.metrics.items()
      if metric.comparability_key
    }
    unsupported = {
      metric_id: metric.unsupported_reason
      for metric_id, metric in row.metrics.items()
      if metric.unsupported_reason
    }
    return {
      "sourceOrdinal": int(raw["ordinal"]),
      "trialExecutionId": row.identity["trialExecutionId"],
      "identity": row.identity,
      "dimensions": {
        key: _json_value(value) for key, value in row.dimensions.items()
      },
      "metricValues": metric_values,
      "metricDomains": metric_domains,
      "groupKey": [
        _json_value(row.dimensions.get(field))
        for field in evaluation_spec["groupBy"]
      ],
      "values": _column_values(row, evaluation_spec["columns"]),
      "unsupportedReasons": unsupported,
    }

  def _build_groups(
    self,
    run_id: str,
    owner_id: str,
    evaluation_spec: dict[str, Any],
    row_definitions: dict[str, dict[str, Any]],
  ) -> int:
    aggregate_definitions = {
      item["aggregateId"]: {
        **item,
        "dataType": "integer" if item["function"] == "count" else "decimal",
        "unit": (
          None if item["function"] == "count"
          else row_definitions[item["metricId"]].get("unit")
        ),
        "grain": "group",
      }
      for item in evaluation_spec["aggregates"]
    }
    group_formulas = _formula_definitions(evaluation_spec, "group_formula_v1")
    compiled = _compile_formulas(
      group_formulas, set(aggregate_definitions), allow_prior=False,
    )
    output_definitions = {**aggregate_definitions, **group_formulas}
    count = 0
    for group_key in self._iter_group_keys(run_id):
      group = self._aggregate_group(
        run_id, group_key, evaluation_spec, row_definitions,
      )
      group = _apply_formulas(group, group_formulas, compiled)
      if not _passes_filters(
        group, evaluation_spec["groupFilters"], output_definitions,
      ):
        continue
      self._write_group(run_id, owner_id, group_key, group, evaluation_spec)
      count += 1
    self._assert_persisted_domains(
      run_id, "backtest_experiment_evaluation_group_result",
      [item["metricId"] for item in evaluation_spec["sort"]],
    )
    self._rank_groups(run_id, evaluation_spec["sort"], output_definitions)
    return count

  @staticmethod
  def _iter_group_keys(run_id: str) -> Iterator[list[Any]]:
    with _worker_session() as conn:
      with conn.cursor(name=f"evaluation_groups_{run_id.replace('-', '')}") as cur:
        cur.execute("""
          SELECT DISTINCT group_key
          FROM backtest_experiment_evaluation_result_row
          WHERE evaluation_run_id=%s
          ORDER BY group_key
        """, (run_id,))
        while True:
          batch = cur.fetchmany(100)
          if not batch:
            return
          for row in batch:
            yield list(row["group_key"])

  def _aggregate_group(
    self,
    run_id: str,
    group_key: list[Any],
    evaluation_spec: dict[str, Any],
    row_definitions: dict[str, dict[str, Any]],
  ) -> GroupRow:
    aggregates = list(evaluation_spec["aggregates"])
    select_parts = ["count(*) AS member_count"]
    params: list[Any] = []
    for index, item in enumerate(aggregates):
      expression, expression_params = _aggregate_expression(item)
      select_parts.append(f"{expression} AS aggregate_{index}")
      params.extend(expression_params)
    query = f"""
      SELECT {', '.join(select_parts)}
      FROM backtest_experiment_evaluation_result_row
      WHERE evaluation_run_id=%s AND group_key=%s
    """
    params.extend([run_id, Jsonb(group_key)])
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(query, params)
        stats = dict(cur.fetchone())
        cur.execute("""
          SELECT identity
          FROM backtest_experiment_evaluation_result_row
          WHERE evaluation_run_id=%s AND group_key=%s
          ORDER BY (identity->>'stageIndex')::bigint,
            (identity->>'trialIndex')::bigint, identity->>'trialId'
          LIMIT 1
        """, (run_id, Jsonb(group_key)))
        first = dict(cur.fetchone()["identity"])
    dimensions = dict(zip(evaluation_spec["groupBy"], group_key, strict=True))
    metrics: dict[str, MetricValue] = {}
    for index, item in enumerate(aggregates):
      definition = row_definitions[item["metricId"]]
      domains = self._aggregate_domains(
        run_id, group_key, item["metricId"],
        normalized=(
          evaluation_spec["catalogs"].get("normalizerVersion")
          == CANDIDATE_RATE_DISTRIBUTION_NORMALIZER_VERSION
          and definition.get("unit") == "ratio"
        ),
        normalizer_version=evaluation_spec["catalogs"].get("normalizerVersion"),
      )
      value = stats[f"aggregate_{index}"]
      if value is not None and item["function"] == "count":
        value = int(value)
      elif value is not None:
        value = Decimal(str(value))
      metrics[item["aggregateId"]] = MetricValue(
        value,
        domains,
        "all_values_null" if value is None else None,
      )
    return GroupRow(
      identity={
        "groupKey": digest(with_version({"dimensions": dimensions})),
        "stageIndex": int(first["stageIndex"]),
        "trialIndex": int(first["trialIndex"]),
        "trialId": str(first["trialId"]),
      },
      dimensions=dimensions,
      metrics=metrics,
      member_count=int(stats["member_count"]),
      members=(),
    )

  @staticmethod
  def _aggregate_domains(
    run_id: str,
    group_key: list[Any],
    metric_id: str,
    *,
    normalized: bool,
    normalizer_version: str | None,
  ) -> str | None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT DISTINCT metric_domains->>%s AS domain
          FROM backtest_experiment_evaluation_result_row
          WHERE evaluation_run_id=%s AND group_key=%s
            AND metric_values->>%s IS NOT NULL
            AND metric_domains->>%s IS NOT NULL
          ORDER BY domain
        """, (
          metric_id, run_id, Jsonb(group_key), metric_id, metric_id,
        ))
        if normalized:
          return _normalized_domain_digest(
            cur, normalizer_version, metric_id,
          )
        rows = cur.fetchmany(2)
    if len(rows) > 1:
      raise _not_comparable(metric_id, len(rows))
    return str(rows[0]["domain"]) if rows else None

  @staticmethod
  def _write_group(
    run_id: str,
    owner_id: str,
    group_key: list[Any],
    group: GroupRow,
    evaluation_spec: dict[str, Any],
  ) -> None:
    metric_values = {
      metric_id: _json_value(metric.value)
      for metric_id, metric in group.metrics.items()
    }
    metric_domains = {
      metric_id: metric.comparability_key
      for metric_id, metric in group.metrics.items()
      if metric.comparability_key
    }
    unsupported = {
      metric_id: metric.unsupported_reason
      for metric_id, metric in group.metrics.items()
      if metric.unsupported_reason
    }
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          INSERT INTO backtest_experiment_evaluation_group_result (
            evaluation_run_id, owner_user_id, group_id, group_key,
            identity, dimensions, metric_values, metric_domains,
            member_count, values, unsupported_reasons
          ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (evaluation_run_id, group_id) DO UPDATE
          SET ordinal=NULL, group_key=EXCLUDED.group_key,
            identity=EXCLUDED.identity, dimensions=EXCLUDED.dimensions,
            metric_values=EXCLUDED.metric_values,
            metric_domains=EXCLUDED.metric_domains,
            member_count=EXCLUDED.member_count, values=EXCLUDED.values,
            unsupported_reasons=EXCLUDED.unsupported_reasons
        """, (
          run_id, owner_id, group.identity["groupKey"], Jsonb(group_key),
          Jsonb(group.identity), Jsonb(group.dimensions), Jsonb(metric_values),
          Jsonb(metric_domains), group.member_count,
          Jsonb(_column_values(group, evaluation_spec["columns"])),
          Jsonb(unsupported),
        ))

  def _assert_persisted_domains(
    self,
    run_id: str,
    table: str,
    metric_ids: Sequence[str],
  ) -> None:
    if table not in {
      "backtest_experiment_evaluation_result_row",
      "backtest_experiment_evaluation_group_result",
    }:
      raise ValueError("评价结果表不在白名单")
    with _worker_session() as conn:
      with conn.cursor() as cur:
        for metric_id in metric_ids:
          cur.execute(f"""
            SELECT metric_domains->>%s AS domain
            FROM {table}
            WHERE evaluation_run_id=%s AND metric_values->>%s IS NOT NULL
            GROUP BY 1
            LIMIT 2
          """, (metric_id, run_id, metric_id))
          rows = cur.fetchall()
          if len(rows) > 1:
            raise _not_comparable(metric_id, len(rows))

  @staticmethod
  def _rank_rows(
    run_id: str,
    sort_items: Sequence[dict[str, Any]],
    definitions: dict[str, dict[str, Any]],
  ) -> None:
    order_sql, params = _sort_sql(sort_items, definitions)
    order_sql.extend([
      "(identity->>'stageIndex')::bigint ASC",
      "(identity->>'trialIndex')::bigint ASC",
      "identity->>'trialId' ASC",
    ])
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(f"""
          WITH ranked AS (
            SELECT source_ordinal,
              row_number() OVER (ORDER BY {', '.join(order_sql)}) - 1 AS rank
            FROM backtest_experiment_evaluation_result_row
            WHERE evaluation_run_id=%s
          )
          UPDATE backtest_experiment_evaluation_result_row result
          SET ordinal=ranked.rank, ranking_key=jsonb_build_array(ranked.rank)
          FROM ranked
          WHERE result.evaluation_run_id=%s
            AND result.source_ordinal=ranked.source_ordinal
        """, [*params, run_id, run_id])

  @staticmethod
  def _rank_groups(
    run_id: str,
    sort_items: Sequence[dict[str, Any]],
    definitions: dict[str, dict[str, Any]],
  ) -> None:
    order_sql, params = _sort_sql(sort_items, definitions)
    order_sql.extend([
      "(identity->>'stageIndex')::bigint ASC",
      "(identity->>'trialIndex')::bigint ASC",
      "identity->>'trialId' ASC",
      "group_id ASC",
    ])
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(f"""
          WITH ranked AS (
            SELECT group_id,
              row_number() OVER (ORDER BY {', '.join(order_sql)}) - 1 AS rank
            FROM backtest_experiment_evaluation_group_result
            WHERE evaluation_run_id=%s
          )
          UPDATE backtest_experiment_evaluation_group_result result
          SET ordinal=ranked.rank
          FROM ranked
          WHERE result.evaluation_run_id=%s AND result.group_id=ranked.group_id
        """, [*params, run_id, run_id])

  @staticmethod
  def _count_rows(table: str, run_id: str) -> int:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(
          f"SELECT count(*) AS value FROM {table} WHERE evaluation_run_id=%s",
          (run_id,),
        )
        return int(cur.fetchone()["value"])

  def _build_visualizations(
    self,
    run_id: str,
    evaluation_spec: dict[str, Any],
    catalogs: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
    heartbeat: Callable[[], str],
  ) -> list[dict[str, Any]]:
    dimension_definitions = _catalog_by_id(
      catalogs["dimensionCatalog"], "dimensions", "dimensionId",
    )
    output: list[dict[str, Any]] = []
    for item in evaluation_spec["visualizations"]:
      self._check_heartbeat(heartbeat())
      visualization_type = item["type"]
      if visualization_type == "parameter_curve_v1":
        represented = {item["parameterDimensionId"], *item["seriesBy"]}
        self._assert_analysis_facets(run_id, represented, dimension_definitions)
        series: list[dict[str, Any]] = []
        for rows in self._iter_visualization_groups(
          run_id, list(item["seriesBy"]), definitions,
        ):
          self._check_heartbeat(heartbeat())
          series.extend(
            _parameter_curve(item, rows, dimension_definitions)["series"],
          )
        series.sort(key=lambda row: _stable_key(tuple(
          row["dimensions"].get(field) for field in item["seriesBy"]
        )))
        output.append({
          "type": visualization_type,
          "metricId": item["metricId"],
          "parameterDimensionId": item["parameterDimensionId"],
          "interpolation": "none",
          "evidence": "actual_discrete_trials_only",
          "series": series,
        })
      elif visualization_type == "parameter_heatmap_v1":
        represented = {
          item["xDimensionId"], item["yDimensionId"], *item["facetBy"],
        }
        self._assert_analysis_facets(run_id, represented, dimension_definitions)
        facets: list[dict[str, Any]] = []
        for rows in self._iter_visualization_groups(
          run_id, list(item["facetBy"]), definitions,
        ):
          self._check_heartbeat(heartbeat())
          facets.extend(
            _parameter_heatmap(item, rows, dimension_definitions)["facets"],
          )
        facets.sort(key=lambda row: _stable_key(tuple(
          row["dimensions"].get(field) for field in item["facetBy"]
        )))
        output.append({
          "type": visualization_type,
          "metricId": item["metricId"],
          "xDimensionId": item["xDimensionId"],
          "yDimensionId": item["yDimensionId"],
          "interpolation": "none",
          "evidence": "actual_discrete_trials_only",
          "facets": facets,
        })
      elif visualization_type == "cost_stress_v1":
        candidate_dimensions = sorted(
          dimension_id for dimension_id in dimension_definitions
          if dimension_id not in {
            "costScenarioId", "slippage", "resultStatus",
          }
        )
        comparisons: list[dict[str, Any]] = []
        for rows in self._iter_visualization_groups(
          run_id, candidate_dimensions, definitions,
        ):
          self._check_heartbeat(heartbeat())
          comparisons.extend(
            _cost_stress(item, rows, definitions[item["metricId"]])[
              "comparisons"
            ],
          )
        output.append({
          "type": visualization_type,
          "metricId": item["metricId"],
          "source": "independent_slippage_trials",
          "displayOnlyAdjustment": False,
          "comparisons": comparisons,
        })
      elif visualization_type == "time_stability_v1":
        candidates: list[dict[str, Any]] = []
        for rows in self._iter_visualization_groups(
          run_id, [], definitions, candidate_key=True,
        ):
          self._check_heartbeat(heartbeat())
          candidates.extend(_time_stability(item, rows)["candidates"])
        candidates.sort(key=lambda row: str(row["candidateKey"]))
        output.append({
          "type": visualization_type,
          "metricId": item["metricId"],
          "finalTestEligible": False,
          "referenceSemantics": "reference_is_not_final_test",
          "candidates": candidates,
        })
    return output

  @staticmethod
  def _assert_analysis_facets(
    run_id: str,
    represented: set[str],
    dimensions: dict[str, dict[str, Any]],
  ) -> None:
    analysis_dimensions = {
      "dateScenarioId", "initialCapital", "costScenarioId",
      "executionScenarioId",
      *(key for key in dimensions if key.startswith("parameter.")),
    }
    missing: list[str] = []
    with _worker_session() as conn:
      with conn.cursor() as cur:
        for dimension_id in sorted(analysis_dimensions - represented):
          cur.execute("""
            SELECT count(DISTINCT dimensions->%s) AS value
            FROM backtest_experiment_evaluation_result_row
            WHERE evaluation_run_id=%s
          """, (dimension_id, run_id))
          if int(cur.fetchone()["value"]) > 1:
            missing.append(dimension_id)
    if missing:
      raise ExperimentError(
        "experiment.evaluation_visualization_ambiguous",
        "参数图存在未固定或未分组的额外实验维度",
        422,
        details={"missingDimensions": missing},
      )

  @staticmethod
  def _iter_visualization_groups(
    run_id: str,
    group_dimensions: list[str],
    definitions: dict[str, dict[str, Any]],
    *,
    candidate_key: bool = False,
  ) -> Iterator[list[EvaluationRow]]:
    name_seed = ",".join(group_dimensions) or "candidate"
    cursor_name = "evaluation_visual_" + hashlib.sha256(
      f"{run_id}:{name_seed}".encode("utf-8"),
    ).hexdigest()[:20]
    if candidate_key:
      order_sql = "identity->'candidateKey', source_ordinal"
      params: list[Any] = [run_id]
    else:
      order_parts = ["dimensions->%s" for _ in group_dimensions]
      order_parts.append("source_ordinal")
      order_sql = ", ".join(order_parts)
      params = [run_id, *group_dimensions]
    query = f"""
      SELECT identity, dimensions, metric_values, metric_domains,
        unsupported_reasons
      FROM backtest_experiment_evaluation_result_row
      WHERE evaluation_run_id=%s
      ORDER BY {order_sql}
    """
    with _worker_session() as conn:
      with conn.cursor(name=cursor_name) as cur:
        cur.execute(query, params)
        current_key: str | None = None
        current_rows: list[EvaluationRow] = []
        while True:
          batch = cur.fetchmany(200)
          if not batch:
            break
          for raw in batch:
            row = _persisted_evaluation_row(dict(raw), definitions)
            key = _stable_key(
              row.identity.get("candidateKey")
              if candidate_key else tuple(
                row.dimensions.get(field) for field in group_dimensions
              )
            )
            if current_key is not None and key != current_key:
              yield current_rows
              current_rows = []
            current_key = key
            current_rows.append(row)
        if current_rows:
          yield current_rows

  @staticmethod
  def _summary(
    run: dict[str, Any],
    evaluation_spec: dict[str, Any],
    source_count: int,
    output_count: int,
    grouped: bool,
    visualizations: list[dict[str, Any]],
  ) -> dict[str, Any]:
    completeness = dict(run.get("completeness_snapshot") or {})
    is_partial = bool(completeness.get("isPartial"))
    return with_version({
      "schemaVersion": OUTPUT_SCHEMA_VERSION,
      "pipelineVersion": run["pipeline_version"],
      "statisticsVersion": STATISTICS_VERSION,
      "resultSetHash": run["result_set_hash"],
      "sourceRowCount": source_count,
      "matchedRowCount": output_count,
      "isPartial": is_partial,
      "reportStatus": "provisional" if is_partial else "complete",
      "candidateSelectionEligible": not is_partial,
      "finalTestEligible": False,
      "datasetNotice": (
        "“开发”“验证”和“参考”仅是冻结的展示与分组标签；"
        "对全部候选运行过的“参考”不代表最终测试。"
      ),
      "completeness": completeness,
      "columns": evaluation_spec["columns"],
      "sort": evaluation_spec["sort"],
      "groupBy": evaluation_spec["groupBy"],
      "normalizedResultsPersisted": True,
      "rows": [],
      "visualizations": visualizations,
      "visualizationCount": len(visualizations),
      "grouped": grouped,
    })


def _persisted_evaluation_row(
  raw: dict[str, Any],
  definitions: dict[str, dict[str, Any]],
) -> EvaluationRow:
  values = dict(raw["metric_values"])
  domains = dict(raw["metric_domains"])
  unsupported = dict(raw.get("unsupported_reasons") or {})
  metrics: dict[str, MetricValue] = {}
  for metric_id, definition in definitions.items():
    value = values.get(metric_id)
    data_type = definition["dataType"]
    if value is not None and data_type == "decimal":
      value = Decimal(str(value))
    elif value is not None and data_type == "integer":
      value = int(value)
    elif value is not None and data_type == "string":
      value = str(value)
    metrics[metric_id] = MetricValue(
      value,
      domains.get(metric_id),
      unsupported.get(metric_id),
    )
  return EvaluationRow(
    identity=dict(raw["identity"]),
    dimensions=dict(raw["dimensions"]),
    metrics=metrics,
  )


def _column_values(row: Any, columns: Sequence[str]) -> dict[str, Any]:
  return {
    field_id: _json_value(
      row.metrics[field_id].value
      if field_id in row.metrics else row.dimensions.get(field_id)
    )
    for field_id in columns
  }


def _aggregate_expression(item: dict[str, Any]) -> tuple[str, list[Any]]:
  metric_id = item["metricId"]
  value = "NULLIF(metric_values->>%s, '')::numeric"
  function = item["function"]
  if function == "count":
    return "count(metric_values->>%s)", [metric_id]
  if function == "min":
    return f"min({value})", [metric_id]
  if function == "max":
    return f"max({value})", [metric_id]
  if function == "mean":
    return f"avg({value})", [metric_id]
  if function == "sample_stddev":
    return f"stddev_samp({value})", [metric_id]
  if function == "median":
    return (
      f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {value})",
      [metric_id],
    )
  if function == "nearest_rank_percentile":
    return (
      f"percentile_disc(%s) WITHIN GROUP (ORDER BY {value})",
      [Decimal(str(item["percentile"])), metric_id],
    )
  raise ExperimentError(
    "experiment.evaluation_spec_invalid",
    f"未知聚合函数: {function}",
    422,
  )


def _sort_sql(
  sort_items: Sequence[dict[str, Any]],
  definitions: dict[str, dict[str, Any]],
) -> tuple[list[str], list[Any]]:
  clauses: list[str] = []
  params: list[Any] = []
  for item in sort_items:
    metric_id = item["metricId"]
    definition = definitions[metric_id]
    expression = "metric_values->>%s"
    if definition["dataType"] in {"decimal", "integer"}:
      expression = f"({expression})::numeric"
    direction = "DESC" if item["direction"] == "desc" else "ASC"
    clauses.append(f"{expression} {direction} NULLS LAST")
    params.append(metric_id)
  return clauses, params


def _normalized_domain_digest(
  cur: Any,
  normalizer_version: str | None,
  metric_id: str,
) -> str:
  hasher = hashlib.sha256()
  prefix = {
    "canonicalizationVersion": "canonical_json_v1",
    "metricId": metric_id,
    "normalizerVersion": normalizer_version,
  }
  encoded_prefix = json.dumps(
    prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  )
  hasher.update(encoded_prefix[:-1].encode("utf-8"))
  hasher.update(b',"sourceDomains":[')
  first = True
  while True:
    rows = cur.fetchmany(200)
    if not rows:
      break
    for row in rows:
      if not first:
        hasher.update(b",")
      hasher.update(json.dumps(str(row["domain"]), ensure_ascii=False).encode("utf-8"))
      first = False
  hasher.update(b"]}")
  return hasher.hexdigest()


def _not_comparable(metric_id: str, domain_count: int) -> ExperimentError:
  return ExperimentError(
    "experiment.metric_not_comparable",
    f"指标 {metric_id} 存在多个不可直接比较的口径",
    409,
    details={"metricId": metric_id, "domainCount": domain_count},
  )
