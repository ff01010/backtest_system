# Agent 策略参数网格搜索与 API 使用说明

## 1. 文档目的

本文说明如何让 Agent 调用玄龄回测系统当前已经实现的接口，完成以下闭环：

1. 查询、创建、更新和发布策略；
2. 读取已发布策略的可调参数目录；
3. 预览并创建有限参数网格实验；
4. 轮询实验和 Trial 状态；
5. 对成功结果运行评价；
6. 由 Agent 根据开发区间、验证区间、收益、回撤和交易数量筛选候选参数。

本文以当前后端路由、请求 Schema 和测试为准。已有的设计文档可能包含后续阶段接口，不能据此假定接口已经开放。

## 2. 最重要的当前边界

策略管理和网格实验目前使用两套不同的接口与鉴权方式。

| 能力 | 路径前缀 | 鉴权 | 是否适合外部 Agent |
| --- | --- | --- | --- |
| 策略增删改查、发布和版本读取 | `/api/v1/strategies` | Bearer API Key | 是 |
| 单次开放回测 | `/api/v1/backtests` | Bearer API Key | 是 |
| 参数网格实验及结果评价 | `/api/backtest/experiments` | 登录 Cookie + CSRF | 仅适合受信任的同源 Agent 或浏览器自动化 |

因此，当前不存在“只凭一个 API Key 完成策略创建和网格实验”的纯开放 API 流程。推荐的现阶段组合方式是：

- Agent 使用 API Key 管理策略；
- Agent 使用同一用户的登录会话调用实验接口；
- API Key 所属用户和登录会话用户必须相同；
- 不应把用户名、密码、API Key、Cookie 或 CSRF Token 写入代码仓库、策略代码、日志或 Agent 提示词。

如果调用方不能安全持有登录会话，应停止在“策略已发布”步骤，由用户通过系统页面创建实验。不要尝试把 Bearer API Key 发送给内部实验接口。

当前网格实验还存在以下限制：

- 只支持 `legacy_single_strategy` 实验对象；
- 只支持已启用、未归档且当前已发布的不可变策略版本；
- 只支持标准 Bar 回测，即 `fill_model=bar_next_open`；
- 只支持 `finite_grid_v1`，并且一个计划只能包含一个 Stage；
- 执行场景只支持无参数的 `immediate`；
- L2、TWAP、任意候选元组搜索和自适应优化不在当前接口范围内；
- 完整基础评价要求所选 Stage 的全部 Trial 都成功；存在成功结果但同时有失败、取消或缺失时，可以显式创建带完成率和维度覆盖门槛的 provisional 临时评价；
- 实验取消、恢复、修订、归档、失败 Trial 重试和结果导出已经提供内部 Cookie + CSRF 路由，但都不是 Bearer API Key 开放接口；运行中取消会沿用普通回测取消语义收敛，不能假定立即终止。

## 3. 核心对象和身份关系

| 名称 | 作用 | 关键字段 |
| --- | --- | --- |
| Strategy | 当前可编辑策略 | `config_id`、`revision` |
| Strategy Version | 已发布的不可变策略版本 | `published_version_id` |
| Experiment Subject | 可用于实验的冻结策略引用 | `subjectId=config_id`、`subjectVersion=published_version_id` |
| Experiment | 一次有限网格搜索 | `experimentId` |
| Revision | 实验当前冻结定义 | `revisionId` |
| Stage | 网格生成与执行阶段 | `stageId` |
| Trial | 一个完整维度组合 | `trialId`、`trialIndex`、`candidateKey` |
| Trial Execution | Trial 对应的普通回测执行 | `executionId`、`runId` |
| Evaluation Spec | 不可变的结果读取和评价规则 | `evaluationSpecId` |
| Evaluation Run | 对冻结结果集合的一次评价 | `evaluationRunId` |

Agent 必须使用服务端返回的 ID，不应根据名称猜测或自行拼接 ID。

## 4. 调用准备

### 4.1 基础地址

本文使用以下占位符：

```text
BASE_URL=https://backtest.example.com
```

生产调用必须使用 HTTPS。`BASE_URL` 不应以 `/` 结尾。

### 4.2 创建 API Key

API Key 可由用户在系统页面创建，也可由已经登录的会话调用：

```http
POST /api/api-keys
Cookie: <登录会话 Cookie>
X-CSRF-Token: <CSRF Token>
Origin: https://backtest.example.com
Content-Type: application/json

{
  "name": "strategy-grid-agent",
  "scopes": [
    "strategy:read",
    "strategy:write",
    "strategy:publish",
    "strategy:delete",
    "backtest:read"
  ]
}
```

明文 Key 只在创建响应中返回一次。本文的策略网格主流程至少需要：

- `strategy:read`
- `strategy:write`
- `strategy:publish`

只有确实需要归档时才授予 `strategy:delete`。只读 Agent 不应获得写入 Scope。

### 4.3 建立实验会话

登录接口不要求 CSRF：

```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "agent_user",
  "password": "由安全凭据存储提供"
}
```

成功响应会设置 HttpOnly 会话 Cookie，并返回：

```json
{
  "user": {
    "id": "用户 UUID",
    "username": "agent_user",
    "displayName": "Agent 用户",
    "role": "user"
  },
  "csrfToken": "CSRF_TOKEN"
}
```

实验写接口必须同时携带：

- 登录响应设置的 Cookie；
- `X-CSRF-Token`；
- 与部署地址同源或在服务端白名单中的 `Origin`；
- 需要幂等保护的接口还必须携带 `Idempotency-Key`。

CSRF 失效后，可在有效 Cookie 会话中调用 `GET /api/auth/csrf` 轮换 Token。收到 `auth.csrf_invalid` 时，应先刷新 CSRF Token；原预览仍在有效期内时可使用原幂等键重试创建，否则重新预览。

## 5. Agent 端到端流程

推荐 Agent 固定执行以下顺序：

