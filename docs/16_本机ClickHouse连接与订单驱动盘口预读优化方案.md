# 本机 ClickHouse 连接与订单驱动盘口预读优化方案

## 1. 背景与结论

当前回测项目与 ClickHouse 部署在同一台服务器，但 `l2-runner` 使用宿主机局域网地址访问 ClickHouse。该路径并不经过物理网线，但仍会经过容器到宿主机的路由与 NAT；同时依赖宿主机 IP，容器重建或网络调整时不如服务名稳定。

最近成功任务的性能快照还显示，主要耗时在 `sourceReadMs（数据源读取耗时）`。例如 10 个交易日、1 分钟周期的任务中，`sourceReadMs` 约 6.0 秒，总耗时约 11.8 秒，并发生 172 次盘口查询。当前订单驱动回放已经避免了无订单时的完整盘口回放，但预读缓冲只读到“下一根 Bar 结束前”，导致后续 Bar 再次下单时频繁回到 ClickHouse 发起新查询。

本方案的推荐结论是分两步实施并分别实测：

1. 让 `l2-runner` 通过 Docker 内部服务名 `quant-clickhouse:8123` 访问 ClickHouse，先解决部署稳定性与不必要的宿主机转发路径。
2. 在订单驱动模式下，将盘口预读从“当前 Bar 内”扩大为“有上限的后续快照块”，但仍严格按 Bar/盘口事件时间顺序向 worker 暴露数据。这样减少 ClickHouse 查询次数，不将十档盘口写入 Redis。

不承诺固定秒数。是否保留第二步改动，以同一任务的实测耗时、查询次数和回测结果一致性为准。

## 2. 目标、边界与不变量

### 2.1 目标

1. 消除 `l2-runner` 到本机 ClickHouse 的宿主机 IP 依赖。
2. 减少订单驱动模式中重复的十档盘口查询次数。
3. 保持无前视、Bar 回调时机、订单成交、费用、底仓与尾盘处理语义不变。
4. 将连接状态、读取批次、缓冲命中和耗时写入任务性能快照，便于线上复核。

### 2.2 本次范围

- `l2-runner` 与 `quant-clickhouse` 的 Docker 网络和部署前检查。
- `order_driven` 盘口模式的 ClickHouse 批量预读、进程内缓冲与性能统计。
- 相应的单元测试、部署验证和同条件性能对比。

### 2.3 明确不在本次范围

- 不改变 `continuous` 模式及其 `on_quote(quote, state)` 回调。该模式必须完整回放快照，不能跳过或复用订单驱动预读逻辑。
- 不把原始十档快照长期写入 Redis；Redis 仍只缓存版本化派生 Bar。
- 不改 ClickHouse 表结构、排序键、projection、物化视图或远端数据。
- 不改策略接口、前端表单、成交规则、价格比例或风险控制规则。
- 不改为 Unix Socket、共享文件或跨容器共享内存；这些方案增加部署耦合，不能解决当前查询次数过多的问题。

### 2.4 必须保持的不变量

1. 一根已完成 Bar 产生的订单，只能使用严格晚于订单生效时点的盘口成交。
2. 下一根 Bar 到达前的盘口必须先处理；时间晚于该 Bar 的盘口必须在该 Bar 回调之后才能交给 worker。
3. 缓冲中即使已存在未来盘口，也只能由 runner 在正确事件时刻发送给 worker；策略脚本不能读取缓冲内容。
4. 订单仍未完成时必须按时间顺序继续消费后续盘口；无待成交订单时不得主动全量回放盘口。
5. 进程内缓冲随单个交易日/任务释放，不跨任务复用，不作为可追溯数据源。

## 3. 当前实现与问题定位

### 3.1 部署网络

项目部署脚本同时使用 `deploy/compose.infrastructure.yml` 和 `deploy/compose.clickhouse.yml`。后者已将 `l2-runner` 连接到外部 Docker 网络 `quant-net`，并以 `quant-platform-net` 作为 Compose 内部别名。

