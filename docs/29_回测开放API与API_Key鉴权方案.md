# 回测开放 API 与 API Key 鉴权方案

## 1. 背景与结论

当前网页通过登录 Cookie 调用 `/api/backtest/run` 提交回测。任务、结果和权限已经按用户隔离，但外部程序无法安全、稳定地调用该接口：Cookie 面向浏览器、请求体包含过多网页内部字段，网络重试也可能重复创建任务。

本方案新增 `/api/v1` 版本化开放 API，以 API Key 代表具体用户调用现有回测服务。API 与网页共用策略方案、任务表、PostgreSQL 队列、Bar runner、L2 runner 和结果文件；不会新增另一套回测执行链路。

首期结论：

- 外部 API 仅能引用已保存的策略方案，不接收任意脚本文本。
- 回测均为异步任务；提交立即返回任务标识，调用方轮询查询状态和结果。
- API 创建的任务归属 Key 所属用户，在现有“历史回测”页面可查看相同结果。
- 使用可撤销、可过期、仅展示一次明文的 API Key；数据库只保存密钥摘要。
- 使用幂等键避免网络重试或调用方重复提交产生多个任务。

## 2. 范围

### 2.1 首期范围

1. 用户创建、查看、撤销自身 API Key。
2. API Key 鉴权及权限范围校验。
3. 提交、列表查询、详情查询、取消回测。
4. 成交/信号明细分页读取。
5. API 创建的回测任务在现有网页历史回测中正常展示。
6. API 请求幂等、用户隔离、限额和审计。
7. OpenAPI 文档与调用示例。

### 2.2 不在首期范围

- API 直接上传、修改或执行未保存的策略脚本。
- Webhook 回调、SSE/WebSocket 推送。
- 策略方案、证券目录和账户的完整 CRUD API。
- 实盘下单、QMT/券商交易接口调用。
- 团队共享 Key、子账号委派、细粒度标的白名单。
- VWAP、POV 等执行算法的新增实现。

## 3. 现状与约束

### 3.1 当前链路

```text
网页 Cookie
  -> /api/backtest/run
  -> 参数校验、策略快照、创建 backtest_run
  -> PostgreSQL 队列
  -> Bar runner 或 L2 runner
  -> PostgreSQL 结果 + 信号/结果文件
  -> 网页历史回测展示
```

- 当前认证为 HttpOnly Cookie 会话，后端中间件将当前用户写入数据库用户上下文。
- `backtest_run` 已具有用户归属、任务状态、参数快照和结果快照，L2 任务已有公平领取与并发控制。
- 任务结果可能很大，API 不能把所有成交、信号和可视化数据一次性返回。
- 服务器发布必须通过 `scripts/deploy-server.sh` 同批更新 backend、bar-runner、l2-runner、strategy-worker 和前端；若重建 runner 会影响运行中任务，发布前须检查队列与运行状态。

### 3.2 设计约束

1. 网页 Cookie 接口保持兼容，不能为了开放 API 改变现有登录行为。
2. API 只读取或创建调用方自己的资源；管理员接口与普通 API Key 严格分离。
3. 同一回测必须保存策略、配置、执行算法和数据版本快照，历史结果不能因后续编辑而变化。
4. 调用方重试同一请求不得重复占用回测资源。
5. 错误响应对外只提供安全的分类码和提示；原始堆栈仍只保存在管理员系统日志。

## 4. 总体架构

```text
外部程序
  | Authorization: Bearer qbk_...
  v
/api/v1 路由与 API Key 鉴权
  | 解析用户、权限范围、限额、幂等键
  v
共享回测提交服务
  | 读取已保存的策略方案与脚本快照
  v
backtest_run / bar_backtest_job / l2_backtest_job
  v
现有 PostgreSQL 队列与 Bar/L2 runner
  v
现有网页历史回测 + /api/v1 查询接口
```

### 4.1 复用边界

开放 API 不直接调用 runner，也不绕过网页现有的策略、参数、数据覆盖校验。新增共享服务负责将“已保存方案 + 允许的实验覆盖参数”构造为规范回测请求，网页接口与开放 API 共用该服务，避免两套校验规则漂移。

### 4.2 API 版本策略

- 首期根路径：`/api/v1`。
- `/api/*` 保留给网页内部会话接口，不承诺对外兼容。
- 公共响应使用稳定的 snake_case 字段名；破坏性变更只能增加 `/api/v2`。
- 所有响应包含 `request_id`，便于用户、管理员和系统日志关联。