1. 使用开放 API 查询或创建策略；
2. 确认策略参数已在 `STRATEGY_PARAMETERS` 中声明；
3. 读取策略并取得最新 `config_id`、`revision` 和 `published_version_id`；
4. 使用登录会话读取 Experiment Subject；
5. 只使用 Subject 返回的 `tunableInputCatalog.inputs[].inputId` 组装参数网格；
6. 调用预览接口，检查 `limitDecision.allowed`、计划 Trial 数和资源估算；
7. 使用预览原始请求、`previewToken` 和 `specHash` 创建实验；
8. 轮询实验详情，直到实验终态或出现需要处理的阻塞；
9. 分页读取全部 Trial，核对成功、失败和取消数量；
10. 全部 Trial 成功后读取评价目录，保存 Evaluation Spec 并创建 Evaluation Run；
11. 轮询评价结果；
12. Agent 基于开发区间和验证区间的一致性生成候选参数报告；
13. 默认只输出建议，不自动更新生产策略。需要写回参数时，重新 GET 最新 revision 后再执行完整更新。

## 6. 策略参数声明

只有策略代码模块顶层 `STRATEGY_PARAMETERS` 中声明的参数才能进入网格搜索。

```python
STRATEGY_PARAMETERS = {
  "fast_window": 5,
  "slow_window": 20,
  "threshold": 0.5,
  "use_filter": True,
  "signal_mode": "close",
}


def on_bar(bar, state):
  params = state["params"]
  fast_window = params["fast_window"]
  slow_window = params["slow_window"]
  threshold = params["threshold"]
  return []
```

声明约束：

- 最多 64 个参数；
- 参数名必须以英文字母开头，只能包含字母、数字和下划线；
- 支持 `int`、有限 `float`、`bool` 和 `str`；
- 不支持列表、嵌套对象、`None`、函数调用或变量引用；
- 字符串最长 1024 个字符；
- 保存策略时传入的 `strategy_parameters` 必须是已声明参数，且类型匹配。

参数声明决定可搜索目录，`strategy_parameters` 决定策略的已保存默认值。实验中的候选值只覆盖对应 Trial，不修改策略资源。

## 7. 策略管理开放 API

### 7.1 通用约定

所有 `/api/v1/` 接口使用：

```http
Authorization: Bearer qbk_xxx
```

策略 POST、PUT 和状态变更接口必须提供 8～128 个字符的 `Idempotency-Key`。网络超时重试必须复用原 Key；新的业务操作必须生成新 Key。

开放 API 字段使用 `snake_case`。

### 7.2 接口一览

| 方法与路径 | Scope | 用途 |
| --- | --- | --- |
| `GET /api/v1/strategies` | `strategy:read` | 分页查询策略 |
| `GET /api/v1/strategies/{config_id}` | `strategy:read` | 读取完整策略及当前 revision |
| `POST /api/v1/strategies` | `strategy:write` | 创建完整策略 |
| `PUT /api/v1/strategies/{config_id}` | `strategy:write` | 按 revision 完整更新策略 |
| `POST /api/v1/strategies/{config_id}/publish` | `strategy:publish` | 显式发布或复用不可变版本 |
| `POST /api/v1/strategies/{config_id}/archive` | `strategy:delete` | 可恢复归档，不物理删除 |
| `POST /api/v1/strategies/{config_id}/restore` | `strategy:write` | 恢复策略 |
| `GET /api/v1/strategies/{config_id}/versions` | `strategy:read` | 查询不可变版本列表 |
| `GET /api/v1/strategies/{config_id}/versions/{version_id}` | `strategy:read` | 读取单个不可变版本 |

### 7.3 查询策略

```bash
curl -sS "${BASE_URL}/api/v1/strategies?limit=20&cursor=0&archived=false&enabled=true" \
  -H "Authorization: Bearer ${API_KEY}"
```

查询参数：

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `limit` | `20` | 1～100 |
| `cursor` | `0` | 大于等于 0 的偏移量 |
| `archived` | 不过滤 | `true` 或 `false` |
| `enabled` | 不过滤 | `true` 或 `false` |

响应包含 `items`、`next_cursor` 和 `total`。Agent 应直接使用 `next_cursor`，不要假定分页规则永远不变。

### 7.4 创建策略

```http
POST /api/v1/strategies
Authorization: Bearer qbk_xxx
Idempotency-Key: <本次创建专用 UUID>
Content-Type: application/json
```

请求示例：

```json
{
  "name": "双均线参数搜索示例",
  "description": "用于 Agent 网格搜索演示",
  "code": "STRATEGY_PARAMETERS = {\"fast_window\": 5, \"slow_window\": 20}\n\ndef on_bar(bar, state):\n  params = state[\"params\"]\n  return []\n",
  "entry_type": "on_bar",
  "signal_required": false,
  "symbol": "688981.SH",
  "timeframe": "5m",
  "benchmark_symbol": "688981.SH",
  "enabled": true,
  "backtest_parameters": {
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
    "initial_capital": 1000000,
    "commission_rate": 0.00008,
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
    "fast_window": 5,
    "slow_window": 20
  }
}
```

成功创建返回 HTTP 201：

```json
{
  "strategy": {
    "config_id": "策略配置 UUID",
    "script_id": "策略脚本 UUID",
    "strategy_version_id": "不可变脚本版本 UUID",
    "published_version_id": "不可变策略配置版本 UUID",
    "revision": 1
  },
  "replayed": false,
  "request_id": "req_xxx"
}
```

创建和完整更新会保存完整策略，并产生当前可发布的不可变版本。Agent 仍应读取策略详情，确认 `published=true`、`enabled=true` 和 `published_version_id` 非空后再创建实验。

### 7.5 完整更新策略

```http
PUT /api/v1/strategies/{config_id}
Authorization: Bearer qbk_xxx
Idempotency-Key: <本次更新专用 UUID>
Content-Type: application/json
```

请求体与创建请求相同，但必须增加：

```json
{
  "expected_revision": 3
}
```

实际调用必须发送完整创建主体与 `expected_revision`，不能只发送局部字段。收到 `strategy.revision_conflict` 后，Agent 应重新 GET 策略、比较用户意图与最新内容，再决定是否重试；禁止盲目把 revision 加一。

### 7.6 发布、归档和恢复

三个接口的请求体相同：

```json
{
  "expected_revision": 3
}
```