服务器当前问题在于环境变量仍配置为宿主机地址：

```env
TREND_GRID_CLICKHOUSE_HTTP_URL=http://192.168.251.1:8123
```

目标配置为：

```env
TREND_GRID_CLICKHOUSE_HTTP_URL=http://quant-clickhouse:8123
```

前提是 `quant-clickhouse` 确实连接在外部网络 `quant-net`，且该网络在发布前已存在。容器中的 `127.0.0.1` 只指向 `l2-runner` 自己，不能用于访问 ClickHouse 容器。

### 3.2 订单驱动盘口预读

现有 `_QuotePrefetchBuffer（盘口预读缓冲）` 已按 `TREND_GRID_L2_QUOTE_PREFETCH_SIZE` 批量读取，默认每次最多 16 条。但其查询上界是下一根 Bar 的结束时间：

```text
quotes_after(after_time, next_bar.end_time, prefetch_size)
```

因此缓冲通常无法覆盖多个 Bar。即便同一交易日后续多次出现订单，也会重复访问 ClickHouse。现有任务的 `quoteRowsPrefetched（预读快照行数）` 已远大于 `quotesFetched（实际送入 worker 的快照数）`，说明每次小批读取本身也存在未消费数据；问题是这些数据经常在下一次订单边界被丢弃，未能形成跨 Bar 复用。

## 4. 目标架构

```text
浏览器 / 前端
       │
       ▼
backend ─── PostgreSQL / Redis（仅任务与派生 Bar）
       │
       ▼
l2-runner ── Docker 网络 quant-net ── quant-clickhouse:8123
       │
       ▼
strategy-worker（只接收正确时点的 Bar 或 Quote）
```

`l2-runner` 与 ClickHouse 保持 HTTP/TCP 通信。该通信在同一主机的 Docker 虚拟网络中完成，不经过物理局域网；HTTP 保留既有鉴权、超时、gzip 和故障诊断能力。

## 5. 详细设计

### 5.1 Docker 内部服务发现

1. 保留 `deploy/compose.clickhouse.yml` 中 `l2-runner -> quant-platform-net -> quant-net` 的外部网络连接。
2. 发布配置把 `TREND_GRID_CLICKHOUSE_HTTP_URL` 设为 `http://quant-clickhouse:8123`；不把服务器地址、密码等实际值提交到仓库。
3. 在发布脚本中增加发布前检查：
   - 外部网络 `quant-net` 存在；
   - 组合后的 Compose 配置中 `l2-runner` 连接该网络；
   - `deploy/.env` 中的 ClickHouse URL 为非空 URL。
4. 发布完成后，从 `quant-backtest-l2-runner` 容器执行名称解析和最小只读健康查询，确认 `quant-clickhouse` 可解析且 HTTP 8123 可用。

若内部 DNS 解析失败，发布应明确失败并保留旧容器状态；不得静默回退到局域网 IP。这样故障可见且不会在环境变化后悄悄走较差路径。

### 5.2 盘口块预读与事件边界

将 `_QuotePrefetchBuffer（盘口预读缓冲）` 改为单日有界顺序缓冲，默认一次最多读取 128 条连续竞价快照。128 条对应约 6 分钟 24 秒的 3 秒快照，能覆盖多个 1 分钟 Bar，同时内存上界固定。

查询条件仍使用主键友好的 keyset 范围：

```text
trade_date = ? AND symbol = ? AND datetime > cursor
ORDER BY datetime[, sequence]
LIMIT 128
```

查询不再以“下一根 Bar 的结束时间”作为 ClickHouse 上界，而是读取有限的后续块至交易时段结束。未来快照只进入 runner 内存，按以下规则消费：

