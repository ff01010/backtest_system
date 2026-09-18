# 系统 A 回测开放 API 对接文档

## 1. 文档信息

- 文档版本：`1.0`
- 接口版本：`v1`
- 接口状态：当前已实现
- 调用方：系统 A 策略 Hub
- 服务方：玄龄回测系统
- 基础地址：由部署环境提供，本文使用 `{BASE_URL}`
- 数据格式：`application/json`

本文只描述玄龄回测系统当前已经实现的开放 API。

## 2. 当前能力范围

系统 A 可以通过开放 API：

- 引用玄龄回测系统中已经保存的策略方案提交回测；
- 覆盖本次回测的日期、初始资金和滑点；
- 查询当前 API Key 所属用户的回测任务；
- 查询回测任务详情；
- 分页查询信号和成交明细；
- 取消排队中或运行中的任务。

提交接口以本系统策略方案 ID 作为策略入口，标的、周期和策略逻辑均从方案读取。策略方案必须满足：

- 属于当前 API Key 所属用户；
- 已启用；
- 已发布；
- 已保存完整的策略、标的、周期和运行参数。

策略方案通过玄龄回测系统页面创建和维护，当前开放 API 不提供策略方案新增或修改接口。

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

策略方案提交、任务列表、任务详情、信号和成交查询均按 API Key 所属用户隔离。API Key 不继承管理员权限。

当前取消接口只校验 `backtest:cancel` Scope 和 `run_id`，尚未按任务所有者过滤。因此只应向受信任的系统 A 调用凭据授予 `backtest:cancel`，并将 `run_id` 视为敏感的不透明标识。

## 4. 通用约定

### 4.1 请求头

| 请求头 | 必填 | 说明 |
|---|---:|---|
| `Authorization` | 是 | Bearer API Key |
| `Content-Type` | POST JSON 接口必填 | `application/json` |
| `Idempotency-Key` | 提交回测时必填 | 8～128 个字符，建议使用 UUID |

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
    "slippage": 0
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

覆盖参数只影响本次回测，不修改已保存策略方案。

当前请求模型虽然兼容 `overrides.commission_rate` 字段，但执行校验会统一使用服务端部署的默认手续费率，传入值不会改变实际回测费率。系统 A 不应传递或依赖该字段。

以下内容均从策略方案读取，提交接口不能覆盖：

- 策略脚本及版本；
- 标的和基准标的；
- 回测周期；
- 策略参数；
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
      "slippage": 0
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
| 409 | `idempotency.conflict` | 幂等键冲突，或策略方案未启用、未发布 |
| 422 | `backtest.validation_failed` | 请求字段、方案参数或数据覆盖不符合要求 |
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