每次操作使用新的 `Idempotency-Key`。归档是可恢复状态变更，不是物理删除。已经创建的实验继续使用创建时冻结的策略版本，不会被后续策略更新或归档改写。

### 7.7 读取版本

```bash
curl -sS "${BASE_URL}/api/v1/strategies/${CONFIG_ID}/versions?limit=20&cursor=0" \
  -H "Authorization: Bearer ${API_KEY}"

curl -sS "${BASE_URL}/api/v1/strategies/${CONFIG_ID}/versions/${VERSION_ID}" \
  -H "Authorization: Bearer ${API_KEY}"
```

创建 Experiment Subject 时必须使用策略详情中的当前 `published_version_id`，不能使用 `strategy_version_id`、版本号或 revision 代替。

## 8. 实验 API 通用约定

### 8.1 鉴权与字段风格

实验接口使用登录 Cookie。所有实验写请求都必须带 `X-CSRF-Token` 和合法 `Origin`，需要幂等的写请求还必须带 `Idempotency-Key`。

实验 API 字段使用 `camelCase`。不要把开放策略 API 的 `published_version_id` 原样放进请求；映射关系如下：

| 开放策略字段 | 实验字段 |
| --- | --- |
| `config_id` | `subjectRef.subjectId` |
| `published_version_id` | `subjectRef.subjectVersion` |
| `parameter_definitions` | 以 Subject 返回的 `tunableInputCatalog` 为准 |

### 8.2 接口一览

| 方法与路径 | 用途 | 幂等要求 |
| --- | --- | --- |
| `GET /api/backtest/experiment-subjects/{type}/{id}/versions/{version}` | 读取可实验对象和参数目录 | 只读 |
| `POST /api/backtest/experiments/preview` | 规范化网格并预估 Trial 和资源 | 不要求幂等键 |
| `POST /api/backtest/experiments` | 创建实验 | 必须提供 |
| `GET /api/backtest/experiments/by-idempotency-key/{key}` | 创建响应丢失后找回实验 | 只读 |
| `GET /api/backtest/experiments` | 分页查询实验 | 只读 |
| `GET /api/backtest/experiments/{experimentId}` | 查询实验聚合状态 | 只读，支持 ETag |
| `GET /api/backtest/experiments/{experimentId}/trials` | 分页查询 Trial | 只读 |
| `POST /api/backtest/experiments/{experimentId}/cancel` | 取消尚未物化的实验 | 必须提供 |
| `GET /api/backtest/experiments/{experimentId}/evaluation-catalogs` | 读取评价指标和维度目录 | 只读 |
| `POST /api/backtest/experiments/{experimentId}/evaluation-specs` | 保存评价规则 | 必须提供 |
| `GET /api/backtest/experiments/{experimentId}/evaluation-specs` | 查询评价规则 | 只读 |
| `POST /api/backtest/experiments/{experimentId}/evaluation-runs` | 创建评价运行 | 必须提供 |
| `GET /api/backtest/experiments/{experimentId}/evaluation-runs` | 查询评价运行列表 | 只读 |
| `GET /api/backtest/experiments/{experimentId}/evaluation-runs/{runId}` | 读取评价状态和结果 | 只读 |
| `POST /api/backtest/experiments/{experimentId}/evaluation-runs/{runId}/retry` | 重试失败的评价运行 | 必须提供 |

## 9. 创建参数网格实验

### 9.1 读取 Experiment Subject

```http
GET /api/backtest/experiment-subjects/legacy_single_strategy/{config_id}/versions/{published_version_id}
Cookie: <会话 Cookie>
```

关键响应示例：

```json
{
  "subjectRef": {
    "subjectType": "legacy_single_strategy",
    "subjectId": "策略配置 UUID",
    "subjectVersion": "已发布配置版本 UUID"
  },
  "displayName": "双均线参数搜索示例",
  "templateType": "legacy_single_strategy_v1",
  "fixedStructure": {
    "symbol": "688981.SH",
    "timeframe": "5m",
    "fillModel": "bar_next_open"
  },
  "tunableInputCatalog": {
    "schemaVersion": "tunable_input_catalog_v1",
    "inputs": [
      {
        "inputId": "strategyParameters.fast_window",
        "sourceName": "fast_window",
        "type": "integer",
        "group": "strategyParameters",
        "defaultValue": 5,
        "overridePolicy": "finite_candidates"
      }
    ]
  },
  "defaultExperimentInputs": {
    "strategyParameters": {
      "fast_window": 5,
      "slow_window": 20
    },
    "dateScenario": {},
    "initialCapital": "1000000",
    "slippage": "0"
  }
}
```

Agent 必须根据 `inputId`、`type`、默认值以及服务端可能返回的 `min`、`max`、`options`、`minLength`、`maxLength` 约束生成候选值。不要仅根据参数名猜测类型。

### 9.2 构造网格

推荐同时提供开发区间和验证区间，使同一参数组合能在两个互不重叠的时间场景中运行：

```json
{
  "schemaVersion": "experiment_create_v1",
  "name": "双均线稳健性搜索",
  "description": "比较开发区间和验证区间的参数稳定性",
  "subjectRef": {
    "subjectType": "legacy_single_strategy",
    "subjectId": "策略配置 UUID",
    "subjectVersion": "已发布配置版本 UUID"
  },
  "plan": {
    "planVersion": "experiment_plan_v1",
    "stages": [
      {
        "stageType": "finite_grid_v1",
        "generatorVersion": "1",
        "parameterGrid": [
          {
            "inputId": "strategyParameters.fast_window",
            "values": [5, 10, 15]
          },
          {
            "inputId": "strategyParameters.slow_window",
            "values": [20, 30, 60]
          }
        ],
        "dateScenarios": [
          {
            "scenarioId": "development-2024",
            "name": "开发区间",
            "role": "development",
            "startDate": "2024-01-01",
            "endDate": "2024-12-31"
          },
          {
            "scenarioId": "validation-2025",
            "name": "验证区间",
            "role": "validation",
            "startDate": "2025-01-01",
            "endDate": "2025-12-31"
          }
        ],
        "capitalScenarios": ["1000000"],
        "costScenarios": [
          {
            "scenarioId": "base-cost",
            "slippage": "0"
          }
        ],
        "executionScenarios": [
          {
            "scenarioId": "immediate",
            "algorithm": "immediate",
            "parameters": {}
          }
        ]
      }
    ]
  }
}
```