1. 若当前有待成交订单且缓冲首条盘口早于下一根 Bar 结束时间，先将该盘口发送给 worker。
2. 若缓冲首条盘口不早于下一根 Bar 结束时间，先处理该 Bar；该盘口仍保留在缓冲中。
3. 该 Bar 新产生订单后，runner 仅丢弃不晚于该订单生效边界的旧快照；边界之后的已预读快照可直接复用。
4. 没有待成交订单时，runner 直接批量推进 Bar，不因缓冲中存在未来快照而调用 `on_quote` 或发生成交。
5. 日终若缓冲中已有收盘前最后有效盘口，则直接使用；否则才调用 `last_quote_before` 兜底读取。

为避免列表头部不断删除导致的额外复制，缓冲实现使用 `deque（双端队列）` 或游标索引，而不是 `list.pop(0)`。

### 5.3 缓冲大小与资源边界

- 新增 `TREND_GRID_L2_QUOTE_PREFETCH_SIZE` 的有效范围校验，例如 16 至 512；默认从 16 调整为 128。
- 单个交易日仅保留尚未过期、最多一个读取块的快照；达到上限前不再发起读取。
- 日切换和任务结束时显式清空缓冲。任务取消、异常和 worker 会话释放路径同样不得残留引用。
- 不使用 Redis 缓存盘口：十档快照体积大、按订单时机消费、跨任务命中不确定；写入 Redis 会增加序列化、内存与失效管理开销，且不能替代 ClickHouse 的可追溯原始数据。

### 5.4 性能可观测性

在现有 `performance（性能快照）` 基础上保留并补充：

| 字段 | 含义 |
| --- | --- |
| `sourceReadMs` | ClickHouse 查询与解码累计耗时。 |
| `quoteQueries` | 实际发起的盘口 SQL 查询次数。 |
| `quoteRowsPrefetched` | 从 ClickHouse 读入 runner 缓冲的快照行数。 |
| `quotesFetched` | 按正确事件顺序实际送入 worker 的快照数。 |
| `quoteBufferHits` | 直接由已有缓冲取得盘口的次数。 |
| `quoteBufferMisses` | 缓冲为空而需向 ClickHouse 读取的次数。 |
| `maxQuoteBufferRows` | 单任务实际达到的最大缓冲行数。 |
| `workerMs` | runner 与 strategy-worker 调用累计耗时。 |
| `totalElapsedMs` | 从任务开始到结果持久化前的总耗时。 |

性能快照仅记录计数和耗时，不记录原始盘口内容，避免扩大 PostgreSQL 任务结果。

## 6. 实施步骤

### 阶段 A：内部网络稳定性

1. 检查并补齐 Compose 覆盖配置与部署脚本的前置校验。
2. 在服务器 `deploy/.env` 手工设置 ClickHouse 服务名 URL；该环境文件不纳入 Git。
3. 使用部署脚本重建受影响的 `l2-runner` 容器。
4. 验证容器网络、`quant-clickhouse` DNS、8123 健康查询及一次只读表查询。
5. 使用同一缓存已命中的回测条件，重复执行至少 3 次，记录网络改造前后性能快照。

### 阶段 B：订单驱动盘口块预读

1. 调整 L2 Quote Provider 的有限条 keyset 查询，允许订单驱动缓冲读取有限的后续块，而非截断到下一根 Bar。
2. 重构 `_QuotePrefetchBuffer（盘口预读缓冲）` 的保留、淘汰与消费逻辑，确保未来快照不提前发送给 worker。
3. 优先复用缓冲中的日终最后有效盘口；保留原有日终查询作为安全兜底。
4. 增加配置范围校验和性能字段；更新任务结果性能摘要的兼容读取。
5. 编写单元、集成与回归测试；先验证成交完全一致，再判断性能是否达到保留条件。

## 7. 验证与验收

### 7.1 正确性测试