## 5. API Key 设计

### 5.1 Key 形式与存储

明文 Key 仅在创建时返回一次：

```text
qbk_<key_id_prefix>_<random_secret>
```

- `random_secret` 使用密码学安全随机源生成，长度不少于 32 字节。
- 数据库只保存 `key_prefix`、带服务端 pepper 的 HMAC-SHA-256 摘要，不保存明文 Key。
- 比对使用常量时间比较；日志、审计、异常正文和前端状态均不得记录完整 Key。
- Key 可设置名称、权限范围、过期时间和撤销状态；创建后只显示一次明文。

### 5.2 权限范围

首期固定范围：

| Scope | 能力 |
|---|---|
| `backtest:submit` | 创建回测任务 |
| `backtest:read` | 查询本人任务、概要、成交与信号明细 |
| `backtest:cancel` | 取消本人任务 |

创建 Key 时至少选择一个范围；网页用户本身不能通过 API Key 获得管理员权限。

### 5.3 生命周期与审计

- 用户只能管理自己的 Key；管理员只可查看元数据，不可恢复明文。
- 撤销或到期后立即拒绝请求，不影响已经创建的回测任务和历史结果。
- 每次成功鉴权更新 `last_used_at`，但同一 Key 的更新时间按分钟节流，避免高频写放大。
- 记录 Key 创建、撤销、认证失败、限额拒绝和回测提交事件；审计数据不保存请求中的密钥正文。

## 6. 数据库与迁移

新增幂等迁移，不修改已执行迁移。

### 6.1 `api_access_key`

建议字段：

| 字段 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `owner_user_id` | Key 所属用户 |
| `name` | 用户可读名称 |
| `key_prefix` | 用于快速定位与展示的前缀，唯一 |
| `secret_hash` | pepper HMAC 摘要，唯一 |
| `scopes` | JSONB 权限范围 |
| `expires_at` | 可空，到期时间 |
| `revoked_at` | 可空，撤销时间 |
| `last_used_at` | 最后使用时间 |
| `created_at` / `updated_at` | 审计时间 |

建立 `(owner_user_id, created_at DESC)` 和 `key_prefix` 索引，并启用与现有用户资源一致的行级安全策略。

### 6.2 `api_idempotency_record`

以 API Key 与幂等键唯一约束保证提交原子性：

| 字段 | 说明 |
|---|---|
| `api_key_id` | 调用 Key |
| `idempotency_key` | 调用方提供的 8–128 字符键 |
| `request_hash` | 规范化请求摘要 |
| `run_id` | 已创建的 `backtest_run` |
| `created_at` | 创建时间 |

唯一索引为 `(api_key_id, idempotency_key)`：

- 相同键、相同请求：返回原任务，HTTP `200`。
- 相同键、不同请求：拒绝，HTTP `409`，不创建新任务。
- 新键：在同一事务内创建记录与回测任务，HTTP `202`。

### 6.3 审计

首期将 Key 生命周期事件写入独立 `api_access_audit` 表。高频请求不逐条落库，只更新节流后的 `last_used_at`；回测任务自身已保留调用用户、参数和执行记录。

## 7. 认证与用户上下文

### 7.1 鉴权规则

开放接口只接受：

```http
Authorization: Bearer qbk_<key_id_prefix>_<random_secret>
```

- 缺少、格式错误、撤销、过期或摘要不匹配统一返回 `401 api_key_invalid`。
- Key 有效但缺少所需 scope 返回 `403 api_scope_denied`。
- `/api/v1` 不接受 Cookie 作为替代身份，避免浏览器会话意外调用开放接口。
- 鉴权成功后复用现有 `CurrentUser` 与数据库用户上下文，使 `state_store` 的行级安全策略继续生效。

### 7.2 配置

新增私有环境变量：

| 变量 | 用途 | 默认行为 |
|---|---|---|
| `TREND_GRID_PUBLIC_API_ENABLED` | 是否启用 `/api/v1` | `false`，未配置时路由返回 404 |
| `TREND_GRID_API_KEY_PEPPER` | Key 摘要的服务端秘密 | 未配置时拒绝启用开放 API |
| `TREND_GRID_PUBLIC_API_MAX_INFLIGHT` | 单 Key 运行/排队上限 | 使用保守默认值，例如 2 |
| `TREND_GRID_PUBLIC_API_DAILY_SUBMIT_LIMIT` | 单 Key 每日提交上限 | 使用保守默认值，例如 100 |