候选值规则：

- `number`、资金和滑点必须使用十进制定点字符串，不要发送 JSON 浮点数；
- `integer` 可发送整数或规范整数字符串，不接受浮点数；
- `boolean` 使用 JSON 布尔值，或字符串 `"true"`、`"false"`；
- `string` 必须发送字符串；
- 初始资金必须大于 0；
- 滑点必须大于等于 0 且小于 1；
- 日期格式为 `YYYY-MM-DD`，开始日期不能晚于结束日期；
- 每类 Scenario 的 `scenarioId` 不能重复；
- 重复候选值会在规范化时去重。

权威 Trial 数由服务端计算：

```text
参数 1 候选数 × 参数 2 候选数 × …
× 日期场景数
× 资金场景数
× 成本场景数
× 执行场景数
```

上述示例计划生成 `3 × 3 × 2 × 1 × 1 × 1 = 18` 个 Trial。Agent 只能把这个公式用于预估，最终必须接受预览响应中的 `plannedTrialCount`。

### 9.3 预览实验

```http
POST /api/backtest/experiments/preview
Cookie: <会话 Cookie>
X-CSRF-Token: <CSRF Token>
Origin: https://backtest.example.com
Content-Type: application/json
```

请求体就是 9.2 的完整 Experiment Spec。关键响应：

```json
{
  "previewToken": "预览 UUID",
  "expiresAt": "2026-09-30T10:15:00+08:00",
  "specHash": "64 位 SHA-256",
  "plannedTrialCount": 18,
  "dimensionCounts": {
    "strategyParameters.fast_window": 3,
    "strategyParameters.slow_window": 3,
    "dateScenario": 2,
    "initialCapital": 1,
    "costScenario": 1,
    "executionScenario": 1
  },
  "resourceEstimate": {
    "estimatedBarCount": 100000,
    "maxBarsPerTrial": 6000,
    "resourceClass": "light",
    "confidence": "coverage_metadata"
  },
  "limitDecision": {
    "allowed": true,
    "warnings": [],
    "blockingReasons": []
  },
  "examples": []
}
```

默认预览有效期由部署配置决定，示例部署值为 900 秒。Agent 应按 `expiresAt` 判断，不应硬编码 15 分钟。

只有 `limitDecision.allowed=true` 时才能创建。若为 `false`，Agent 应根据 `blockingReasons` 缩小网格、日期范围或场景数量后重新预览，不能自行截断 Trial。

### 9.4 创建实验

创建请求必须再次携带与预览完全一致的原始 Experiment Spec：

```http
POST /api/backtest/experiments
Cookie: <会话 Cookie>
X-CSRF-Token: <CSRF Token>
Origin: https://backtest.example.com
Idempotency-Key: <本次创建专用 UUID>
Content-Type: application/json
```

```json
{
  "previewToken": "10000000-0000-4000-8000-000000000001",
  "specHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "experimentSpec": {
    "schemaVersion": "experiment_create_v1",
    "name": "双均线稳健性搜索",
    "description": "比较开发区间和验证区间的参数稳定性",
    "subjectRef": {
      "subjectType": "legacy_single_strategy",
      "subjectId": "30000000-0000-4000-8000-000000000001",
      "subjectVersion": "40000000-0000-4000-8000-000000000001"
    },
    "plan": {
      "planVersion": "experiment_plan_v1",
      "stages": [
        {
          "stageType": "finite_grid_v1",
          "generatorVersion": "1",
          "parameterGrid": [
            {
              "inputId": "strategyParameters.fast_window",
              "values": [5, 10, 15]
            },
            {
              "inputId": "strategyParameters.slow_window",
              "values": [20, 30, 60]
            }
          ],
          "dateScenarios": [
            {
              "scenarioId": "development-2024",
              "name": "开发区间",
              "role": "development",
              "startDate": "2024-01-01",
              "endDate": "2024-12-31"
            },
            {
              "scenarioId": "validation-2025",
              "name": "验证区间",
              "role": "validation",
              "startDate": "2025-01-01",
              "endDate": "2025-12-31"
            }
          ],
          "capitalScenarios": ["1000000"],
          "costScenarios": [
            {
              "scenarioId": "base-cost",
              "slippage": "0"
            }
          ],
          "executionScenarios": [
            {
              "scenarioId": "immediate",
              "algorithm": "immediate",
              "parameters": {}
            }
          ]
        }
      ]
    }
  }
}
```

`experimentSpec` 必须与预览时的原始请求完全一致；上例中的 Token 和 Hash 需要替换为预览响应值。创建成功首次返回 HTTP 201：

```json
{
  "experimentId": "实验 UUID",
  "revisionId": "实验修订 UUID",
  "stageId": "Stage UUID",
  "status": "preparing",
  "plannedTrialCount": 18,
  "generatedTrialCount": 0,
  "specHash": "64 位 SHA-256",
  "replayed": false,
  "createdAt": "2026-09-30T10:01:00+08:00"
}
```

若网络超时导致创建结果不确定，不要生成新 Key 或重新预览。先调用：

```http
GET /api/backtest/experiments/by-idempotency-key/{原 Idempotency-Key}
```

返回 HTTP 200 表示已找到创建结果；HTTP 202 和 `status=processing` 表示创建仍在处理；HTTP 404 才表示当前用户下没有该创建记录。

## 10. 监控实验和 Trial

### 10.1 实验状态

```http
GET /api/backtest/experiments/{experimentId}
```

常见状态：

| 状态 | 含义 | Agent 行为 |
| --- | --- | --- |
| `preparing` | 协调器正在分块生成 Trial | 继续轮询 |
| `queued` | Trial 已生成，等待或正在逐步物化 | 继续轮询 |
| `running` | 存在运行中的 Trial | 继续轮询 |
| `completed` | 全部 Trial 成功 | 进入评价 |
| `partially_completed` | 有成功结果，也有失败或取消 | 先输出异常摘要并优先重试失败 Trial；只有调用方明确接受临时评价时，才可基于成功结果创建 `provisional` 评价运行 |
| `failed` | 没有形成完整成功集合 | 输出失败摘要 |
| `cancelled` | 实验已取消 | 停止 |
| `generation_blocked` | Trial 生成被阻塞 | 读取 `blockedReason`，请求运维或人工处理 |

