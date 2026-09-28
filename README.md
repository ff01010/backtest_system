# Quant Backtest Platform

面向多用户的 A 股量化回测工作台。系统支持受控登录、人员管理、策略与方案保存、分钟级回测和历史记录查询；策略、方案和回测结果按用户隔离。

本项目只使用管理员预先导入 ClickHouse 的 QMT 原始历史行情，不会在用户运行回测时联网下载数据。

## 核心能力

- 管理员创建、启用、停用用户并重置密码；系统不开放自助注册。
- 用户维护自己的策略脚本、回测方案、回测账户和历史回测记录，无法读取其他用户的数据。
- 回测支持 `1m`、`3m`、`5m`、`15m`、`30m`、`45m`、`60m`、`1d`；优先读取已发布的原生六周期行情，`3m`、`45m` 由完整 `1m` 数据聚合。
- 回测前检查标的和基准标的在目标日期内的行情覆盖状态；数据缺失或不完整时拒绝回测。
- Bar 与 L2 Runner 为每个任务创建独立策略子进程，并清空服务凭据后执行用户脚本。

## 架构

```text
浏览器 → Nginx 前端 → FastAPI 后端 → PostgreSQL 任务队列
                                  ├─ bar-runner × N → 单任务策略子进程
                                  └─ l2-runner × N → ClickHouse/Redis → 单任务策略子进程
```

| 组件 | 职责 |
| --- | --- |
| `frontend/` | Vite + React 前端；生产环境由 Nginx 提供服务并转发 `/api` 请求。 |
| `backend/` | FastAPI 接口、权限校验、回测编排和行情覆盖检查。 |
| `bar-runner` | 领取标准 Bar 任务，并在独立子进程中执行用户策略。 |
| `l2-runner` | 领取 L2 任务、读取盘口，并在独立子进程中执行策略与撮合。 |
| PostgreSQL | 保存用户、策略、方案、账户和回测记录。 |
| ClickHouse | 保存 QMT 原始历史 K 线、覆盖状态和导入批次。 |

## 运行拓扑与数据库设计

本地与服务器使用相同的数据职责划分：PostgreSQL 保存业务状态，ClickHouse 保存 QMT 行情和统一证券目录。当前本地 Docker 环境中的后端通过 `TREND_GRID_CLICKHOUSE_*` 连接远程共享 ClickHouse；不依赖本机 `quant-net`。服务器也使用独立的 PostgreSQL 容器和同一职责的 ClickHouse 数据库。

| 数据类型 | PostgreSQL | ClickHouse |
| --- | --- | --- |
| 用户、会话、权限审计 | 是 | 否 |
| 策略源码、版本、方案、账户 | 是 | 否 |
| 回测任务、快照、事件、产物元数据 | 是 | 否 |
| QMT 原始分钟/日线和完整度 | 否 | 是 |
| 统一证券目录 | 同步后的搜索副本 | 是 |

本地 PostgreSQL 容器的数据持久化在 Docker 卷 `quant-backtest-postgres-data`；服务器 PostgreSQL 同样只对 Docker 内网开放。ClickHouse 地址、用户名和密码仅保存在 `deploy/.env`，不得写入文档或代码。

### PostgreSQL 业务表

