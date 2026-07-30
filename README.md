# Quant Backtest Platform

趋势网格策略工作台：提供策略方案管理、分钟回测、信号可视化和回测历史。运行时状态与分钟行情均存储在 PostgreSQL；首次请求某个标的和日期区间时，系统从东财拉取分钟 K 线并缓存，后续请求直接复用缓存。

## 架构与数据流

```text
浏览器 → Nginx → FastAPI → PostgreSQL
                         ├─ 命中分钟缓存：直接回测
                         └─ 缺失分钟数据：东财 → 校验 → PostgreSQL → 回测
```

- 回测支持 `1m`、`5m`，仅使用 PostgreSQL 中已验证的分钟 K 线。
- 数据按“标的、周期、交易日”记录覆盖状态。缺失数据仅拉取一次；不完整或拉取失败的数据不会用于回测。
- 信号文件仍以 CSV 形式只读挂载，用于信号可视化；它不是回测行情来源。
- 趋势网格引擎和 `shared/` 仍由外部策略代码目录只读挂载提供。

## 目录结构

```text
backend/       FastAPI、回测内核、东财适配器和缓存服务
frontend/      Vite + React 前端
db/            PostgreSQL 初始化与版本化迁移
deploy/        Docker Compose、镜像与 Nginx 配置
```

## 数据库模型

数据库通过 `db/migrations` 创建和演进：

- `001_initial_schema.sql`：用户、策略、回测记录、日线市场模型。
- `002_backtest_workspace.sql`：回测账号、方案和版本。
- `003_market_minute_cache.sql`：分钟 K 线缓存与按日覆盖状态。

分钟缓存的关键表：

- `market_minute_bar`：`1m/5m` OHLCV 数据，按标的、周期、复权方式、分钟时间去重。
- `market_minute_coverage`：每个交易日的 `complete`、`empty`、`incomplete`、`failed` 状态。
- `market_fetch_log`：缓存命中、东财拉取和失败记录。

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
TREND_GRID_HOST_CODE_ROOT=/srv/quant-backtest/code
```

`TREND_GRID_HOST_KLINE_ROOT` 是旧 CSV 兼容挂载，可以为空；分钟回测不依赖其中的数据。

### 2. 创建宿主机挂载目录

```bash
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/quant-backtest/data/kline
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/quant-backtest/data/signals
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/quant-backtest/code
```

外部策略代码目录至少需要：

```text
/srv/quant-backtest/code/shared/
/srv/quant-backtest/code/strategies/trend_grid/engine.py
/srv/quant-backtest/code/strategies/trend_grid/config.yaml
```

若不使用信号可视化，`/srv/quant-backtest/data/signals` 可以为空。

### 3. 启动 PostgreSQL 并执行迁移

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d postgres
docker inspect --format '{{.State.Health.Status}}' quant-backtest-postgres
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml --profile migration run --rm migrate
```

健康检查显示 `healthy` 后再执行迁移。升级项目且出现新的 SQL 迁移文件时，重复执行迁移命令。

### 4. 启动应用

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d --build backend frontend
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml ps
```

前端默认访问地址为 `http://服务器地址:8080`。健康检查为 `GET /api/health`。

## 分钟缓存行为

当用户运行回测时：

1. 后端检查目标标的和基准标的在请求日期范围内的缓存覆盖状态。
2. 已完整缓存的数据直接读取 PostgreSQL。
3. 只对缺失日期请求东财分钟 K 线，随后校验 OHLC、去重并写入缓存。
4. 单日根数不足时标记为 `incomplete` 并拒绝回测；网络或上游失败时标记为 `failed` 并返回错误。
5. 同一标的、周期的并发请求通过 PostgreSQL 咨询锁串行化，避免重复拉取。

手工数据同步接口也会写入同一缓存：

- `POST /api/data/sync`
- `POST /api/data/sync/batch`
- `POST /api/data/refill`

未传日期范围的手工同步默认尝试最近 7 个自然日。回测请求始终以表单提供的日期范围为准。

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
- `GET /api/instruments`、`POST /api/instruments`
- `GET /api/backtest/timeframes`
- `POST /api/backtest/run`
- `GET /api/backtest/runs`、`GET /api/backtest/runs/{run_id}`
- `GET /api/chart`
- `GET /api/data/status`
- `POST /api/data/sync`

## 当前边界

- 东财分钟行情是临时数据源，可能受历史范围、限流、停牌数据与上游响应变化影响。
- 当前只支持 `1m` 与 `5m` 分钟缓存；日线表保留给后续完整数据源。
- 信号文件仍依赖外部 CSV，暂不入库。
- 趋势网格策略代码仍依赖外部挂载目录；容器镜像不包含私有策略代码。
- 若部署到公网，需在 Nginx 前增加 HTTPS 与访问认证；当前系统适合内网或 VPN 使用。
