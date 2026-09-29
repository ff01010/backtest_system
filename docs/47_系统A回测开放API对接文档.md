# 系统 A 回测开放 API 对接文档

## 1. 文档信息

- 文档版本：`1.1`
- 接口版本：`v1`
- 接口状态：当前已实现
- 调用方：系统 A 策略 Hub
- 服务方：玄龄回测系统
- 基础地址：由部署环境提供，本文使用 `{BASE_URL}`
- 数据格式：`application/json`

本文只描述玄龄回测系统当前已经实现的开放 API。

## 2. 当前能力范围

系统 A 可以通过开放 API：

- 分页读取当前用户的活动或归档策略及不可变版本；
- 创建、完整修改、显式发布、可恢复归档和恢复回测策略；
- 引用玄龄回测系统中已经保存的策略方案提交回测；
- 覆盖本次回测的日期、初始资金、滑点和策略声明参数；
- 查询当前 API Key 所属用户的回测任务；
- 查询回测任务详情；
- 分页查询信号和成交明细；
- 取消排队中或运行中的任务。

提交接口以本系统策略方案 ID 作为策略入口，标的、周期和策略逻辑均从方案读取。策略方案必须满足：

- 属于当前 API Key 所属用户；
- 已启用；
- 已发布；
- 已保存完整的策略、标的、周期和运行参数。

策略方案既可通过玄龄回测系统页面维护，也可通过本文第 15 节的完整策略接口维护。开放策略接口只接受回测模式，不接受实盘账户、交易凭据或实盘执行配置。

## 3. 鉴权

### 3.1 API Key

所有 `/api/v1/` 接口使用 Bearer API Key：

```http
Authorization: Bearer qbk_xxx
```

API Key 由玄龄回测系统用户在系统页面创建。明文只在创建时展示一次，系统 A 应妥善保存。

API Key 无效、已撤销、已过期，或者所属用户已停用时，接口返回 HTTP `401`。

### 3.2 权限范围

| Scope | 用途 |
|---|---|
| `backtest:submit` | 创建回测任务 |
| `backtest:read` | 查询任务、信号和成交 |
| `backtest:cancel` | 取消回测任务 |
| `strategy:read` | 读取策略、归档状态和不可变版本 |
| `strategy:write` | 创建、修改和恢复完整策略 |
| `strategy:publish` | 显式发布或复用当前不可变版本 |
| `strategy:delete` | 可恢复地归档策略，不执行物理删除 |

策略、版本、回测任务、信号和成交查询均按 API Key 所属用户隔离。API Key 不继承管理员权限。已有 Key 不会自动增加策略 Scope，必须由用户重新创建并明确授权。

当前取消接口只校验 `backtest:cancel` Scope 和 `run_id`，尚未按任务所有者过滤。因此只应向受信任的系统 A 调用凭据授予 `backtest:cancel`，并将 `run_id` 视为敏感的不透明标识。

## 4. 通用约定

### 4.1 请求头

| 请求头 | 必填 | 说明 |
|---|---:|---|
| `Authorization` | 是 | Bearer API Key |
| `Content-Type` | POST JSON 接口必填 | `application/json` |
| `Idempotency-Key` | 提交回测及策略 POST/PUT 写接口必填 | 8～128 个字符，建议使用 UUID；查询接口和取消回测不要求 |

### 4.2 日期和数值

- 日期使用 `YYYY-MM-DD`。
- 初始资金必须大于 0。
- 滑点必须大于等于 0。
- 费率使用小数，例如万分之三填写为 `0.0003`。

### 4.3 请求追踪

提交和取消接口的成功响应包含 `request_id`，所有错误响应的错误对象也包含 `request_id`。列表、详情、信号和成交查询的成功响应不包含该字段。向服务方反馈问题时，应提供本次调用可取得的 `request_id` 和相关 `run_id`。

### 4.4 任务状态

任务详情使用以下状态：

| 状态 | 含义 |
|---|---|
| `queued` | 已排队 |
| `running` | 执行中 |
| `cancelling` | 正在取消 |
| `succeeded` | 执行成功 |
| `failed` | 执行失败 |
| `cancelled` | 已取消 |

## 5. 提交回测

### 5.1 接口

