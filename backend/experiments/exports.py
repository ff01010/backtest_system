"""评价结果表与研究报告的安全流式序列化。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import csv
from html import escape
from io import StringIO
import json
from pathlib import Path
import re
from typing import Any, BinaryIO
from urllib.parse import urlencode
from zipfile import ZIP_DEFLATED, ZipFile


ProgressCallback = Callable[[int], None]
CancelCallback = Callable[[], None]
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SENSITIVE_KEY_PARTS = (
  "token", "secret", "password", "cookie", "dsn", "path", "directory",
  "strategycode", "sourcecode",
)
_PRIVATE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_PRIVATE_DSN = re.compile(
  r"^(?:postgres(?:ql)?|mysql|mariadb|redis|mongodb(?:\+srv)?):\/\/",
  re.IGNORECASE,
)


class EvaluationExportError(ValueError):
  """冻结来源或导出契约不再有效。"""


def validate_columns(output: dict[str, Any], columns: list[str]) -> None:
  available = set(output.get("columns") or [])
  unknown = [column for column in columns if column not in available]
  if unknown:
    raise EvaluationExportError(f"评价输出不包含导出字段: {', '.join(unknown)}")


def safe_spreadsheet_value(value: Any) -> Any:
  """阻断表格软件把用户可控文本解释成公式。"""
  if not isinstance(value, str):
    return value
  if value.lstrip().startswith(_FORMULA_PREFIXES):
    return f"'{value}"
  return value


def column_labels(catalog_snapshot: dict[str, Any]) -> dict[str, str]:
  metric_catalog = dict(catalog_snapshot.get("metricCatalog") or {})
  dimension_catalog = dict(catalog_snapshot.get("dimensionCatalog") or {})
  definitions = [
    *list(metric_catalog.get("metrics") or []),
    *list(dimension_catalog.get("dimensions") or []),
  ]
  labels: dict[str, str] = {}
  for item in definitions:
    if not isinstance(item, dict):
      continue
    identifier = item.get("metricId") or item.get("dimensionId")
    if identifier:
      labels[str(identifier)] = str(item.get("displayName") or identifier)
  return labels


def iter_csv_chunks(
  output: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None = None,
  ensure_active: CancelCallback | None = None,
) -> Iterator[bytes]:
  validate_columns(output, columns)
  yield "\ufeff".encode("utf-8")
  yield _csv_line([
    safe_spreadsheet_value(labels.get(column, column)) for column in columns
  ])
  for index, row in enumerate(_rows(output), start=1):
    if ensure_active:
      ensure_active()
    values = dict(row.get("values") or {})
    yield _csv_line([safe_spreadsheet_value(values.get(column)) for column in columns])
    if progress:
      progress(index)


def write_csv(
  path: Path,
  output: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None = None,
  ensure_active: CancelCallback | None = None,
) -> None:
  with path.open("wb") as stream:
    for chunk in iter_csv_chunks(output, columns, labels, progress, ensure_active):
      stream.write(chunk)


def write_xlsx(
  path: Path,
  output: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None = None,
  ensure_active: CancelCallback | None = None,
) -> None:
  validate_columns(output, columns)
  with ZipFile(path, "w", ZIP_DEFLATED, allowZip64=True) as archive:
    _write_xlsx_structure(archive)
    with archive.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as sheet:
      _write_sheet(sheet, output, columns, labels, progress, ensure_active)


def write_report(
  path: Path,
  source: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None = None,
  ensure_active: CancelCallback | None = None,
) -> None:
  output = dict(source.get("output") or {})
  validate_columns(output, columns)
  with path.open("wb") as stream:
    for chunk in iter_report_chunks(
      source, columns, labels, progress, ensure_active,
    ):
      stream.write(chunk)


def iter_report_chunks(
  source: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None = None,
  ensure_active: CancelCallback | None = None,
) -> Iterator[bytes]:
  output = dict(source.get("output") or {})
  yield _html(_report_head(source))
  yield _html(_report_summary(source))
  yield _html(_report_rules(source, labels))
  yield _html(_report_table_head(columns, labels))
  experiment = dict(source.get("experiment") or {})
  fixed = dict(experiment.get("fixedStructure") or {})
  for index, row in enumerate(_rows(output), start=1):
    if ensure_active:
      ensure_active()
    yield _html(_report_row(index, row, columns, fixed))
    if progress:
      progress(index)
  yield _html("</tbody></table></section>")
  yield _html(_report_exceptions(output))
  yield from _report_analysis_chunks(output, ensure_active)
  yield _html(_report_versions(source))
  yield _html("</main></body></html>")


def artifact_contract(export_format: str) -> tuple[str, str]:
  contracts = {
    "csv": ("csv", "text/csv; charset=utf-8"),
    "xlsx": (
      "xlsx",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "report": ("html", "text/html; charset=utf-8"),
  }
  try:
    return contracts[export_format]
  except KeyError as error:
    raise EvaluationExportError("不支持的评价导出格式") from error


def safe_filename(value: str) -> str:
  safe = "".join(
    "_" if char in '\\/:*?\"<>|' or ord(char) < 32 else char
    for char in value.strip()
  ).strip(" ._")
  return safe[:120] or "评价结果"


def _rows(output: dict[str, Any]) -> Iterable[dict[str, Any]]:
  rows = output.get("rows")
  if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, dict)):
    raise EvaluationExportError("评价输出行格式不受支持")
  return rows


def _csv_line(values: list[Any]) -> bytes:
  buffer = StringIO(newline="")
  writer = csv.writer(buffer, lineterminator="\n")
  writer.writerow([_text(value) for value in values])
  return buffer.getvalue().encode("utf-8")


def _text(value: Any) -> str:
  if value is None:
    return ""
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, (dict, list)):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return str(value)


def _write_xlsx_structure(archive: ZipFile) -> None:
  entries = {
    "[Content_Types].xml": _CONTENT_TYPES,
    "_rels/.rels": _ROOT_RELS,
    "xl/workbook.xml": _WORKBOOK,
    "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
    "xl/styles.xml": _STYLES,
  }
  for name, content in entries.items():
    archive.writestr(name, content)


def _write_sheet(
  stream: BinaryIO,
  output: dict[str, Any],
  columns: list[str],
  labels: dict[str, str],
  progress: ProgressCallback | None,
  ensure_active: CancelCallback | None,
) -> None:
  stream.write(_html('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'))
  stream.write(_html('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'))
  _write_xlsx_row(stream, 1, [labels.get(column, column) for column in columns])
  for index, row in enumerate(_rows(output), start=1):
    if ensure_active:
      ensure_active()
    values = dict(row.get("values") or {})
    _write_xlsx_row(
      stream, index + 1,
      [safe_spreadsheet_value(values.get(column)) for column in columns],
    )
    if progress:
      progress(index)
  stream.write(_html("</sheetData></worksheet>"))


def _write_xlsx_row(stream: BinaryIO, row_number: int, values: list[Any]) -> None:
  cells = [
    _xlsx_cell(_cell_reference(index + 1, row_number), value)
    for index, value in enumerate(values)
  ]
  stream.write(_html(f'<row r="{row_number}">{"".join(cells)}</row>'))


def _xlsx_cell(reference: str, value: Any) -> str:
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    return f'<c r="{reference}" t="n"><v>{escape(str(value))}</v></c>'
  text = escape(_text(value))
  preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
  return f'<c r="{reference}" t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _cell_reference(column_number: int, row_number: int) -> str:
  letters = ""
  value = column_number
  while value:
    value, remainder = divmod(value - 1, 26)
    letters = chr(65 + remainder) + letters
  return f"{letters}{row_number}"


def _report_head(source: dict[str, Any]) -> str:
  evidence = dict(source.get("evidence") or {})
  title = escape(_report_text(evidence.get("evaluationName") or "评价研究报告"))
  return (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
    f"<title>{title}</title><style>{_REPORT_STYLE}</style></head><body><main>"
    f"<h1>{title}</h1>"
    '<p class="warning">本报告呈现用户定义的评价规则和历史回测证据，不构成系统投资建议。</p>'
  )


def _report_summary(source: dict[str, Any]) -> str:
  output = dict(source.get("output") or {})
  completeness = dict(output.get("completeness") or {})
  experiment = dict(source.get("experiment") or {})
  subject = dict(experiment.get("subject") or {})
  search = experiment.get("searchSpace") or []
  status = "provisional（部分评价）" if output.get("isPartial") else "complete（完整评价）"
  items = [
    ("实验对象", _report_text(subject.get("subjectType"))),
    ("对象 ID", _report_text(subject.get("subjectId"))),
    ("精确版本", _report_text(subject.get("subjectVersion"))),
    ("模板类型", _report_text(experiment.get("templateType"))),
    ("运行路径", _report_text(experiment.get("runPath"))),
    ("范围类型", _report_text(experiment.get("scopeType"))),
    ("冻结固定结构", _report_text(experiment.get("fixedStructure"))),
    ("Stage 与搜索空间", _report_text(search)),
    ("数据快照", _report_text(experiment.get("datasetSnapshots"))),
    ("评价状态", status),
    ("计划 Trial", _report_text(completeness.get("plannedTrialCount"))),
    ("成功", _report_text(completeness.get("succeededCount"))),
    ("失败", _report_text(completeness.get("failedCount"))),
    ("取消", _report_text(completeness.get("cancelledCount"))),
    ("缺失", _report_text(completeness.get("missingCount"))),
    ("完成率", _report_text(completeness.get("completionRate"))),
  ]
  return '<section><h2>实验与完成质量</h2><dl>' + "".join(
    f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in items
  ) + "</dl></section>"


def _report_rules(source: dict[str, Any], labels: dict[str, str]) -> str:
  spec = dict(source.get("evaluationSpec") or {})
  rules = {
    "展示字段": [labels.get(item, item) for item in list(spec.get("columns") or [])],
    "行级公式": _safe_rule_list(spec.get("derivedMetrics")),
    "过滤": _safe_rule_list(spec.get("filters")),
    "分组": list(spec.get("groupBy") or []),
    "聚合": _safe_rule_list(spec.get("aggregates")),
    "分组后过滤": _safe_rule_list(spec.get("groupFilters")),
    "排序": _safe_rule_list(spec.get("sort")),
    "高级分析": _safe_rule_list(spec.get("visualizations")),
    "报告章节": list(spec.get("reportSections") or []),
    "指标与维度定义": _safe_catalog_definitions(source),
  }
  return '<section><h2>评价规则与指标口径</h2><dl>' + "".join(
    f"<dt>{escape(label)}</dt><dd>{escape(_report_text(value))}</dd>"
    for label, value in rules.items()
  ) + "</dl></section>"


def _safe_rule_list(value: Any) -> list[dict[str, Any]]:
  allowed = {
    "type", "metricId", "displayName", "formula", "precision", "unit",
    "operator", "value", "dimensionId", "aggregateId", "function",
    "percentile", "direction", "parameterDimensionId", "seriesBy",
    "xDimensionId", "yDimensionId", "facetBy", "baselineScenarioId",
    "degradationThreshold",
  }
  return [
    {key: item[key] for key in sorted(item) if key in allowed}
    for item in list(value or []) if isinstance(item, dict)
  ]


def _safe_catalog_definitions(source: dict[str, Any]) -> list[dict[str, Any]]:
  snapshot = dict(source.get("catalogSnapshot") or {})
  metric_catalog = dict(snapshot.get("metricCatalog") or {})
  dimension_catalog = dict(snapshot.get("dimensionCatalog") or {})
  allowed = {
    "metricId", "dimensionId", "displayName", "dataType", "unit",
    "direction", "grain", "windowSemantics", "annualization",
    "benchmarkSemantics", "nullSemantics", "adapterType", "adapterVersion",
  }
  definitions = [
    *list(metric_catalog.get("metrics") or []),
    *list(dimension_catalog.get("dimensions") or []),
  ]
  return [
    {key: item[key] for key in sorted(item) if key in allowed}
    for item in definitions if isinstance(item, dict)
  ]


def _report_table_head(columns: list[str], labels: dict[str, str]) -> str:
  headers = "".join(
    f"<th>{escape(_report_text(labels.get(column, column)))}</th>"
    for column in columns
  )
  return f"<section><h2>筛选后评价结果</h2><table><thead><tr><th>排名</th>{headers}<th>单次回测</th></tr></thead><tbody>"


def _report_row(
  index: int,
  row: dict[str, Any],
  columns: list[str],
  fixed: dict[str, Any],
) -> str:
  values = dict(row.get("values") or {})
  cells = "".join(
    f"<td>{escape(_report_text(values.get(column)))}</td>" for column in columns
  )
  run_id = row.get("runId")
  if run_id:
    query = {
      "workspace": "backtest",
      "runId": str(run_id),
      "symbol": str(fixed.get("symbol") or ""),
      "timeframe": str(fixed.get("timeframe") or ""),
      "date": str(row.get("startDate") or ""),
    }
    href = f"/?{urlencode({key: value for key, value in query.items() if value})}"
    link = f'<a href="{escape(href, quote=True)}">{escape(str(run_id))}</a>'
  else:
    link = escape(f"{row.get('memberCount', '—')} 项聚合")
  return f"<tr><td>{index}</td>{cells}<td>{link}</td></tr>"


def _report_exceptions(output: dict[str, Any]) -> str:
  completeness = dict(output.get("completeness") or {})
  exceptions = list(completeness.get("exceptions") or [])
  rows = []
  for item in exceptions:
    if not isinstance(item, dict):
      continue
    error = item.get("error") if isinstance(item.get("error"), dict) else {}
    summary = {
      "stageId": item.get("stageId"),
      "trialIndex": item.get("trialIndex"),
      "status": item.get("status"),
      "materializationStatus": item.get("materializationStatus"),
      "errorCode": error.get("code"),
      "errorCategory": error.get("category"),
    }
    rows.append(f"<li><code>{escape(_report_text(summary))}</code></li>")
  content = "".join(rows) or "<li>无</li>"
  return f"<section><h2>失败、取消与缺失项</h2><ul>{content}</ul></section>"


def _report_analysis_chunks(
  output: dict[str, Any],
  ensure_active: CancelCallback | None,
) -> Iterator[bytes]:
  yield _html("<section><h2>参数敏感性、成本压力与时间稳定性</h2>")
  visualizations = list(output.get("visualizations") or [])
  if not visualizations:
    yield _html("<p>当前评价规则未配置高级分析。</p></section>")
    return
  for item in visualizations:
    if ensure_active:
      ensure_active()
    if not isinstance(item, dict):
      continue
    analysis_type = _report_text(item.get("type") or "analysis")
    yield _html(
      f"<article><h3>{escape(analysis_type)}</h3>"
      f"<pre>{escape(_pretty_report_json(item))}</pre></article>"
    )
  yield _html("</section>")


def _report_versions(source: dict[str, Any]) -> str:
  evidence = dict(source.get("evidence") or {})
  safe = {
    key: evidence.get(key) for key in (
      "evaluationRunId", "evaluationSpecId", "evaluationSpecVersion",
      "outputHash", "resultSetHash", "outputSchemaVersion", "pipelineVersion",
      "statisticsVersion", "metricCatalogVersion", "dimensionCatalogVersion",
      "normalizerVersion", "adapterType", "adapterVersion",
      "runTemplateDigest", "datasetSnapshotDigest",
    )
  }
  assets = list(source.get("assetVersions") or [])
  return (
    "<section><h2>数据、结果与评价版本证据</h2>"
    f"<pre>{escape(_pretty_report_json(safe))}</pre>"
    f"<h3>资产版本</h3><pre>{escape(_pretty_report_json(assets))}</pre>"
    "</section>"
  )


def _report_text(value: Any) -> str:
  return _text(_redact_report_value(value))


def _pretty_report_json(value: Any) -> str:
  return json.dumps(
    _redact_report_value(value), ensure_ascii=False, indent=2, sort_keys=True,
  )


def _redact_report_value(value: Any, parent_key: str = "") -> Any:
  if any(part in parent_key.lower() for part in _SENSITIVE_KEY_PARTS):
    return "[REDACTED]"
  if isinstance(value, dict):
    return {
      str(key): _redact_report_value(item, str(key))
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [_redact_report_value(item, parent_key) for item in value]
  if isinstance(value, str):
    text = value.strip()
    if _PRIVATE_PATH.match(text) or _PRIVATE_DSN.match(text):
      return "[REDACTED]"
  return value


def _html(value: str) -> bytes:
  return value.encode("utf-8")


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="评价结果" sheetId="1" r:id="rId1"/></sheets></workbook>"""
_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="1"><xf xfId="0"/></cellXfs></styleSheet>"""
_REPORT_STYLE = """
body{font-family:system-ui,sans-serif;color:#172033;background:#f6f8fb;margin:0}
main{max-width:1200px;margin:auto;padding:32px}section{background:#fff;padding:20px;margin:16px 0;border-radius:12px}
.warning{padding:14px;background:#fff3cd;border-left:4px solid #d49b00}dl{display:grid;grid-template-columns:180px 1fr;gap:8px}
dt{font-weight:700}dd{margin:0;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d9deea;padding:8px;text-align:left}th{background:#eef2f8}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}
"""