`.env.example` 仅提供关闭状态和占位说明；服务器 `deploy/.env` 由部署者单独配置，发布脚本不得覆盖私有配置。

## 8. API 契约

### 8.1 提交回测

```http
POST /api/v1/backtests
Authorization: Bearer qbk_...
Idempotency-Key: client-generated-key
Content-Type: application/json
```

```json
{
  "config_id": "932a0ce2-9d8d-4060-8614-5df0adbbf8a9",
  "overrides": {
    "start_date": "2026-06-01",
    "end_date": "2026-06-30",
    "initial_capital": 1000000
  }
}
```

规则：

- `config_id` 必须属于当前 Key 所属用户，且关联已保存策略脚本。
- `overrides` 首期仅允许开始日期、结束日期、初始资金、手续费率与滑点等实验参数；不允许改标的、周期、策略代码、方向、底仓、成交模式或执行算法。
- 若要改变策略、底仓或执行算法，调用方先通过网页保存新的策略方案；首期不开放方案写入 API。
- 服务端从方案快照构造规范请求、校验数据覆盖后创建现有异步任务。

成功响应：

```json
{
  "run_id": "cfe8b82b-52b6-4f16-afe1-f82a06ff017f",
  "display_no": 126,
  "status": "queued",
  "fill_model": "l2_quote",
  "request_id": "req_..."
}
```

### 8.2 任务列表与详情

```http
GET /api/v1/backtests?limit=20&cursor=...
GET /api/v1/backtests/{run_id}
```

列表使用游标分页，默认按创建时间倒序。详情返回任务状态、方案快照摘要、错误分类、概要指标、执行算法摘要和结果可用性；不内联大量成交明细。

### 8.3 成交和信号明细

```http
GET /api/v1/backtests/{run_id}/trades?limit=200&cursor=...
GET /api/v1/backtests/{run_id}/signals?limit=200&cursor=...
```

服务端统一按结果快照读取，返回稳定排序与下一页游标。首期不开放任意文件路径下载，避免结果目录路径泄露。

### 8.4 取消任务

```http
POST /api/v1/backtests/{run_id}/cancel
```

复用现有取消语义：排队任务立即标记取消；运行任务写入取消请求，由 runner 在安全边界停止。已经成功、失败或取消的任务返回当前终态，不重复变更。

### 8.5 Key 管理

网页 Cookie 接口：

```text
POST   /api/api-keys
GET    /api/api-keys
DELETE /api/api-keys/{key_id}
```

创建响应只含一次 `plain_text_key`；列表只返回前缀、名称、范围、过期/撤销状态与最后使用时间。

## 9. 统一错误模型

开放 API 统一返回：

```json
{
  "error": {
    "code": "backtest.validation_failed",
    "message": "回测区间无可用数据。",
    "request_id": "req_...",
    "retryable": false
  }
}
```

建议错误码：

| HTTP | Code | 场景 |
|---|---|---|
| 400 | `request.invalid` | JSON、分页或幂等键格式错误 |
| 401 | `api_key_invalid` | Key 无效、过期或已撤销 |
| 403 | `api_scope_denied` | Key 无权限或访问他人任务 |
| 404 | `resource.not_found` | 方案或任务不存在，避免泄露归属 |
| 409 | `idempotency.conflict` | 同一幂等键对应不同请求 |
| 422 | `backtest.validation_failed` | 策略方案、参数或数据覆盖不满足要求 |
| 429 | `rate_limit.exceeded` | 并发或日提交额度超限 |
| 503 | `backtest.unavailable` | 服务未启用或临时不可用 |

策略错误仍沿用对普通用户安全的分类信息；管理员通过任务号在系统日志查看原始异常，不通过公共 API 返回堆栈。

## 10. 限额与公平性

公平调度决定“已入队任务如何领取”，不限制 API 调用方提交大量任务。首期增加两层保护：

1. 单 Key 排队加运行任务数上限。
2. 单 Key 自然日提交次数上限。

限额在创建任务前使用数据库事务检查；超限不创建 `backtest_run`。该控制只防止 API Key 滥用，不替代现有多用户公平调度，也不改变网页用户的既有使用规则。

## 11. 实施步骤