```http
POST {BASE_URL}/api/v1/backtests
Authorization: Bearer qbk_xxx
Idempotency-Key: 55932b0f-58f2-48aa-888a-e04780b67fc2
Content-Type: application/json
```

需要 API Key 具备 `backtest:submit` Scope。

### 5.2 请求体

```json
{
  "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
  "overrides": {
    "start_date": "2026-06-01",
    "end_date": "2026-06-30",
    "initial_capital": 1000000,
    "slippage": 0,
    "strategy_params": {
      "window": 10
    }
  }
}
```

字段说明：

| 字段 | 必填 | 约束 | 说明 |
|---|---:|---|---|
| `config_id` | 是 | 1～64 个字符 | 本系统已保存策略方案 ID |
| `overrides` | 否 | object | 本次运行覆盖参数；省略时使用方案保存值 |
| `overrides.start_date` | 否 | `YYYY-MM-DD` | 回测开始日期 |
| `overrides.end_date` | 否 | `YYYY-MM-DD` | 回测结束日期 |
| `overrides.initial_capital` | 否 | 大于 0 | 初始资金 |
| `overrides.slippage` | 否 | 大于等于 0 | 滑点 |
| `overrides.strategy_params` | 否 | object | 只覆盖当前已发布策略声明的参数；名称和类型必须匹配声明 |

覆盖参数只影响本次回测，不修改已保存策略方案。

当前请求模型虽然兼容 `overrides.commission_rate` 字段，但执行校验会统一使用服务端部署的默认手续费率，传入值不会改变实际回测费率。系统 A 不应传递或依赖该字段。

以下内容均从策略方案读取，提交接口不能覆盖：

- 策略脚本及版本；
- 标的和基准标的；
- 回测周期；
- 未声明的策略参数和策略参数声明；
- 多空方向；
- 仓位和底仓配置；
- 撮合模式；
- 执行算法。

### 5.3 cURL 示例

```bash
curl -X POST "{BASE_URL}/api/v1/backtests" \
  -H "Authorization: Bearer qbk_xxx" \
  -H "Idempotency-Key: 55932b0f-58f2-48aa-888a-e04780b67fc2" \
  -H "Content-Type: application/json" \
  -d '{
    "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
    "overrides": {
      "start_date": "2026-06-01",
      "end_date": "2026-06-30",
      "initial_capital": 1000000,
      "slippage": 0,
      "strategy_params": {
        "window": 10
      }
    }
  }'
```

### 5.4 成功响应

新建任务通常返回：