| 表 | 关键字段 | 用途与关系 |
| --- | --- | --- |
| `app_user` | `id`、`username`、`email`、`display_name`、`password_hash`、`role`、`status`、`max_concurrent_runs`、`strategy_quota`、`created_at`、`updated_at`、`last_login_at` | 用户主体；`role` 为 `admin` 或 `user`，`status` 为 `active` 或 `disabled`。 |
| `auth_session` | `id`、`user_id`、`token_hash`、`expires_at`、`revoked_at`、`last_seen_at`、`client_ip`、`user_agent` | 登录会话；`user_id` 关联 `app_user`，只保存令牌哈希。 |
| `audit_log` | `id`、`actor_user_id`、`target_user_id`、`action`、`resource_type`、`resource_id`、`client_ip`、`metadata`、`created_at` | 权限与管理操作审计。 |
| `user_strategy` | `id`、`owner_user_id`、`name`、`description`、`strategy_type`、`status`、`current_version_no`、`created_at`、`updated_at` | 策略元数据；一条策略拥有多个版本。 |
| `user_strategy_version` | `id`、`strategy_id`、`version_no`、`source_code`、`source_checksum`、`runtime_config`、`created_by_user_id`、`created_at` | 策略源码和运行配置；`source_checksum` 防止同一策略重复版本。 |
| `backtest_account` | `id`、`owner_user_id`、`name`、`initial_capital`、`commission_rate`、`slippage`、`position_mode`、`order_cash`、`order_pct`、`allow_short`、`is_default`、时间字段 | 用户回测账户与交易成本参数。 |
| `strategy_config` | `id`、`owner_user_id`、`name`、`strategy_id`、`strategy_key`、`mode`、`instrument_id`、`timeframe`、`benchmark_instrument_id`、`account_id`、`params`、`enabled`、`published`、时间字段 | 可执行回测方案；关联策略、标的、基准和账户。 |
| `strategy_config_version` | `id`、`config_id`、`version_no`、`strategy_version_id`、`snapshot`、`change_note`、`created_at` | 方案版本；保存策略版本引用与配置快照。 |
| `backtest_run` | `id`、`display_no`、`owner_user_id`、`strategy_version_id`、`config_id`、`config_version_id`、`account_id`、`instrument_id`、`benchmark_instrument_id`、`timeframe`、`status`、`fill_model`、`requested_params`、`market_data_snapshot`、`script_snapshot`、`params_snapshot`、`result_summary`、`result_json`、`error_message`、任务时间字段 | 一次回测的可追溯快照和结果；`display_no` 按用户递增。 |
| `backtest_run_event` | `id`、`run_id`、`event_type`、`payload`、`created_at` | 回测运行事件流。 |
| `artifact_file` | `id`、`owner_user_id`、`run_id`、`artifact_type`、`storage_key`、`original_filename`、`content_type`、`byte_size`、`content_sha256`、`created_at`、`expires_at` | 回测产物文件的元数据；实际文件位于产物卷。 |
| `market_instrument` | `id`、`symbol`、`exchange`、`display_name`、`instrument_type`、`board`、`eastmoney_secid`、`list_date`、`delist_date`、`is_active`、`source_metadata`、时间字段 | 应用搜索用证券目录。后端启动时从 ClickHouse 的 `xl_derived.stock_directory` 同步活跃 A 股；`eastmoney_secid` 为历史兼容标识字段，不代表当前数据来源。 |
| `l2_quote_daily_availability` | `source_key`、`symbol`、`trade_date`、`status`、`snapshot_count`、首末快照时间 | L2 日级可用性摘要；页面只读其完整日期范围，不在请求阶段扫描或回填原始 L2 行情。 |
| `user_backtest_counter` | `owner_user_id`、`next_display_no` | 为每名用户原子分配回测展示编号。 |

`auth_session`、策略、回测、产物和计数器表启用行级安全策略；常规用户只能读取和修改自己拥有的数据，管理员可执行管理职责。

迁移中还保留 `market_trade_calendar`、`market_bar`、`market_data_coverage`、`market_fetch_log`、`market_minute_bar`、`market_minute_coverage` 等历史兼容结构。它们包含标的、周期、OHLCV、来源、覆盖日期和校验状态等字段；当前回测路径不读取这些 PostgreSQL 行情表，而是只读取下列 ClickHouse QMT 表。

### ClickHouse 行情与证券目录表

沪、深、北交所统一优先从 `qmt_history_dev` 的六张周期原始表读取 `none` 口径的有效最终数据。界面优先展示 `qmt_ingest_runs` 与 `qmt_universe_snapshot` 声明的新版范围；仅当新版元数据查询成功且没有该标的时，才展示 `qmt_kline_coverage` 声明的旧版范围，不合并两套范围，也不在保存或提交时扫描原始 Bar。范围查询会对超时和临时网络错误进行有限重试；重试耗尽或发生确定性查询错误时直接报告行情数据异常，不回退旧版范围。Worker 实际执行时使用覆盖元数据选择来源并读取行情；新库不能覆盖预热期和正式回测期时，整次任务回退到 `raw_historical_data.qmt_kline`，同一任务不跨两库拼接。统一证券目录位于 `xl_derived`。

