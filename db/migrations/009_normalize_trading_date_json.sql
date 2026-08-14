-- 仅标准化已知任务 JSON 字段，避免修改策略代码、备注与时间戳文本。
UPDATE backtest_run
SET requested_params = jsonb_set(
  jsonb_set(
    requested_params,
    '{startDate}',
    to_jsonb(concat(substr(requested_params->>'startDate', 1, 4), '-', substr(requested_params->>'startDate', 5, 2), '-', substr(requested_params->>'startDate', 7, 2))),
    true
  ),
  '{endDate}',
  to_jsonb(concat(substr(requested_params->>'endDate', 1, 4), '-', substr(requested_params->>'endDate', 5, 2), '-', substr(requested_params->>'endDate', 7, 2))),
  true
)
WHERE requested_params->>'startDate' ~ '^[0-9]{8}$'
  AND requested_params->>'endDate' ~ '^[0-9]{8}$';

UPDATE l2_backtest_job
SET payload = jsonb_set(
  jsonb_set(
    payload,
    '{startDate}',
    to_jsonb(concat(substr(payload->>'startDate', 1, 4), '-', substr(payload->>'startDate', 5, 2), '-', substr(payload->>'startDate', 7, 2))),
    true
  ),
  '{endDate}',
  to_jsonb(concat(substr(payload->>'endDate', 1, 4), '-', substr(payload->>'endDate', 5, 2), '-', substr(payload->>'endDate', 7, 2))),
  true
)
WHERE payload->>'startDate' ~ '^[0-9]{8}$'
  AND payload->>'endDate' ~ '^[0-9]{8}$';