```json
{
  "run_id": "cfe8b82b-52b6-4f16-afe1-f82a06ff017f",
  "display_no": 126,
  "status": "queued",
  "fill_model": "bar_next_open",
  "replayed": false,
  "request_id": "req_01J..."
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string | 回测任务唯一 ID |
| `display_no` | integer | 页面展示编号 |
| `status` | string | 当前任务状态 |
| `fill_model` | string | 方案使用的撮合模式 |
| `replayed` | boolean | 是否为幂等重放返回的已有任务 |
| `request_id` | string | 本次 HTTP 请求追踪 ID |

幂等重放已经结束的任务时，提交响应会把 `succeeded` 映射为 `ok`，把 `failed` 映射为 `error`。任务详情接口仍返回原始状态 `succeeded` 或 `failed`。

## 6. 幂等和提交限额

### 6.1 幂等规则

- `Idempotency-Key` 在同一 API Key 内唯一；
- 相同 Key 和相同请求返回原任务，`replayed=true`；
- 相同 Key 用于不同请求时返回 HTTP `409`；
- 网络超时重试必须复用原 Key；
- 用户主动发起新回测必须生成新 Key；
- 当前接口不公开承诺幂等键自动过期，调用方不得复用已经使用过的 Key。

### 6.2 默认限额

当前默认限额：

| 限额 | 默认值 |
|---|---:|
| 单个 API Key 排队、运行或取消中的任务总数 | 2 |
| 单个 API Key 自然日提交次数 | 100 |

部署环境可以调整以上额度。超过额度时返回 HTTP `429`，且不创建回测任务。

## 7. 查询任务列表

### 7.1 接口

```http
GET {BASE_URL}/api/v1/backtests?limit=20&cursor=0
Authorization: Bearer qbk_xxx
```

需要 API Key 具备 `backtest:read` Scope。

查询参数：

| 参数 | 必填 | 默认值 | 约束 |
|---|---:|---:|---|
| `limit` | 否 | 20 | `1～100` |
| `cursor` | 否 | 0 | 大于等于 0 的整数偏移量 |

### 7.2 响应

```json
{
  "items": [
    {
      "run_id": "cfe8b82b-52b6-4f16-afe1-f82a06ff017f",
      "display_no": 126,
      "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
      "symbol": "000001.SZ",
      "timeframe": "5m",
      "fill_model": "bar_next_open",
      "strategy_category": "directional_holding",
      "overlay_settlement_policy": "intraday_restore",
      "result_schema_version": 1,
      "status": "succeeded",
      "start_date": "2026-06-01",
      "end_date": "2026-06-30",
      "summary": {},
      "created_at": "2026-09-18T10:00:00+08:00",
      "finished_at": "2026-09-18T10:01:20+08:00"
    }
  ],
  "next_cursor": null
}
```

`next_cursor` 为字符串或 `null`。有下一页时，调用方应直接将返回值作为下一次请求的 `cursor`，不要自行计算。

## 8. 查询任务详情

### 8.1 接口

```http
GET {BASE_URL}/api/v1/backtests/{run_id}
Authorization: Bearer qbk_xxx
```

需要 API Key 具备 `backtest:read` Scope。

### 8.2 响应

```json
{
  "run": {
    "run_id": "cfe8b82b-52b6-4f16-afe1-f82a06ff017f",
    "display_no": 126,
    "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
    "symbol": "000001.SZ",
    "timeframe": "5m",
    "fill_model": "bar_next_open",
    "strategy_category": "directional_holding",
    "overlay_settlement_policy": "intraday_restore",
    "result_schema_version": 1,
    "status": "succeeded",
    "start_date": "2026-06-01",
    "end_date": "2026-06-30",
    "summary": {
      "totalPnl": 15200.5,
      "totalReturn": 0.0152,
      "maxDrawdown": 8300.0,
      "maxDrawdownRate": 0.0081,
      "annualReturn": 0.032,
      "annualSharpe": 1.18,
      "sortino": 1.42,
      "winRate": 0.57,
      "tradeCount": 24,
      "finalEquity": 1015200.5
    },
    "created_at": "2026-09-18T10:00:00+08:00",
    "finished_at": "2026-09-18T10:01:20+08:00"
  }
}
```

说明：

- `summary` 在任务未成功完成或没有概要结果时为空对象；
- `finished_at` 在任务未结束时为 `null`；
- `strategy_category`、`overlay_settlement_policy` 和 `result_schema_version` 对部分历史任务可能为 `null`；
- 详情接口不返回完整信号和成交数组，应使用分页接口查询。

## 9. 查询信号明细

### 9.1 接口

```http
GET {BASE_URL}/api/v1/backtests/{run_id}/signals?limit=100&cursor=0
Authorization: Bearer qbk_xxx
```

需要 API Key 具备 `backtest:read` Scope。

查询参数：

| 参数 | 必填 | 默认值 | 约束 |
|---|---:|---:|---|
| `limit` | 否 | 100 | `1～200` |
| `cursor` | 否 | 0 | 大于等于 0 的整数偏移量 |

### 9.2 响应

```json
{
  "items": [],
  "next_cursor": null,
  "total": 0
}
```

当前接口将结果快照中的以下两类记录合并后按时间排序：

- `type=execution_plan`：执行计划记录；
- `type=skipped_signal`：被策略配置跳过的信号记录。

除 `type` 外，各项保留对应 `result_schema_version` 中的原始结果字段。调用方应根据任务详情中的 `result_schema_version` 处理字段兼容性。任务尚未生成结果时，接口返回空列表。

## 10. 查询成交明细

### 10.1 接口

```http
GET {BASE_URL}/api/v1/backtests/{run_id}/trades?limit=100&cursor=0
Authorization: Bearer qbk_xxx
```

需要 API Key 具备 `backtest:read` Scope。

查询参数：

| 参数 | 必填 | 默认值 | 约束 |
|---|---:|---:|---|
| `limit` | 否 | 100 | `1～200` |
| `cursor` | 否 | 0 | 大于等于 0 的整数偏移量 |

### 10.2 响应

```json
{
  "items": [],
  "next_cursor": null,
  "total": 0
}
```

`items` 直接来自该任务不可变结果快照中的 `trades`。调用方应根据任务详情中的 `result_schema_version` 处理字段兼容性。任务尚未生成结果时，接口返回空列表。

## 11. 取消任务

### 11.1 接口

```http
POST {BASE_URL}/api/v1/backtests/{run_id}/cancel
Authorization: Bearer qbk_xxx
```

需要 API Key 具备 `backtest:cancel` Scope。

### 11.2 响应

```json
{
  "run_id": "cfe8b82b-52b6-4f16-afe1-f82a06ff017f",
  "status": "cancelling",
  "request_id": "req_01J..."
}
```

取消语义：

- `queued` 任务取消后返回 `cancelled`；
- `running` 任务取消后返回 `cancelling`，Runner 随后停止任务；
- 重复取消返回数据库中的当前状态；
- 已进入终态的任务返回当前终态，不重新执行取消；
- 任务 ID 不存在时返回 HTTP `404`。

## 12. 错误响应

所有 `/api/v1/` 错误使用统一结构：

```json
{
  "error": {
    "code": "backtest.validation_failed",
    "message": "请求参数不符合接口约束",
    "retryable": false,
    "request_id": "req_01J..."
  }
}
```

当前错误码：

| HTTP | Code | 常见场景 |
|---:|---|---|
| 401 | `api_key_invalid` | Key 无效、已撤销、已过期或所属用户已停用 |
| 403 | `api_scope_denied` | Key 缺少接口要求的 Scope |
| 404 | `api.disabled` | 当前部署未启用开放 API |
| 404 | `resource.not_found` | 提交或读取接口中的策略方案、回测任务不存在，或者不属于当前用户 |
| 404 | `strategy.not_found` | 策略或版本不存在，或者不属于当前用户 |
| 409 | `strategy.revision_conflict` | `expected_revision` 已过期，应重新读取策略后再决定是否重试 |
| 409 | `strategy.name_conflict` | 恢复归档策略时已有同名活动策略 |
| 409 | `strategy.state_conflict` | 策略当前状态不允许该操作，或完整策略身份不一致 |
| 409 | `backtest.strategy_unavailable` | 策略未启用或尚未发布，不能提交新回测 |
| 409 | `idempotency.conflict` | 同一幂等键用于不同请求 |
| 422 | `backtest.validation_failed` | 请求字段、方案参数或数据覆盖不符合要求 |
| 422 | `strategy.validation_failed` | 策略代码、结构、日期、标的或回测配置无效 |
| 422 | `strategy.parameter_invalid` | 策略参数未声明或值与声明类型不匹配 |
| 429 | `rate_limit.exceeded` | 活动任务数或当日提交数达到上限 |
| 503 | `request.failed` | 回测 Runner 暂时不可用 |
| 503 | `backtest.unavailable` | 接口发生未预期的服务异常 |

`retryable` 当前规则：

- HTTP `503` 为 `true`；
- 其他上述错误为 `false`。

接口不会向系统 A 返回服务器堆栈和内部文件路径。

## 13. 系统 A 接入前置条件

系统 A 接入前需要从玄龄回测系统负责人处取得：

- 联调环境 `{BASE_URL}`；
- 具备所需 Scope 的 API Key；
- 可用于联调的已保存、已启用、已发布策略方案 `config_id`；
- 需要维护策略时，具备对应策略 Scope 的独立 API Key；
- 该方案的标的、周期、撮合模式及允许覆盖参数说明；
- 当前部署环境的提交额度；
- 问题反馈联系人。

系统 A 需要实现：

- API Key 安全存储和轮换；
- 每次业务提交生成唯一 `Idempotency-Key`；
- 网络超时后复用原 Key 重试；
- 根据 `run_id` 轮询任务状态；
- 到达 `succeeded`、`failed` 或 `cancelled` 后停止轮询；
- 使用返回的 `next_cursor` 分页读取结果；
- 在问题日志中记录 `request_id` 和 `run_id`，但不得记录完整 API Key。

## 14. 最小联调流程

1. 玄龄回测系统负责人创建或确认一个已发布策略方案，并提供 `config_id`。
2. 系统 A 使用唯一 `Idempotency-Key` 调用 `POST /api/v1/backtests`。
3. 系统 A 保存响应中的 `run_id` 和 `request_id`。
4. 系统 A 调用 `GET /api/v1/backtests/{run_id}` 轮询任务状态。
5. 任务成功后，系统 A 分页读取 signals 和 trades。
6. 重复使用相同请求和相同 `Idempotency-Key`，确认返回原任务且 `replayed=true`。
7. 使用新的 `Idempotency-Key` 创建任务并调用取消接口，确认状态进入 `cancelled` 或 `cancelling`。
8. 使用缺少 Scope 的 Key 验证 HTTP `403`。
9. 使用不存在或不属于当前用户的 `config_id` 验证 HTTP `404`。

## 15. 完整策略管理

### 15.1 参数声明与完整策略模型

策略代码使用模块顶层 `STRATEGY_PARAMETERS` 字典声明允许保存和单次覆盖的参数：

```python
STRATEGY_PARAMETERS = {
  "window": 20,
  "threshold": 0.5,
  "enabled": True,
  "label": "breakout",
}

