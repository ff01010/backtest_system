# Quant Backtest Platform

趋势网格策略工作台，提供信号可视化、策略方案管理与回测历史查询。

## 目录结构

```text
backend/       FastAPI 后端和回测引擎
frontend/      Vite + React 前端
db/            PostgreSQL 初始化和版本化迁移
deploy/        Docker Compose 基础设施配置
```

## 数据库

页面状态、策略、方案版本和回测记录全部使用 PostgreSQL。`db/migrations/001_initial_schema.sql`
是核心规范模型，`002_backtest_workspace.sql` 只补充回测工作台实体；不再使用 MySQL，
也不保留 `viewer_*` 表。

策略、方案、方案版本和回测记录均使用 UUID，前后端 API 以字符串传递这些 ID。回测账号
是独立业务实体，继续使用 bigint 自增 ID。

在服务器上配置数据库：

```bash
cd /opt/projects/quant_backtest_platform_git
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，替换所有示例密码和数据目录
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d postgres
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml --profile migration run --rm migrate
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d --build backend frontend
```

迁移服务会按文件名顺序执行 `db/migrations/*.sql`。后端不执行 DDL；若迁移尚未完成，
启动检查会直接报错。

容器运行时会将宿主机 K 线、信号和策略代码目录只读挂载到应用中。策略代码根目录必须
包含 `shared/` 以及 `strategies/trend_grid/engine.py`。回测产物写入 Docker 卷
`quant-backtest-artifacts`。QMT 数据同步脚本不在本仓库且不能在 Linux 容器中运行，
服务器部署后应由采集端先把数据写入 K 线挂载目录。

## 后端配置与启动

后端需要以下环境变量：

```text
TREND_GRID_DATABASE_URL=postgresql://quant_backtest_app:密码@127.0.0.1:5432/quant_backtest_platform
TREND_GRID_DEFAULT_USER_ID=00000000-0000-0000-0000-000000000001
TREND_GRID_KLINE_ROOT=/srv/quant-backtest/data/kline
TREND_GRID_SIGNAL_ROOT=/srv/quant-backtest/data/signals
```

本地开发可在 Python 虚拟环境中安装锁定依赖并启动：

```powershell
cd C:\Users\zhongying\backtest_system
python -m pip install -r backend\requirements.txt
$env:TREND_GRID_DATABASE_URL = 'postgresql://quant_backtest_app:密码@127.0.0.1:5432/quant_backtest_platform'
python backend\app.py
```

后端健康检查：`GET http://127.0.0.1:8000/api/health`。

## 前端启动

```powershell
cd C:\Users\zhongying\backtest_system\frontend
npm run dev
```

默认地址为 `http://127.0.0.1:5174`。

## API 概览

基础接口：

- `GET /api/health`
- `GET /api/strategies`
- `GET /api/instruments`
- `GET /api/dates`
- `GET /api/chart`
- `GET /api/signal-files`
- `POST /api/data/sync`

账号和标的管理：

- `GET /api/accounts`
- `POST /api/accounts`
- `PUT /api/accounts/{id}`
- `DELETE /api/accounts/{id}`
- `POST /api/instruments`
- `PUT /api/instruments/{symbol}`
- `DELETE /api/instruments/{symbol}`

回测：

- `POST /api/backtest/run`

回测请求示例：

```json
{
  "accountId": 1,
  "account": "default",
  "symbol": "688981.SH",
  "strategy": "trend_grid",
  "startDate": "20260623",
  "endDate": "20260624",
  "initialCapital": 1000000,
  "commissionRate": 0.0003,
  "slippage": 0,
  "positionMode": "fixed_cash",
  "orderCash": 100000,
  "orderPct": null,
  "allowShort": true
}
```

## 当前能力

信号可视化页面：

- 选择标的、日期、策略和信号文件。
- 显示 5m K 线、成交量、收盘线。
- 显示 A/B 模块买卖 marker。
- 支持实际触发和理论触发对比。
- 支持自动刷新和同步 K 线。

策略回测页面：

- 添加和选择标的。
- 添加和选择回测账号。
- 可视化编辑初始资金、手续费、滑点、仓位模式、单笔金额、单笔比例、是否允许做空。
- 调用现有 `BacktestEngine` 运行同步回测。
- 展示总收益、最大回撤、胜率、交易次数、净值曲线和交易明细。
- 回测记录写入 PostgreSQL。

## 已知边界

- K 线和信号仍读取 CSV 文件，不写入 PostgreSQL。
- 手续费参数会保存到回测记录；当前引擎仍使用 `shared.cost` 中的全局成本常量。
- `allowShort=false` 暂未真正改变引擎行为，当前引擎仍按策略默认双向逻辑执行。
- 交易明细中的 `time` 字段当前由引擎输出为空，页面会显示 `-`。
- 回测接口目前是同步执行；如果后续回测耗时明显增加，建议改成任务式接口。

## 常见问题

### ModuleNotFoundError: No module named 'psycopg'

原因是当前虚拟环境尚未安装后端锁定依赖。

解决：

```powershell
python -m pip install -r backend\requirements.txt
```

### 前端请求接口失败

确认后端已经启动在：

```text
http://127.0.0.1:8000
```

确认前端 Vite 代理配置在：

```text
frontend/vite.config.ts
```

### 回测提示没有 K 线数据

确认对应标的文件存在。例如：

```text
%TREND_GRID_KLINE_ROOT%\5m\688981_SH\688981_SH_5m.csv
```