详情响应的 `snapshotVersion` 会随状态变化递增，并通过 `ETag` 返回。轮询客户端可以发送 `If-None-Match`，收到 HTTP 304 时复用上次快照。

推荐轮询策略：

- 前 30 秒每 2 秒一次；
- 之后每 5 秒一次；
- 连续长期无变化时退避到 15 秒；
- 收到 429 或可重试的 503 时采用指数退避并增加随机抖动；
- 到达终态立即停止轮询；
- 设置 Agent 自己的最长等待时间，但超时不等于服务端失败。

### 10.2 分页读取 Trial

```http
GET /api/backtest/experiments/{experimentId}/trials?limit=50&status=succeeded
```

`limit` 为 1～200，默认 50。`cursor` 是不透明签名字符串，下一页必须原样使用 `nextCursor`。可选 `status`：

- `pending`
- `queued`
- `running`
- `cancelling`
- `succeeded`
- `failed`
- `cancelled`

关键字段：

| 字段 | 说明 |
| --- | --- |
| `trialIndex` | 稳定的网格展开顺序 |
| `candidateKey` | 同一参数候选跨日期场景的候选身份 |
| `dimensionAssignment` | 本 Trial 的参数和场景组合 |
| `summaryStatus` | Trial 当前聚合状态 |
| `selectedExecution` | 选定的成功执行及结果摘要身份 |
| `runId` | 对应普通回测任务 ID |
| `runUrl` | 页面查看路径 |
| `errorSummary` | 失败摘要 |

不要为每个 Trial 单独高频轮询普通回测接口。先轮询实验聚合详情，终态后再分页读取 Trial。

### 10.3 取消实验

```http
POST /api/backtest/experiments/{experimentId}/cancel
Cookie: <会话 Cookie>
X-CSRF-Token: <CSRF Token>
Origin: https://backtest.example.com
Idempotency-Key: <本次取消专用 UUID>
Content-Type: application/json

{}
```

当前允许取消 `preparing`、`generation_blocked`、`queued` 或 `running` 实验。取消会停止生成和物化新 Trial，并让已有普通回测任务按各自取消语义收敛；Agent 应继续读取实验状态，不应把 HTTP 接受响应理解为全部任务已经同步终止。

## 11. 评价实验结果

### 11.1 前置条件

完整基础评价要求：

- 实验 Stage 状态为 `completed`；
- Trial 数等于计划数；
- 每个 Trial 都有成功的 `selectedExecution`；
- 结果摘要和结果 Hash 完整。

仍在运行的实验不能创建完整基础评价。`partially_completed`、`failed` 或 `cancelled` 只有在仍有成功选定执行时，才可以通过 `allowPartial=true` 显式创建 provisional 临时评价；调用方必须冻结最低完成率和需要的维度覆盖门槛，并把输出当作非最终结果。

### 11.2 获取评价目录

```http
GET /api/backtest/experiments/{experimentId}/evaluation-catalogs
```

Agent 应从响应读取：

- `experimentRevisionId`
- `stages[].stageId`
- `metricCatalog.version`
- `metricCatalog.metrics[]`
- `dimensionCatalog.version`
- `dimensionCatalog.dimensions[]`

常用指标包括：

| metricId | 含义 | 方向 |
| --- | --- | --- |
| `totalReturn` | 总收益率 | 越高越好 |
| `annualReturn` | 年化收益率 | 越高越好 |
| `maxDrawdownRate` | 最大回撤率 | 越低越好 |
| `annualSharpe` | 年化夏普 | 越高越好 |
| `sortino` | 索提诺 | 越高越好 |
| `completedTradeCount` | 完成交易数 | 用作最低样本门槛 |
| `winRate` | 胜率 | 仅作辅助指标 |
| `profitLossRatio` | 盈亏比 | 仅作辅助指标 |

参数维度 ID 的格式通常为 `parameter.strategyParameters.<参数名>`，但仍必须使用目录实际返回值。

### 11.3 保存基础 Evaluation Spec

普通页面当前使用可解释的基础预设：以全部策略参数维度作为候选身份，通过 `candidate_rate_distribution_v1` 对无量纲比率指标形成跨日期、资金、成本和执行场景的冻结分布，聚合夏普中位数、平均收益率、最差回撤率、夏普离散度和成功样本数。Agent 可以直接复用该规则，也可以请求原始评价行自行分析。金额指标不能使用该 Normalizer 跨不同本金聚合。

以下示例展示基础候选聚合的关键结构；实际参数维度必须来自目录：

```json
{
  "schemaVersion": "evaluation_spec_v1",
  "name": "双均线开发验证结果",
  "source": {
    "experimentRevisionId": "10000000-0000-4000-8000-000000000001",
    "stageIds": ["20000000-0000-4000-8000-000000000001"],
    "executionSelection": "selected_execution_at_run_creation"
  },
  "catalogs": {
    "metricCatalogVersion": "metric_catalog_v1",
    "dimensionCatalogVersion": "dimension_catalog_v1",
    "normalizerVersion": "candidate_rate_distribution_v1"
  },
  "completeness": {
    "allowPartial": false,
    "minimumCompletionRate": "1.0000",
    "requiredDimensionCoverage": []
  },
  "columns": [
    "parameter.strategyParameters.fast_window",
    "parameter.strategyParameters.slow_window",
    "medianAnnualSharpe",
    "meanTotalReturn",
    "worstMaxDrawdownRate",
    "annualSharpeDispersion",
    "successfulTrialCount"
  ],
  "derivedMetrics": [],
  "filters": [],
  "groupBy": [
    "parameter.strategyParameters.fast_window",
    "parameter.strategyParameters.slow_window"
  ],
  "aggregates": [
    {"type": "metric_aggregate_v1", "aggregateId": "medianAnnualSharpe", "metricId": "annualSharpe", "function": "median", "percentile": null},
    {"type": "metric_aggregate_v1", "aggregateId": "meanTotalReturn", "metricId": "totalReturn", "function": "mean", "percentile": null},
    {"type": "metric_aggregate_v1", "aggregateId": "worstMaxDrawdownRate", "metricId": "maxDrawdownRate", "function": "max", "percentile": null},
    {"type": "metric_aggregate_v1", "aggregateId": "annualSharpeDispersion", "metricId": "annualSharpe", "function": "sample_stddev", "percentile": null},
    {"type": "metric_aggregate_v1", "aggregateId": "successfulTrialCount", "metricId": "totalReturn", "function": "count", "percentile": null}
  ],
  "groupFilters": [],
  "sort": [
    {"type": "metric_sort_v1", "metricId": "medianAnnualSharpe", "direction": "desc"},
    {"type": "metric_sort_v1", "metricId": "worstMaxDrawdownRate", "direction": "asc"},
    {"type": "metric_sort_v1", "metricId": "annualSharpeDispersion", "direction": "asc"}
  ],
  "visualizations": [],
  "reportSections": []
}
```