def on_bar(bar, state):
  params = state["params"]
  return []
```

支持 `integer`、有限 `number`、`boolean` 和 `string`。最多 64 个参数，名称必须匹配 `^[A-Za-z][A-Za-z0-9_]{0,63}$`，单个字符串最长 1024 个字符，规范化声明最大 64 KiB。表达式、变量引用、函数调用、列表、`None` 和嵌套对象会被拒绝。未声明参数的旧策略继续按空自定义参数运行。

创建和完整更新使用同一主体。创建时不传 `expected_revision`，更新时必须传：

```json
{
  "name": "均线策略",
  "description": "均线方向策略",
  "code": "STRATEGY_PARAMETERS = {\"window\": 20}\n\ndef on_bar(bar, state):\n  return []",
  "entry_type": "on_bar",
  "signal_required": false,
  "symbol": "688981.SH",
  "timeframe": "5m",
  "benchmark_symbol": "688981.SH",
  "enabled": true,
  "backtest_parameters": {
    "start_date": "2026-06-01",
    "end_date": "2026-06-30",
    "initial_capital": 1000000,
    "commission_rate": 0.0003,
    "slippage": 0,
    "allow_long": true,
    "allow_short": false,
    "strategy_category": "directional_holding",
    "holding_policy": "overnight",
    "overlay_settlement_policy": "intraday_restore",
    "fill_model": "bar_next_open",
    "execution_algorithm": "immediate",
    "execution_parameters": {},
    "base_quantity": 0,
    "base_cost_price": 0,
    "initial_position_enabled": false,
    "t0_enabled": false
  },
  "strategy_parameters": {
    "window": 10
  }
}
```

`fill_model` 支持 `bar_next_open` 或 `l2_quote`；`execution_algorithm` 支持 `immediate` 或 `twap`。TWAP 的 `execution_parameters` 使用 `duration_seconds`、`interval_seconds` 和 `min_child_quantity`。保存时，明确提供的 `strategy_parameters` 优先；未提供时保留同名同类型旧值，否则使用代码声明默认值。删除的声明从当前默认值中移除。

### 15.2 分页查询策略

```http
GET {BASE_URL}/api/v1/strategies?limit=20&cursor=0&archived=false&enabled=true
```

需要 `strategy:read`。`limit` 为 `1～100`，默认 `20`；`cursor` 是大于等于 `0` 的偏移量；`archived` 和 `enabled` 可省略。响应：

```json
{
  "items": [
    {
      "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
      "script_id": "8ea79532-a885-49ab-a623-69e51647f12d",
      "strategy_version_id": "d799136a-bf0c-4474-a814-a8a864e751cd",
      "published_version_id": "62737778-daf6-431d-b4c4-09b558d317ea",
      "revision": 3,
      "name": "均线策略",
      "description": "均线方向策略",
      "code": "STRATEGY_PARAMETERS = {\"window\": 20}\n\ndef on_bar(bar, state):\n  return []",
      "entry_type": "on_bar",
      "signal_required": false,
      "mode": "backtest",
      "symbol": "688981.SH",
      "timeframe": "5m",
      "benchmark_symbol": "688981.SH",
      "enabled": true,
      "published": true,
      "archived": false,
      "archived_at": null,
      "backtest_parameters": {
        "start_date": "2026-06-01",
        "end_date": "2026-06-30",
        "initial_capital": 1000000,
        "commission_rate": 0.0003,
        "slippage": 0,
        "allow_long": true,
        "allow_short": false,
        "strategy_category": "directional_holding",
        "holding_policy": "overnight",
        "overlay_settlement_policy": "intraday_restore",
        "fill_model": "bar_next_open",
        "execution_algorithm": "immediate",
        "execution_parameters": {},
        "base_quantity": 0,
        "base_cost_price": 0,
        "initial_position_enabled": false,
        "t0_enabled": false
      },
      "parameter_definitions": {
        "window": {"type": "integer", "default": 20}
      },
      "strategy_parameters": {"window": 10},
      "created_at": "2026-09-28T10:00:00+08:00",
      "updated_at": "2026-09-28T10:05:00+08:00"
    }
  ],
  "next_cursor": null,
  "total": 1
}
```

`next_cursor` 为字符串或 `null`。有下一页时直接把返回值用于下一次 `cursor`。可直接运行的 cURL：

```bash
curl -sS "{BASE_URL}/api/v1/strategies?limit=20&cursor=0&archived=false" \
  -H "Authorization: Bearer qbk_xxx"
