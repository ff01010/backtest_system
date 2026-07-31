# Quant Backtest Platform

多用户量化回测工作台：提供受控登录、人员管理、策略保存、分钟回测和回测历史。用户、策略和回测记录存储在 PostgreSQL；QMT 原始历史行情存储在 ClickHouse，回测只读取已导入的数据。

## 架构与数据流

```text
浏览器 → Nginx → FastAPI → PostgreSQL
                         ├─ QMT 覆盖检查与 K 线读取 → ClickHouse
                         └─ 策略快照 + 行情 → strategy-worker → 回测结果
```

- 回测当前支持 `1m`、`5m`；QMT 还保存 `60m`、`1d` 原始 K 线，`15m`、`30m` 由 `1m` 聚合。
- 数据按“标的、周期、交易日”记录覆盖状态。未导入或不完整的数据不会用于回测，也不会在回测时联网下载。
- 策略源码、方案、版本和回测记录按登录用户隔离；普通用户无法读取其他人的数据；回测展示编号也从每位用户自己的 `#01` 开始递增。
- 策略代码在无数据库凭据、无宿主机目录挂载的 `strategy-worker` 容器执行；该容器只接收本次脚本快照与 K 线数据。
- 初始化仅创建管理员 `admin`（初始密码 `123456`）、管理员默认账户，以及管理员策略库中的一条均线示例策略。请首次登录后重置管理员密码。

## 目录结构

```text
backend/       FastAPI、回测内核、QMT ClickHouse 读取服务
frontend/      Vite + React 前端
db/            PostgreSQL 初始化与版本化迁移
deploy/        Docker Compose、镜像与 Nginx 配置
```

## 数据库模型

数据库通过 `db/migrations` 创建和演进：

- `001_initial_schema.sql`：用户、策略、回测记录、日线市场模型。
- `002_backtest_workspace.sql`：回测账号、方案和版本。
- `003_market_minute_cache.sql`：分钟 K 线缓存与按日覆盖状态。
- `004_authentication_and_users.sql`：默认管理员与账户查询索引。
- `005_strategy_runtime.sql`：隔离策略运行标记与管理员示例策略。
- `006_global_market_instrument_directory.sql`：全市场证券目录与搜索索引。
- `007_backtest_run_display_number.sql`：按用户递增的回测展示编号与原子计数器。

QMT 原始历史行情位于 ClickHouse 数据库 `raw_historical_data`：

- `qmt_kline`：保留 QMT 原始索引和 11 个原始字段，按标的、周期、毫秒时间戳去重。
- `qmt_kline_coverage`：每个交易日的 `complete`、`partial`、`empty`、`failed` 状态。
- `qmt_import_batch`：原始文件的校验值、导入行数和执行结果。

后端不执行 DDL。部署时必须先运行迁移服务；后端启动只检查必需表是否存在。

## 服务器部署

以下示例以 Linux 服务器目录 `/opt/projects/quant_backtest_platform_git` 为例。

### 1. 获取代码并准备环境文件

```bash
cd /opt/projects/quant_backtest_platform_git
git pull --ff-only origin main
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

编辑 `deploy/.env`：为 `POSTGRES_PASSWORD` 与 `APP_DB_PASSWORD` 设置两个不同的高强度随机密码。默认挂载路径如下：

```text
TREND_GRID_HOST_KLINE_ROOT=/srv/quant-backtest/data/kline
TREND_GRID_HOST_SIGNAL_ROOT=/srv/quant-backtest/data/signals
```

`TREND_GRID_HOST_KLINE_ROOT` 是旧 CSV 兼容挂载，可以为空；回测不依赖其中的数据。

### 2. 创建宿主机挂载目录

```bash
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/quant-backtest/data/kline
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/quant-backtest/data/signals
```

不再需要 `/srv/quant-backtest/code`，也不再需要 `shared/` 或外部趋势网格策略代码。当前回测行情全部来自 ClickHouse 中已导入的 QMT 数据。

### 3. 启动 PostgreSQL 并执行迁移

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d postgres
docker inspect --format '{{.State.Health.Status}}' quant-backtest-postgres
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml --profile migration run --rm migrate
```

健康检查显示 `healthy` 后再执行迁移。升级项目且出现新的 SQL 迁移文件时，重复执行迁移命令。

### 4. 启动应用

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml -f deploy/compose.clickhouse.yml up -d --build strategy-worker backend frontend
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml -f deploy/compose.clickhouse.yml ps
```

前端默认访问地址为 `http://服务器地址:8080`。健康检查为 `GET /api/health`。

首次访问会进入登录页。初始管理员是 `admin` / `123456`；系统不开放自助注册，人员必须由管理员在“人员管理”页创建。部署在公网前必须配置 HTTPS，并在 `deploy/.env` 中设置 `TREND_GRID_AUTH_COOKIE_SECURE=true`。

## QMT 行情读取行为

当用户运行回测时：

1. 后端在点击回测时检查目标标的和基准标的在请求日期范围内的 QMT 覆盖状态。
2. 数据完整时直接从 ClickHouse 读取原始 K 线。
3. 单日根数不足时标记为 `partial` 并拒绝回测；未导入数据同样拒绝回测。
4. 回测不会调用东财、新浪或其他外部行情接口。

数据管理接口只检查同一份 QMT 覆盖状态：

- `POST /api/data/sync`
- `POST /api/data/sync/batch`
- `POST /api/data/refill`

未传日期范围的检查默认使用最近 7 个自然日。回测请求始终以表单提供的日期范围为准。

## 本地开发

后端依赖已锁定在 `backend/requirements.txt`：

```powershell
cd C:\Users\zhongying\backtest_system
python -m pip install -r backend\requirements.txt
$env:TREND_GRID_DATABASE_URL = 'postgresql://quant_backtest_app:密码@127.0.0.1:5432/quant_backtest_platform'
python backend\app.py
```

前端：

```powershell
cd C:\Users\zhongying\backtest_system\frontend
npm run dev
```

测试：

```powershell
python -m pytest backend/tests
cd frontend
npm test
npm run build
```

## 主要接口

- `GET /api/health`
- `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`
- `GET|POST|PUT /api/admin/users`、`POST /api/admin/users/{id}/reset-password`（仅管理员）
- `GET /api/instruments`、`POST /api/instruments`
- `GET /api/backtest/timeframes`
- `POST /api/backtest/run`
- `GET /api/backtest/runs`、`GET /api/backtest/runs/{run_id}`
- `GET /api/chart`
- `GET /api/data/status`
- `POST /api/data/sync`

## 当前边界

- QMT 原始数据由本地 Windows QMT 导出后导入服务器 ClickHouse；导入前必须执行 dry-run 校验。
- 当前回测支持 `1m` 与 `5m`；`60m`、`1d` 已可保存，`15m`、`30m` 由 `1m` 聚合。
- 信号文件仍依赖外部 CSV，暂不入库。
- 当前仅支持数据库中保存的 `on_bar` 用户脚本；策略保存后即可回测，历史回测保留策略和参数快照。
- 若部署到公网，必须配置 HTTPS 并启用 `TREND_GRID_AUTH_COOKIE_SECURE=true`；当前系统适合内网或 VPN 使用。