1. **数据模型与配置**：新增 API Key、幂等记录、审计表迁移与 RLS；新增关闭默认的配置项、启动校验和服务器配置说明。
2. **认证服务**：实现 Key 生成、摘要、解析、过期/撤销校验、scope 校验和用户上下文接入。
3. **共享提交服务**：从保存方案构造规范回测请求，抽离网页与 API 共用的校验、快照、限额、幂等创建逻辑。
4. **开放回测 API**：实现提交、列表、详情、取消、成交和信号分页接口，以及统一错误响应。
5. **网页 Key 管理**：在个人设置增加 Key 列表、创建、一次性展示、复制提醒和撤销操作；不在策略页面暴露 Key。
6. **文档**：补充 OpenAPI、curl/Python 调用示例、认证说明、错误码和迁移说明；明确 API 提交的任务可在网页查看。
7. **测试与发布**：完成下列测试后，按服务器发布约束发布全部依赖服务，并验证网页、API 和历史任务兼容。

## 12. 测试方案

### 12.1 单元与契约测试

- Key 明文只在创建响应出现一次，数据库和日志不存在明文。
- Key 摘要匹配、过期、撤销、错误格式和 scope 拒绝。
- Key 成功认证后数据库行级安全只允许访问所属用户资源。
- 相同幂等键/相同请求返回同一任务；相同键/不同请求返回 409。
- 不同 Key 的幂等键互不冲突。
- 允许与禁止的 `overrides` 均按白名单校验。
- 数据不可用、策略未保存、配置不存在、取消终态等错误码稳定。
- 成交、信号分页无重复、无遗漏、游标不可跨用户复用。

### 12.2 集成测试

- 使用 API Key 创建标准 Bar 回测，网页历史回测能看到相同任务与结果。
- 使用 API Key 创建 L2 立即成交和 L2-TWAP 回测，任务进入现有队列并由 runner 正常执行。
- 网页 Cookie 提交回测保持原有行为。
- 取消 API 任务后，网页与 API 都显示一致终态。
- 同用户多个 Key、不同用户多个 Key 的隔离和公平调度不回退。
- 并发重试、限额边界、Key 撤销后的在途任务和查询行为符合定义。

### 12.3 服务器发布验证

- 发布前检查排队和运行中的 L2 任务，避免静默中断。
- 配置 `TREND_GRID_PUBLIC_API_ENABLED` 与 `TREND_GRID_API_KEY_PEPPER` 后执行健康检查。
- 创建测试 Key，完成一次提交、轮询、网页查看、明细读取、撤销 Key 和无效 Key 拒绝验证。
- 验证旧网页 Cookie 登录、历史回测和 API 未启用环境不受影响。

## 13. 风险与处理

| 风险 | 处理 |
|---|---|
| Key 泄露 | 仅显示一次、摘要存储、可撤销/可过期、最小 scope、日志脱敏 |
| 调用重试重复回测 | 幂等键与请求摘要在同一事务内处理 |
| API 绕过策略快照 | 只接受已保存 `config_id`，由服务端生成快照 |
| 单用户大量提交占用资源 | 单 Key 限额 + 现有多用户公平调度 |
| 大结果响应过慢 | 任务详情与成交/信号明细分离，游标分页 |
| 公开错误泄露内部信息 | 公共错误码与安全提示；原始堆栈只保留管理员日志 |
| 服务器配置缺失 | 默认关闭；缺少 pepper 时拒绝启用；部署前校验 |

## 14. 验收标准

1. 用户可在网页创建、列出和撤销自己的 API Key，明文只出现一次。
2. 外部程序可用 API Key 提交已保存方案的 Bar、L2 立即成交和 L2-TWAP 回测。
3. API 创建的任务在网页历史回测中可见，并与 API 返回相同的任务状态、概要和结果。
4. API 只能访问所属用户任务；无效、过期、撤销和缺少 scope 的 Key 被正确拒绝。
5. 相同 Key 和幂等键不会创建重复任务；冲突请求给出稳定 409。
6. 任务详情、成交与信号明细支持稳定分页，不泄露服务器文件路径或原始内部异常。
7. 现有 Cookie 登录、网页回测、L2 公平调度、历史任务读取与服务器部署流程不回退。

## 15. 后续演进

在首期稳定后，按需求依次评估：

1. 回测完成/失败 Webhook 与签名校验。
2. 策略方案、策略版本和账户的受限管理 API。
3. 更细粒度限额、团队服务账号和 API 使用统计。
4. SSE/WebSocket 任务进度推送。
5. 研究任务批量提交和参数组合优化接口。