保存接口：

```http
POST /api/backtest/experiments/{experimentId}/evaluation-specs
Idempotency-Key: <本次保存专用 UUID>
```

成功首次返回 HTTP 201，并包含 `evaluationSpecId`。相同规范内容可能复用已有 Spec 并返回 HTTP 200。

### 11.4 创建并轮询 Evaluation Run

```http
POST /api/backtest/experiments/{experimentId}/evaluation-runs
Idempotency-Key: <本次评价运行专用 UUID>
Content-Type: application/json

{
  "evaluationSpecId": "评价规则 UUID",
  "forceRecompute": false
}
```

首次创建返回 HTTP 202，状态通常为 `queued`。当相同 `specHash + resultSetHash + pipelineVersion` 已经存在成功报告且 `forceRecompute=false` 时，接口返回 HTTP 200、`replayed=true` 和原 `evaluationRunId`，调用方应直接打开原报告。只有明确需要重复计算证据时才设置 `forceRecompute=true`；该请求会创建新的 EvaluationRun，但不会重新运行 Trial。轮询：

```http
GET /api/backtest/experiments/{experimentId}/evaluation-runs/{evaluationRunId}
```

评价状态为 `queued`、`running`、`succeeded` 或 `failed`。成功时 `output` 至少包含：

- `sourceRowCount`
- `matchedRowCount`
- `columns`
- `rows[]`
- 未分组行的 `trialId`、`runId`、`trialIndex`、`values` 和 `unsupportedReasons`
- 分组候选行的 `groupKey`、`memberCount`、`members[]`、`values` 和 `unsupportedReasons`；`members[]` 保存可下钻的 Trial/Execution/run 身份

指标缺失时保持 `null`，不会自动当作 0。Agent 必须检查 `unsupportedReasons`，不能把缺失指标当成最差或最好值进行静默排序。

评价运行失败时，只能对 `failed` 状态调用：

```http
POST /api/backtest/experiments/{experimentId}/evaluation-runs/{evaluationRunId}/retry
Idempotency-Key: <本次重试专用 UUID>
```

## 12. Agent 参数选择规则

网格搜索的目标不是找到样本内最高收益点，而是找到在不同日期场景中相对稳定的候选区域。推荐 Agent 使用以下规则：

1. 先剔除任一场景中 `completedTradeCount` 低于策略所需最低样本数的组合；
2. 剔除任一场景中关键指标为 `null` 或存在不支持原因的组合；
3. 设置最大可接受回撤门槛；
4. 按参数组合聚合 `development` 和 `validation` 行；
5. 优先选择两个区间收益同号、回撤受控、夏普未明显恶化的组合；
6. 比较相邻参数点，避免选择孤立尖峰；
7. 保留前 3～5 个候选，由人复核成交记录和策略逻辑；
8. 不因一次网格结果自动更新生产策略或实盘参数。

可使用以下保守评分思路生成报告，但不要把它当作系统内置指标：

```text
稳健得分 =
  min(开发区间收益, 验证区间收益)
  - 回撤惩罚
  - 开发/验证收益差异惩罚
  - 参数孤立尖峰惩罚
```

不同日期范围、初始资金、成本或执行语义可能不属于同一指标可比较域。若 API 返回 `experiment.metric_not_comparable`，应拆分评价或只读取原始行后按业务认可的规则分别比较，不能强行归一化。

## 13. Agent 状态机和重试规则

Agent 应持久化以下最小状态，避免进程重启后重复创建资源：

```json
{
  "configId": "策略配置 UUID",
  "publishedVersionId": "已发布版本 UUID",
  "previewToken": "预览 UUID",
  "specHash": "SHA-256",
  "experimentCreateKey": "幂等 UUID",
  "experimentId": "实验 UUID",
  "revisionId": "修订 UUID",
  "stageId": "Stage UUID",
  "evaluationSpecId": "评价规则 UUID",
  "evaluationRunId": "评价运行 UUID"
}
```

推荐状态转换：

| Agent 状态 | 成功后进入 | 可安全重试方式 |
| --- | --- | --- |
| `STRATEGY_READY` | `SUBJECT_READY` | 只读 GET 可重试 |
| `SUBJECT_READY` | `PREVIEW_READY` | 重新预览会得到新 Token |
| `PREVIEW_READY` | `EXPERIMENT_CREATED` | 创建结果不确定时使用原 Key 查询 |
| `EXPERIMENT_CREATED` | `EXPERIMENT_TERMINAL` | GET 详情轮询 |
| `EXPERIMENT_TERMINAL` | `EVALUATION_SPEC_SAVED` | 仅 `completed` 进入评价 |
| `EVALUATION_SPEC_SAVED` | `EVALUATION_RUNNING` | 使用原 Key 找回或查询列表 |
| `EVALUATION_RUNNING` | `REPORT_READY` | GET 详情轮询 |

幂等原则：

- 相同业务意图、相同请求正文和网络重试复用同一 Key；
- 用户改变了参数、日期、成本或操作意图时生成新 Key；
- 同一 Key 用于不同请求会返回 `idempotency.conflict`；
- 不要在失败后无条件生成新 Key，否则可能创建重复实验；
- 预览过期或漂移时需要重新预览，并为新的创建意图生成新 Key。