```

### 15.3 读取单个策略

```http
GET {BASE_URL}/api/v1/strategies/{config_id}
```

需要 `strategy:read`。响应为 `{"strategy": <15.2 items 中的完整策略>}`，包括当前 `revision`。归档策略仍可由所有者读取；归档策略不能用于新回测。

```bash
curl -sS "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9" \
  -H "Authorization: Bearer qbk_xxx"
```

### 15.4 创建策略

```http
POST {BASE_URL}/api/v1/strategies
```

需要 `strategy:write`、`Content-Type: application/json` 和 `Idempotency-Key`。请求体使用 15.1 的创建主体。成功返回 HTTP `201`：

```json
{
  "strategy": {
    "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
    "script_id": "8ea79532-a885-49ab-a623-69e51647f12d",
    "strategy_version_id": "d799136a-bf0c-4474-a814-a8a864e751cd",
    "published_version_id": "62737778-daf6-431d-b4c4-09b558d317ea",
    "revision": 1
  },
  "replayed": false,
  "request_id": "req_01J..."
}
```

```bash
curl -sS -X POST "{BASE_URL}/api/v1/strategies" \
  -H "Authorization: Bearer qbk_xxx" \
  -H "Idempotency-Key: 52923cb2-74c5-4a3b-a3e1-0a0b154f44b7" \
  -H "Content-Type: application/json" \
  --data-binary @strategy-create.json