日期选择过程只修改本地草稿；用户点击“应用”时，页面才校验所选闭区间是否完整落在当前可回测起止范围内。范围端点可以选取，越界、倒置或范围尚不可用时不更新正式参数；该边界校验不扫描行情，也不判断范围内逐日数据完整性。

| 表 | 字段 | 用途 |
| --- | --- | --- |
| `qmt_kline` | `symbol`、`period`、`qmt_index`、`time`、`open`、`high`、`low`、`close`、`volume`、`amount`、`settelementPrice`、`openInterest`、`preClose`、`suspendFlag`、`import_batch_id`、`ingested_at`、`ingest_version` | QMT 原始 K 线。按 `symbol`、`period`、时间索引保存，保留 QMT 原字段与导入版本。 |
| `qmt_kline_coverage` | `symbol`、`period`、`trade_date`、`first_bar_time`、`last_bar_time`、`expected_row_count`、`observed_row_count`、`valid_row_count`、`status`、`quality_note`、`import_batch_id`、`record_version` | 每标的、周期、交易日的完整度。`status` 只能为 `complete`、`partial`、`empty`、`failed`；非完整数据拒绝回测。 |
| `qmt_import_batch` | `batch_id`、`source_file`、`source_file_sha256`、`symbol`、`period`、`data_start_time`、`data_end_time`、`source_row_count`、`imported_row_count`、`invalid_row_count`、`status`、`failure_reason`、`qmt_client_version`、`created_at`、`updated_at`、`record_version` | 每个原始 CSV 的导入审计与失败原因。 |
| `qmt_history_dev.qmt_kline_{1m,5m,15m,30m,1h,1d}_raw` | `symbol`、`period`、`adjustment`、`trade_date`、`bar_time`、OHLCV、`is_final`、`quality_status`、`run_id`、`manifest_sha256`、`revision` | 优先行情来源；平台 `60m` 映射到物理 `1h`，仅读取 `none`、有效且定稿的当前修订。 |
| `qmt_history_dev.qmt_ingest_runs` | `run_id`、`state`、`target_date`、路由数量、缺失与重复计数、`validation_result`、`actual_routes` | 新库独立的发布和完整性依据，不与旧库 `qmt_kline_coverage` 的状态语义混用。 |
| `qmt_history_dev.qmt_universe_snapshot` | `universe_snapshot_id`、`effective_date`、`symbol`、`exchange`、`is_active` | 记录每次发布对应的标的范围；与 `qmt_ingest_runs` 结合后用于展示标的可回测日期范围。 |
| `xl_derived.stock_directory` | `symbol`、`exchange`、`name`、`type`、`status`、`board_type`、`listed_date`、`de_listed_date`、`source`、`__ver` | 全市场证券主数据。后端启动时筛选 `CS/Active`，规范化交易所和板块后同步到 PostgreSQL `market_instrument`。 |

### 关系与读取规则

```text
app_user → user_strategy → user_strategy_version
         → backtest_account
         → strategy_config → strategy_config_version
         → backtest_run → backtest_run_event / artifact_file

ClickHouse xl_derived.stock_directory → PostgreSQL market_instrument
ClickHouse qmt_history_dev 六周期表/qmt_ingest_runs/qmt_universe_snapshot → 可回测范围与优先回测行情
ClickHouse raw_historical_data.qmt_kline/qmt_kline_coverage → 整段兼容回退
```

- 策略源码、方案、账户、回测快照和结果始终保存于 PostgreSQL，不写入 ClickHouse。
- ClickHouse 保存行情、导入审计和统一证券目录；回测不会触发外部行情下载。
- 新库直接读取 `1m`、`5m`、`15m`、`30m`、`1h`、`1d` 六个原始周期；平台 `3m`、`45m` 由新库完整 `1m` 数据派生。新库不能覆盖整个任务范围时，旧库保持原有直接周期和分钟派生规则。
- 本地启动成功后，后端会从远程 ClickHouse 的 `xl_derived.stock_directory` 同步活跃 A 股目录；本机和服务器共享相同的目录映射规则。


## 快速启动（本地 Docker）

### 前置条件