## 14. Python Agent 参考实现

以下示例展示鉴权、预览、创建、轮询和评价的最小骨架。示例依赖调用方环境中的 `requests`，不要求修改本仓库依赖。

```python
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import requests


TERMINAL_EXPERIMENT_STATUSES = {
  "completed",
  "partially_completed",
  "failed",
  "cancelled",
}
TERMINAL_EVALUATION_STATUSES = {"succeeded", "failed"}


class BacktestAgentClient:
  def __init__(
    self,
    base_url: str,
    api_key: str,
    username: str,
    password: str,
  ) -> None:
    self.base_url = base_url.rstrip("/")
    parsed = urlsplit(self.base_url)
    self.origin = f"{parsed.scheme}://{parsed.netloc}"
    self.api_key = api_key
    self.session = requests.Session()
    response = self.session.post(
      f"{self.base_url}/api/auth/login",
      json={"username": username, "password": password},
      timeout=30,
    )
    response.raise_for_status()
    self.csrf_token = response.json()["csrfToken"]

  def public_get(self, path: str) -> dict[str, Any]:
    response = requests.get(
      f"{self.base_url}{path}",
      headers={"Authorization": f"Bearer {self.api_key}"},
      timeout=30,
    )
    response.raise_for_status()
    return response.json()

  def internal_get(self, path: str) -> dict[str, Any]:
    response = self.session.get(f"{self.base_url}{path}", timeout=30)
    response.raise_for_status()
    return response.json()

  def internal_post(
    self,
    path: str,
    body: dict[str, Any],
    *,
    idempotency_key: str | None = None,
  ) -> dict[str, Any]:
    headers = {
      "Origin": self.origin,
      "X-CSRF-Token": self.csrf_token,
    }
    if idempotency_key is not None:
      headers["Idempotency-Key"] = idempotency_key
    response = self.session.post(
      f"{self.base_url}{path}",
      json=body,
      headers=headers,
      timeout=60,
    )
    if response.status_code == 403:
      payload = response.json()
      if payload.get("error", {}).get("code") == "auth.csrf_invalid":
        self.refresh_csrf()
    response.raise_for_status()
    return response.json()

  def refresh_csrf(self) -> None:
    response = self.session.get(
      f"{self.base_url}/api/auth/csrf",
      timeout=30,
    )
    response.raise_for_status()
    self.csrf_token = response.json()["csrfToken"]

  def wait_experiment(
    self,
    experiment_id: str,
    timeout_seconds: int = 7200,
  ) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    interval = 2
    while time.monotonic() < deadline:
      detail = self.internal_get(
        f"/api/backtest/experiments/{experiment_id}",
      )
      if detail["status"] in TERMINAL_EXPERIMENT_STATUSES:
        return detail
      if detail["status"] == "generation_blocked":
        raise RuntimeError(f"实验生成阻塞: {detail.get('blockedReason')}")
      time.sleep(interval)
      interval = min(15, interval + 1)
    raise TimeoutError("等待实验完成超时；服务端任务可能仍在运行")

  def wait_evaluation(
    self,
    experiment_id: str,
    evaluation_run_id: str,
    timeout_seconds: int = 600,
  ) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
      detail = self.internal_get(
        f"/api/backtest/experiments/{experiment_id}"
        f"/evaluation-runs/{evaluation_run_id}",
      )
      if detail["status"] in TERMINAL_EVALUATION_STATUSES:
        return detail
      time.sleep(2)
    raise TimeoutError("等待评价完成超时")


def main() -> None:
  client = BacktestAgentClient(
    base_url=os.environ["BACKTEST_BASE_URL"],
    api_key=os.environ["BACKTEST_API_KEY"],
    username=os.environ["BACKTEST_USERNAME"],
    password=os.environ["BACKTEST_PASSWORD"],
  )

  strategy = client.public_get(
    f"/api/v1/strategies/{os.environ['BACKTEST_CONFIG_ID']}",
  )["strategy"]
  if (
    not strategy["enabled"]
    or not strategy["published"]
    or not strategy["published_version_id"]
  ):
    raise RuntimeError("策略必须启用并发布")

  subject_ref = {
    "subjectType": "legacy_single_strategy",
    "subjectId": strategy["config_id"],
    "subjectVersion": strategy["published_version_id"],
  }
  subject_path = (
    "/api/backtest/experiment-subjects/legacy_single_strategy/"
    f"{subject_ref['subjectId']}/versions/{subject_ref['subjectVersion']}"
  )
  subject = client.internal_get(subject_path)
  input_ids = {
    item["sourceName"]: item["inputId"]
    for item in subject["tunableInputCatalog"]["inputs"]
  }

  experiment_spec = {
    "schemaVersion": "experiment_create_v1",
    "name": "Agent 双均线网格搜索",
    "description": "开发区间与验证区间稳定性检查",
    "subjectRef": subject_ref,
    "plan": {
      "planVersion": "experiment_plan_v1",
      "stages": [{
        "stageType": "finite_grid_v1",
        "generatorVersion": "1",
        "parameterGrid": [
          {"inputId": input_ids["fast_window"], "values": [5, 10, 15]},
          {"inputId": input_ids["slow_window"], "values": [20, 30, 60]},
        ],
        "dateScenarios": [
          {
            "scenarioId": "development-2024",
            "name": "开发区间",
            "role": "development",
            "startDate": "2024-01-01",
            "endDate": "2024-12-31",
          },
          {
            "scenarioId": "validation-2025",
            "name": "验证区间",
            "role": "validation",
            "startDate": "2025-01-01",
            "endDate": "2025-12-31",
          },
        ],
        "capitalScenarios": ["1000000"],
        "costScenarios": [{"scenarioId": "base-cost", "slippage": "0"}],
        "executionScenarios": [{
          "scenarioId": "immediate",
          "algorithm": "immediate",
          "parameters": {},
        }],
      }],
    },
  }

  preview = client.internal_post(
    "/api/backtest/experiments/preview",
    experiment_spec,
  )
  if not preview["limitDecision"]["allowed"]:
    raise RuntimeError(preview["limitDecision"]["blockingReasons"])

  create_key = str(uuid4())
  created = client.internal_post(
    "/api/backtest/experiments",
    {
      "previewToken": preview["previewToken"],
      "specHash": preview["specHash"],
      "experimentSpec": experiment_spec,
    },
    idempotency_key=create_key,
  )
  detail = client.wait_experiment(created["experimentId"])
  if detail["status"] != "completed":
    raise RuntimeError(f"实验未全部成功: {detail}")

  catalogs = client.internal_get(
    f"/api/backtest/experiments/{created['experimentId']}"
    "/evaluation-catalogs",
  )
  parameter_columns = [
    item["dimensionId"]
    for item in catalogs["dimensionCatalog"]["dimensions"]
    if item["dimensionId"].startswith("parameter.strategyParameters.")
  ]
  evaluation_spec = {
    "schemaVersion": "evaluation_spec_v1",
    "name": "Agent 参数稳定性评价",
    "source": {
      "experimentRevisionId": catalogs["experimentRevisionId"],
      "stageIds": [catalogs["stages"][0]["stageId"]],
      "executionSelection": "selected_execution_at_run_creation",
    },
    "catalogs": {
      "metricCatalogVersion": catalogs["metricCatalog"]["version"],
      "dimensionCatalogVersion": catalogs["dimensionCatalog"]["version"],
      "normalizerVersion": None,
    },
    "completeness": {
      "allowPartial": False,
      "minimumCompletionRate": "1.0000",
      "requiredDimensionCoverage": [],
    },
    "columns": [
      *parameter_columns,
      "dateScenarioId",
      "dateRole",
      "totalReturn",
      "annualSharpe",
      "maxDrawdownRate",
      "completedTradeCount",
    ],
    "derivedMetrics": [],
    "filters": [],
    "groupBy": [],
    "aggregates": [],
    "groupFilters": [],
    "sort": [],
    "visualizations": [],
    "reportSections": [],
  }
  saved_spec = client.internal_post(
    f"/api/backtest/experiments/{created['experimentId']}"
    "/evaluation-specs",
    evaluation_spec,
    idempotency_key=str(uuid4()),
  )
  evaluation_run = client.internal_post(
    f"/api/backtest/experiments/{created['experimentId']}"
    "/evaluation-runs",
    {"evaluationSpecId": saved_spec["evaluationSpecId"]},
    idempotency_key=str(uuid4()),
  )
  result = client.wait_evaluation(
    created["experimentId"],
    evaluation_run["evaluationRunId"],
  )
  if result["status"] != "succeeded":
    raise RuntimeError(result.get("error"))
  print(result["output"]["rows"])


if __name__ == "__main__":
  main()
```