```

### 15.5 完整更新策略

```http
PUT {BASE_URL}/api/v1/strategies/{config_id}
```

需要 `strategy:write` 和 `Idempotency-Key`。请求体与 15.1 相同，并增加从最新读取结果取得的 `"expected_revision": 3`。成功返回 HTTP `200`，响应结构与创建相同，revision 已递增。更新成功后会冻结当前完整发布版本；相同脚本内容会复用不可变脚本版本。

```bash
curl -sS -X PUT "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9" \
  -H "Authorization: Bearer qbk_xxx" \
  -H "Idempotency-Key: 169450d2-15d0-47d9-8bdd-b541819c12cc" \
  -H "Content-Type: application/json" \
  --data-binary @strategy-update.json
```

### 15.6 发布、归档和恢复

三个接口都接收 `{"expected_revision": 3}`，都要求新的 `Idempotency-Key`，成功返回 HTTP `200`。

| 接口 | Scope | 结果 |
|---|---|---|
| `POST /api/v1/strategies/{config_id}/publish` | `strategy:publish` | 发布当前内容；内容未变化时复用版本并返回 `created=false` |
| `POST /api/v1/strategies/{config_id}/archive` | `strategy:delete` | 设置归档、取消发布并递增 revision，不删除历史 |
| `POST /api/v1/strategies/{config_id}/restore` | `strategy:write` | 恢复并重新启用发布；同名活动策略存在时返回名称冲突 |

发布响应示例：

```json
{
  "strategy": {
    "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
    "revision": 4,
    "published_version_id": "62737778-daf6-431d-b4c4-09b558d317ea"
  },
  "created": false,
  "replayed": false,
  "request_id": "req_01J..."
}
```

```bash
curl -sS -X POST "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9/publish" \
  -H "Authorization: Bearer qbk_xxx" -H "Idempotency-Key: 39cde407-b68b-4c71-a7d1-cdf2962086f2" \
  -H "Content-Type: application/json" -d '{"expected_revision":3}'