1. Bar 在 09:31:00 完成并下单时，09:31:00 盘口不得成交，09:31:03 才可成交。
2. 缓冲已读到 09:36 的盘口，但当前处理 09:32 Bar 时，09:32 之后盘口不得提前传给 worker。
3. 同一订单跨多个快照部分成交时，按盘口时间严格递增成交；全部完成后不继续处理无关盘口。
4. 下一根 Bar 时间先于缓冲首条可消费盘口时，必须先调用该 Bar。
5. 新订单生效时，缓冲内早于或等于其生效时间的快照被丢弃，晚于该时点的快照可复用。
6. 有/无日终强平、涨跌停单边盘口、取消任务、异常退出均保持既有结果和资源释放语义。
7. `continuous` 模式保持完整快照回放与 NDJSON 流式传输，不受订单驱动缓冲影响。

### 7.2 对照回测

选择固定的数据版本、标的、策略、日期范围、参数和 Bar 缓存状态，分别运行改造前与改造后任务。比较：

- 信号数量与每条信号的时间、动作、数量；
- 订单接受、拒绝、取消、部分成交和最终成交明细；
- 现金、仓位、日终权益、收益、回撤及尾盘结算结果；
- `sourceReadMs`、`quoteQueries`、`quoteRowsPrefetched`、`workerMs`、`totalElapsedMs`。

除新增性能字段外，订单驱动模式的业务结果必须一致。若出现差异，先修正事件边界，不以“性能更快”为由保留差异。

### 7.3 性能保留条件

阶段 A 与阶段 B 分开记录。阶段 B 只有同时满足以下条件才保留：

1. 在至少 3 次同条件、Bar 缓存命中的回测中，`quoteQueries` 明显下降。
2. `totalElapsedMs` 或 `sourceReadMs` 存在可重复的实际下降，而非单次网络波动。
3. `quoteRowsPrefetched` 不出现不成比例增长；最大缓冲行数始终在配置上限内。
4. 业务结果对照完全一致。

若查询次数下降但总耗时没有可重复改善，则保留阶段 A，撤回或不合入阶段 B，转而基于新的性能快照评估 ClickHouse 查询计划、projection 或正式 Bar 表。

## 8. 风险与处理

| 风险 | 处理方式 |
| --- | --- |
| 服务名解析失败 | 发布前检查外部网络，发布后从 runner 验证 DNS 与健康查询；失败不静默回退 IP。 |
| 未来盘口进入缓冲被误认为前视 | 缓冲仅是 runner 私有数据；由事件时间决定何时发送 worker，并用边界测试固定。 |
| 大预读造成无效读取或内存上涨 | 采用 128 条有界块、记录读取与消费比；若实测无收益则不保留。 |
| 查询跨午休或收盘 | Provider 保持连续竞价时段过滤，runner 仍按交易日重建缓冲。 |
| 缓冲淘汰错误导致漏成交 | 对部分成交、跨 Bar、订单生效边界和日终强平建立回归测试。 |
| 网络改造与代码优化混在一起，无法定位收益 | 分阶段发布、分阶段对比并在任务快照记录配置。 |
| ClickHouse 本身扫描慢 | 本方案不修改远端表；若实测确认仍是扫描瓶颈，再独立评估排序键、projection 或物化视图。 |

## 9. 发布、回滚与后续决策

发布顺序为：阶段 A 验证通过后，再发布阶段 B。每阶段都保留对应提交与基准任务编号。

阶段 A 回滚只需恢复 `TREND_GRID_CLICKHOUSE_HTTP_URL` 并重建 `l2-runner`；阶段 B 回滚只需恢复旧的预读边界和默认预读大小，不涉及数据库迁移、Redis 清理或历史任务重算。

本方案完成后，以实测结果决定下一步：若 `sourceReadMs` 仍占主要部分且查询次数已足够低，再进行 ClickHouse `EXPLAIN indexes = 1`、投影或正式 Bar 表的独立方案；若 `workerMs` 成为主因，则另行审查策略执行与 runner/worker 协议，而不是继续调整数据连接。