这个示例故意不包含“自动写回最佳参数”。Agent 应先把评价行按参数维度分组，生成开发/验证对照报告，并把建议交给用户确认。

## 15. 错误处理

### 15.1 实验错误格式

```json
{
  "error": {
    "code": "experiment.preview_stale",
    "message": "预览绑定的输入或运行环境已变化，请重新预览",
    "retryable": false,
    "requestId": "req_xxx",
    "details": {}
  }
}
```

常见处理方式：

| HTTP | code | Agent 行为 |
| --- | --- | --- |
| 401 | `auth.unauthorized` | 重新登录，不要继续使用旧 Cookie |
| 403 | `auth.csrf_invalid` | 刷新 CSRF Token；原预览仍有效时可复用原幂等键重试，否则重新预览 |
| 403 | `experiment.forbidden` | 检查策略所有者、启用、归档和当前发布版本 |
| 404 | `experiment.not_found` | 核对资源 ID 和当前用户，不向用户泄露其他所有者信息 |
| 409 | `idempotency.conflict` | 停止；同一 Key 被用于不同请求 |
| 409 | `experiment.preview_stale` | 重新读取 Subject 并重新预览 |
| 410 | `experiment.preview_expired` | 重新预览，并生成新的创建意图 Key |
| 422 | `experiment.validation_failed` | 根据 `details.field` 修正请求，不重试原请求 |
| 409 | `experiment.revision_conflict` | 当前 Revision 已变化；刷新详情并重新确认生命周期命令 |
| 409 | `experiment.lifecycle_conflict` | 生命周期版本已变化；丢弃旧幂等键并重新确认 |
| 503 | `experiment.capacity_unavailable` | 仅表示真实执行前置能力不可用；资源估算不可用本身只产生警告 |

### 15.2 请求追踪

所有 API 响应都会返回 `X-Request-Id` 响应头；错误体也包含 `requestId`。Agent 日志应记录：

- 时间；
- 方法和路径模板；
- HTTP 状态；
- `requestId`；
- 本地操作 ID；
- 资源 ID；
- 脱敏后的错误码。

禁止记录 Authorization、Cookie、CSRF Token、用户名密码、完整策略源码或完整参数快照。

## 16. 上线前检查清单

- API Key Scope 遵循最小权限；
- API Key 用户与实验登录用户一致；
- 策略已启用、未归档且存在当前发布版本；
- 网格只引用 Subject 返回的可调参数；
- Decimal 类字段使用字符串；
- 开发区间与验证区间不重叠；
- 预览允许创建，`plannedTrialCount` 等于完整有限笛卡尔积，预计 Bar 数只作为资源与耗时提示；
- Trial 总数未超过 `2^53 - 1` 跨端精确整数边界，且没有由 Agent 自行拆分、截断或抽样；
- 幂等键已持久化，网络重试不会生成重复资源；
- 轮询具有退避、最长等待和终态判断；
- `partially_completed` 不被误判为完整成功；
- 指标 `null` 和 `unsupportedReasons` 被显式处理；
- Agent 只生成候选建议，参数写回需要用户确认；
- 没有把凭据、用户策略或实验快照写入仓库和持久日志。

## 17. 相关文档

- `docs/47_系统A回测开放API对接文档.md`：开放策略和单次回测 API 的完整对接说明；
- `docs/55_单标的有限网格回测自动化建设方案.md`：实验层的设计、容量、调度和数据模型；
- `docs/玄龄回测系统使用手册.md`：策略代码、页面操作和回测行为说明。