- 已安装并启动 Docker Desktop。
- `deploy/.env` 已存在，且已配置 PostgreSQL 应用账号、端口和 ClickHouse 连接信息。该文件包含敏感信息，不应提交到 Git。
- 若需要实际运行回测，ClickHouse 必须可访问，且其中已导入所需 QMT 行情。仅启动网页和 API 不代表行情数据已就绪。

在项目根目录执行一键启动脚本：

```powershell
cd C:\Users\zhongying\backtest_system
.\scripts\start-local.ps1
```

脚本会启动 PostgreSQL、等待其健康检查、执行数据库迁移，再构建并启动 Bar/L2 Runner、后端和前端，最后请求 `/api/health` 验证应用。

若镜像无需重建，可使用：

```powershell
.\scripts\start-local.ps1 -SkipBuild
```

访问地址由 `deploy/.env` 的 `APP_HTTP_PORT` 决定：

```text
http://localhost:<APP_HTTP_PORT>
```

例如 `APP_HTTP_PORT=28601` 时，访问 `http://localhost:28601`；健康检查地址为 `http://localhost:28601/api/health`。请勿将示例环境文件中的默认端口 `8080` 当作当前部署端口。

首次初始化会创建管理员 `admin` 和默认账户；初始密码仅用于首次登录，应立即重置，且不应在公网环境继续使用默认凭据。

## 配置与数据边界

`deploy/.env` 中的常用配置如下：

| 配置项 | 用途 |
| --- | --- |
| `APP_HTTP_PORT` | 前端对宿主机公开的 HTTP 端口。 |
| `POSTGRES_*`、`APP_DB_*` | PostgreSQL 引导账号和应用账号。两套密码应不同。 |
| `TREND_GRID_CLICKHOUSE_*` | 后端访问共享 ClickHouse 的地址和凭据。 |
| `TREND_GRID_HOST_KLINE_ROOT` | 旧 CSV 兼容挂载；当前回测不依赖其中的数据。 |
| `TREND_GRID_HOST_SIGNAL_ROOT` | 外部信号 CSV 的只读挂载目录。 |

ClickHouse 不由 `scripts/start-local.ps1` 创建或导入数据。当前本地后端通过 `TREND_GRID_CLICKHOUSE_*` 直接连接已配置的远程 ClickHouse；具体服务器部署方式见下方部署说明。

## 数据与安全约束

- QMT 历史行情须先从 Windows QMT 导出、完成校验后导入 ClickHouse；回测不会调用第三方行情接口。
- 仅覆盖状态为完整的日期会被用于回测；单日 K 线根数不足会标记为 `partial` 并拒绝运行。
- 部署到公网时必须配置 HTTPS，并设置 `TREND_GRID_AUTH_COOKIE_SECURE=true`。
- PostgreSQL、后端和 Runner 的内部端口不应直接映射到公网；系统只需公开前端 HTTP(S) 入口。

## 项目结构

```text
backend/       FastAPI、回测内核、QMT ClickHouse 读取服务
frontend/      Vite + React 前端
db/            PostgreSQL 初始化脚本与版本化迁移
deploy/        Docker Compose、Dockerfile、Nginx 与环境变量示例
scripts/       本地启动脚本
docs/          使用、策略编写与服务器部署文档
```

## 延伸文档

- [用户使用说明](docs/user_guide.md)：账号、策略、方案和回测的使用流程。
- [策略代码编辑器使用手册](docs/玄龄回测系统使用手册.md)：`on_bar` 契约、可用字段和委托规则的唯一说明。
- [服务器部署与维护说明](docs/server_deployment_guide.md)：服务器目录、Compose 启动顺序、ClickHouse 接入、迁移与排障。

## 开发与验证

本地开发可分别启动后端和前端：

```powershell
python -m pip install -r backend\requirements.txt
$env:TREND_GRID_DATABASE_URL = 'postgresql://quant_backtest_app:<密码>@127.0.0.1:5432/quant_backtest_platform'
python backend\app.py

cd frontend
npm run dev
```

前端开发服务器默认使用 `5174`，并把 API 请求代理到 `http://127.0.0.1:8000`；这仅适用于后端与依赖服务已自行准备好的开发场景。

```powershell
python -m pytest backend/tests
cd frontend
npm test
npm run build
```