curl -sS -X POST "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9/archive" \
  -H "Authorization: Bearer qbk_xxx" -H "Idempotency-Key: 00423566-8e6e-4654-82f1-965fc24717d8" \
  -H "Content-Type: application/json" -d '{"expected_revision":4}'

curl -sS -X POST "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9/restore" \
  -H "Authorization: Bearer qbk_xxx" -H "Idempotency-Key: 4e5490bf-f0d9-4d9b-ae34-c7cca0c435c7" \
  -H "Content-Type: application/json" -d '{"expected_revision":5}'
```

归档和恢复响应中的 `strategy` 使用 15.4 的资源引用结构。归档不会删除策略版本、回测任务或共享快照。

### 15.7 查询版本历史和单个版本

```http
GET {BASE_URL}/api/v1/strategies/{config_id}/versions?limit=20&cursor=0
GET {BASE_URL}/api/v1/strategies/{config_id}/versions/{version_id}
```

都需要 `strategy:read`。列表响应使用 `items`、`next_cursor` 和 `total`；分页参数与 15.2 相同。单个版本响应为 `{"version": <版本>}`。版本字段包括 `version_id`、`version_no`、`config_id`、`strategy_version_id`、`name`、冻结代码、入口、信号声明、标的、周期、基准、`backtest_parameters`、`parameter_definitions`、`strategy_parameters` 和 `created_at`。

```bash
curl -sS "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9/versions?limit=20&cursor=0" \
  -H "Authorization: Bearer qbk_xxx"

curl -sS "{BASE_URL}/api/v1/strategies/932a0ce2-9d8d-4060-8614-5df0adbbf8a9/versions/62737778-daf6-431d-b4c4-09b558d317ea" \
  -H "Authorization: Bearer qbk_xxx"
```

### 15.8 revision、幂等和安全日志

- 修改、发布、归档和恢复必须使用最新 `expected_revision`。冲突后先重新 GET，不能盲目改用更大的数字重试。
- 策略写入的幂等记录和资源变更在同一数据库事务中完成。相同 Key 与相同方法、路径、正文重放首次最小响应，`replayed=true`；资源后来更新或归档也不改变该重放结果。
- 不同请求复用同一 Key 返回 `409 idempotency.conflict`。网络超时重试必须复用原 Key，新的业务操作必须生成新 Key。
- 审计只记录操作类型、资源类型、资源 ID、请求摘要和结果，不记录策略源码与完整参数。
- 不存在与跨用户访问都返回 `404 strategy.not_found`。API Key 所属账号即使是管理员，也不会绕过 Scope 或所有权隔离。

### 15.9 建议联调顺序

1. 用 `strategy:write` 创建策略，再用 `strategy:read` 读取并保存 `config_id` 和 `revision`。
2. 使用读取结果完整更新，验证过期 revision 返回 `strategy.revision_conflict`。
3. 显式发布并重复同一幂等请求，确认 `replayed=true`；再查询版本列表。
4. 使用 `POST /api/v1/backtests` 的 `overrides.strategy_params` 做一次局部覆盖，确认它不修改策略读取结果中的 `strategy_parameters`。
5. 归档后确认活动列表不再返回该策略、`archived=true` 查询可见、历史版本仍可读且新回测被拒绝。
6. 恢复后重新读取最新 revision，并验证同名冲突不会静默改名。
